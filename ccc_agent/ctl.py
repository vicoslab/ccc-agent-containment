"""Operator/hook control surface over branch sessions (ccc-agentctl).

Hooks are *reporters*: ``finish-turn`` only records lifecycle events, and
``check-before-final`` only reads live status to drive bounded self-repair.
Commit authority stays here, in trusted supervisor code, behind explicit
operator commands (or the runner's policy decision).
"""

import json
import os
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
