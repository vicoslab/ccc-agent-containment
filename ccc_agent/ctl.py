"""Operator/hook control surface over branch sessions (ccc-agentctl).

Hooks are *reporters*: ``finish-turn`` only records lifecycle events.  Commit
authority stays here, in trusted supervisor code, behind explicit operator
commands (or the runner's policy decision).
"""

import json
import os
import sys

from .runner import finalize_session
from .session import TERMINAL_STATES


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
