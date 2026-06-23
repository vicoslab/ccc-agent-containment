"""Operator/hook control surface over branch sessions (ccc-agentctl).

Hooks are *reporters*: ``finish-turn`` only records lifecycle events, and
``check-before-final`` only reads live status to drive bounded self-repair.
Commit authority stays here, in trusted supervisor code, behind explicit
operator commands (or the runner's policy decision).
"""

import difflib
import json
import os
import shutil
import subprocess
import sys

from .policy import PolicyConfig, classify
from .runner import finalize_session
from .session import TERMINAL_STATES

# check-before-final outcomes (stable strings for hook adapters and logs)
CHECK_ALLOW = "allow"          # change set clean: finish normally
CHECK_REPAIR = "repair"        # dirty, budget left: agent should revert
CHECK_EXHAUSTED = "exhausted"  # dirty, budget spent: defer to human review


class ControlError(Exception):
    pass


class Controller(object):
    def __init__(self, store, backend, alias_map):
        self.store = store
        self.backend = backend
        self.alias_map = alias_map

    # -- helpers -----------------------------------------------------------
    def _load(self, session_id):
        try:
            return self.store.load(session_id)
        except KeyError:
            raise ControlError("no such session: %s" % session_id)

    def _require_state(self, session, allowed, action):
        if session.state not in allowed:
            raise ControlError(
                "cannot %s session %s in state %s (needs one of: %s)"
                % (action, session.session_id, session.state,
                   ", ".join(allowed)))

    # -- read-only ----------------------------------------------------------
    def list(self, out=None):
        out = out or sys.stdout
        sessions = self.store.list()
        out.write("%-42s %-16s %-14s %s\n"
                  % ("SESSION", "STATE", "AGENT", "CREATED"))
        for session in sessions:
            out.write("%-42s %-16s %-14s %s\n"
                      % (session.session_id, session.state,
                         session.agent_kind, session.created_at))
        return sessions

    def show(self, session_id, out=None):
        out = out or sys.stdout
        session = self._load(session_id)
        json.dump(session.to_dict(), out, indent=2, sort_keys=True)
        out.write("\n")
        return session

    def status(self, session_id, out=None):
        """Live BranchFS status for each protected root."""
        out = out or sys.stdout
        session = self._load(session_id)
        for name, root in sorted(session.protected_roots.items()):
            out.write("# root %s (branch %s)\n" % (name, root.branch))
            for change in self.backend.status(root):
                out.write("%s %s (%s, %d bytes)\n"
                          % (change.op, change.path, change.kind,
                             change.bytes))
        return session

    def diff(self, session_id, out=None):
        """Stored review diff; falls back to live status when absent."""
        out = out or sys.stdout
        session = self._load(session_id)
        review = self.store.review_dir(session_id)
        wrote = False
        if os.path.isdir(review):
            for name in sorted(os.listdir(review)):
                if not (name.startswith("status.") and name.endswith(".json")):
                    continue
                with open(os.path.join(review, name)) as fh:
                    for entry in json.load(fh):
                        out.write("%s %s (%s, %d bytes)\n"
                                  % (entry["op"], entry["path"],
                                     entry.get("kind", "file"),
                                     entry.get("bytes", 0)))
                        wrote = True
        if not wrote:
            return self.status(session_id, out=out)
        return session

    # -- mutating ------------------------------------------------------------
    def commit(self, session_id):
        session = self._load(session_id)
        self._require_state(session, ("pending-review", "frozen"), "commit")
        for name, root in sorted(session.protected_roots.items()):
            self.backend.commit(root)
            session.add_event("committed-root", name)
        session.transition("committed")
        self.store.save(session)
        return session

    def abort(self, session_id):
        session = self._load(session_id)
        if session.state in TERMINAL_STATES:
            raise ControlError("session %s already %s"
                               % (session_id, session.state))
        for name, root in sorted(session.protected_roots.items()):
            self.backend.abort(root)
            session.add_event("aborted-root", name)
            try:
                self.backend.unmount(root)
            except Exception:
                pass
        session.transition("aborted")
        self.store.save(session)
        return session

    # -- selective / line-level review (post-session, branch unmounted) -----
    def _store_paths(self, root, change):
        """(rel, delta-file-in-store, base-file) for a change."""
        visible = self.alias_map.canonicalize(root.visible)
        rel = os.path.relpath(self.alias_map.canonicalize(change.path), visible)
        delta = os.path.join(root.store, "branches", root.branch, "files", rel)
        return rel, delta, os.path.join(root.base, rel)

    def _apply_change_from_store(self, root, change):
        """Apply one change to base by reading its delta from the store (the
        branch is not mounted post-session)."""
        _rel, delta, base = self._store_paths(root, change)
        if change.op == "D":
            if os.path.islink(base) or os.path.isfile(base):
                os.unlink(base)
            elif os.path.isdir(base):
                shutil.rmtree(base)
        elif change.kind == "dir":
            os.makedirs(base, exist_ok=True)
        elif os.path.exists(delta):
            parent = os.path.dirname(base)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(delta, base)

    def _changes(self, session):
        out = []
        for _name, root in sorted(session.protected_roots.items()):
            for change in self.backend.status(root):
                out.append((root, change))
        return out

    def _emit_patch(self, changes, out):
        """Unified base-vs-view diff the user can prune to a hunk subset, then
        re-apply with `review --apply-patch`."""
        for root, change in changes:
            rel, delta, base = self._store_paths(root, change)
            old = []
            if os.path.isfile(base):
                with open(base, "r", errors="replace") as fh:
                    old = fh.readlines()
            new = []
            if change.op != "D" and os.path.isfile(delta):
                with open(delta, "r", errors="replace") as fh:
                    new = fh.readlines()
            for line in difflib.unified_diff(old, new,
                                             fromfile="a/" + rel,
                                             tofile="b/" + rel):
                out.write(line if line.endswith("\n") else line + "\n")

    def review(self, session_id, accept=False, reject=False, commit_paths=None,
               emit_patch=False, apply_patch=None, out=None):
        """Post-session review of a pending/frozen session's change set.

        Default browses the diff.  ``accept`` commits everything, ``reject``
        discards everything, ``commit_paths`` commits a file-level subset (the
        rest are discarded), ``emit_patch`` prints a base-vs-view unified diff,
        and ``apply_patch`` applies a (possibly pruned) patch to base for
        line-level control.
        """
        out = out or sys.stdout
        if accept:
            return self.commit(session_id)
        if reject:
            return self.abort(session_id)
        session = self._load(session_id)
        self._require_state(session, ("pending-review", "frozen"), "review")
        changes = self._changes(session)

        if emit_patch:
            self._emit_patch(changes, out)
            return session

        if apply_patch:
            # patch the base directly (one base per root; the primary root is
            # the common case), then discard the now-stale branch deltas.
            base = sorted(session.protected_roots.values(),
                          key=lambda r: r.name)[0].base
            with open(apply_patch) as fh:
                proc = subprocess.run(["patch", "-p1", "-d", base],
                                      stdin=fh, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True)
            out.write(proc.stdout)
            if proc.returncode != 0:
                raise ControlError("patch failed (rc=%d)" % proc.returncode)
            return self._finish_selective(session, "patch applied")

        if commit_paths:
            chosen = set(commit_paths)
            applied = []
            for root, change in changes:
                if change.path in chosen:
                    self._apply_change_from_store(root, change)
                    applied.append(change.path)
            out.write("committed %d path(s); discarding the rest\n"
                      % len(applied))
            return self._finish_selective(session, "file-level commit")

        return self.diff(session_id, out=out)

    def _finish_selective(self, session, detail):
        """After a selective/patch apply to base, discard the branch deltas and
        mark the session committed."""
        for name, root in sorted(session.protected_roots.items()):
            try:
                self.backend.abort(root)
            except Exception:
                pass
            try:
                self.backend.unmount(root)
            except Exception:
                pass
            session.add_event("selective-commit", "%s: %s" % (name, detail))
        session.transition("committed")
        self.store.save(session)
        return session

    def thaw(self, session_id):
        """Intentionally reopen a pending-review session for more work."""
        session = self._load(session_id)
        self._require_state(session, ("pending-review",), "thaw")
        for root in session.protected_roots.values():
            self.backend.thaw(root)
        session.transition("running")
        session.add_event("thawed")
        self.store.save(session)
        return session

    def finish(self, session_id):
        """Finalize a long-running/manual session now (freeze+policy)."""
        session = self._load(session_id)
        self._require_state(session, ("running",), "finish")
        session.transition("finalizing")
        self.store.save(session)
        finalize_session(session, self.store, self.backend, self.alias_map)
        return self.store.load(session_id)

    def finish_turn(self, session_id):
        """Hook entrypoint: record turn completion; never commits."""
        session = self._load(session_id)
        session.add_event("turn-finished")
        self.store.save(session)
        return session

    def check_before_final(self, session_id, out=None):
        """Hook entrypoint (blocking Stop hooks): bounded self-repair check.

        Classifies *live* branch status against the session policy — no
        freeze, commit, or abort.  A dirty change set consumes one unit of
        the per-session repair budget and the offending paths are printed
        for the agent to revert; once the budget is spent the check stands
        aside and finalize parks the session for human review.  Policy
        *mode* (manual/read-only-review/...) is applied at finalize, not
        here: this check is only about scope and deny/hide hygiene.
        """
        out = out or sys.stdout
        session = self._load(session_id)
        self._require_state(session, ("running",), "check-before-final")

        config = PolicyConfig.from_dict(session.policy)
        changes = []
        for _name, root in sorted(session.protected_roots.items()):
            changes.extend(self.backend.status(root))
        out_of_scope, deny_matches = classify(changes, config, self.alias_map)

        if not out_of_scope and not deny_matches:
            session.add_event("check-clean",
                              "%d change(s), all in scope" % len(changes))
            self.store.save(session)
            out.write("clean: %d change(s), all within policy\n"
                      % len(changes))
            return CHECK_ALLOW

        if session.repair_attempts >= config.max_policy_repair_attempts:
            session.add_event(
                "repair-budget-exhausted",
                "%d/%d attempts used; deferring to review at finalize"
                % (session.repair_attempts,
                   config.max_policy_repair_attempts))
            self.store.save(session)
            out.write(
                "repair budget exhausted (%d attempt(s)); changes will be "
                "frozen for human review at finalize\n"
                % session.repair_attempts)
            return CHECK_EXHAUSTED

        session.repair_attempts += 1
        session.add_event(
            "repair-requested",
            "attempt %d/%d: %d out-of-scope, %d deny match(es)"
            % (session.repair_attempts, config.max_policy_repair_attempts,
               len(out_of_scope), len(deny_matches)))
        self.store.save(session)

        out.write("policy violations; revert these before finishing "
                  "(attempt %d/%d):\n"
                  % (session.repair_attempts,
                     config.max_policy_repair_attempts))
        for path in out_of_scope:
            out.write("  out-of-scope: %s\n" % path)
        for match in deny_matches:
            out.write("  deny-pattern %s: %s\n" % (match.pattern, match.path))
        out.write("undo the listed changes (restore original content, or "
                  "delete files you created), then finish again.\n")
        return CHECK_REPAIR
