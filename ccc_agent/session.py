"""Branch session schema, state machine, and durable JSON store.

A *session* is the human-facing review unit for one agent run.  It may span
several BranchFS branches (one per protected root) — that set is the session's
*branch bundle*.  Session metadata is stored as JSON (not YAML) so the trusted
supervisor stays stdlib-only; the schema mirrors the accepted design doc.
"""

import json
import os
import time
import uuid

STATES = (
    "created",
    "mounting",
    "running",
    "finalizing",
    "frozen",
    "auto-committed",
    "pending-review",
    "committed",
    "aborted",
    "failed",
)

TERMINAL_STATES = ("auto-committed", "committed", "aborted", "failed")

_TRANSITIONS = {
    "created": ("mounting", "aborted", "failed"),
    "mounting": ("running", "aborted", "failed"),
    "running": ("finalizing", "aborted", "failed"),
    "finalizing": ("frozen", "aborted", "failed"),
    "frozen": ("auto-committed", "pending-review", "committed", "aborted",
               "failed"),
    # thaw: a human may intentionally reopen a pending session for more work
    "pending-review": ("committed", "aborted", "running", "failed"),
    "auto-committed": (),
    "committed": (),
    "aborted": (),
    "failed": (),
}


class StateError(Exception):
    """Raised on an illegal session state transition."""


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_session_id():
    # Path-safe (no ':' or '/'): the id becomes a directory name and a
    # BranchFS branch name component.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return "agent-%s-%s" % (stamp, uuid.uuid4().hex[:8])


class ProtectedRoot(object):
    """One protected writable CCC root covered by a BranchFS branch."""

    __slots__ = ("name", "base", "store", "branch", "mount", "visible",
                 "home_subdir")

    def __init__(self, name, base, store, branch, mount, visible,
                 home_subdir=None):
        self.name = name          # e.g. "storage_user"
        self.base = base          # real underlay, e.g. /__real/storage_user
        self.store = store        # BranchFS delta/metadata store
        self.branch = branch      # branch name for this session
        self.mount = mount        # BranchFS mountpoint (trusted-side)
        self.visible = visible    # agent-visible path, e.g. /storage/user
        self.home_subdir = home_subdir  # set on the root that backs /home

    def to_dict(self):
        data = {"name": self.name, "base": self.base, "store": self.store,
                "branch": self.branch, "mount": self.mount,
                "visible": self.visible}
        if self.home_subdir is not None:
            data["home_subdir"] = self.home_subdir
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(name=data["name"], base=data["base"], store=data["store"],
                   branch=data["branch"], mount=data["mount"],
                   visible=data["visible"],
                   home_subdir=data.get("home_subdir"))


class Session(object):
    def __init__(self, session_id, owner, agent_kind, agent_command,
                 workspace, policy, protected_roots, state="created",
                 created_at=None, finished_at=None, exit_status=None,
                 completion="process-exit", events=None, repair_attempts=0):
        self.session_id = session_id
        self.owner = owner
        self.agent_kind = agent_kind
        self.agent_command = list(agent_command)
        self.workspace = workspace
        self.policy = dict(policy)
        self.protected_roots = dict(protected_roots)
        self.state = state
        self.created_at = created_at or utc_now()
        self.finished_at = finished_at
        self.exit_status = exit_status
        self.completion = completion
        self.events = list(events or [])
        self.repair_attempts = repair_attempts

    def transition(self, new_state):
        if new_state not in STATES:
            raise StateError("unknown state %r" % (new_state,))
        allowed = _TRANSITIONS[self.state]
        if new_state not in allowed:
            raise StateError("illegal transition %s -> %s"
                             % (self.state, new_state))
        self.state = new_state
        if new_state in TERMINAL_STATES and not self.finished_at:
            self.finished_at = utc_now()
        self.add_event("state:%s" % new_state)

    def add_event(self, event, detail=None):
        entry = {"time": utc_now(), "event": event}
        if detail is not None:
            entry["detail"] = detail
        self.events.append(entry)

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "agent_kind": self.agent_kind,
            "agent_command": self.agent_command,
            "workspace": self.workspace,
            "policy": self.policy,
            "protected_roots": {name: root.to_dict()
                                for name, root in self.protected_roots.items()},
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "exit_status": self.exit_status,
            "completion": self.completion,
            "events": self.events,
            "repair_attempts": self.repair_attempts,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            session_id=data["session_id"],
            owner=data["owner"],
            agent_kind=data["agent_kind"],
            agent_command=data["agent_command"],
            workspace=data["workspace"],
            policy=data["policy"],
            protected_roots={
                name: ProtectedRoot.from_dict(root)
                for name, root in data["protected_roots"].items()},
            state=data["state"],
            created_at=data.get("created_at"),
            finished_at=data.get("finished_at"),
            exit_status=data.get("exit_status"),
            completion=data.get("completion", "process-exit"),
            events=data.get("events"),
            repair_attempts=data.get("repair_attempts", 0),
        )


class SessionStore(object):
    """Durable session registry under a state directory.

    Layout::

        <state_dir>/sessions/<session-id>/session.json
        <state_dir>/reviews/<session-id>/...

    The state dir should live on shared storage that the *agent cannot see*
    from inside its chroot (it is covered by the default ``.ccc-agent`` deny
    pattern as defense in depth if it ever is visible).
    """

    def __init__(self, state_dir):
        self.state_dir = os.path.abspath(state_dir)
        self.sessions_dir = os.path.join(self.state_dir, "sessions")
        self.reviews_dir = os.path.join(self.state_dir, "reviews")

    # -- paths ------------------------------------------------------------
    def session_dir(self, session_id):
        return os.path.join(self.sessions_dir, session_id)

    def session_file(self, session_id):
        return os.path.join(self.session_dir(session_id), "session.json")

    def review_dir(self, session_id):
        return os.path.join(self.reviews_dir, session_id)

    # -- lifecycle ---------------------------------------------------------
    def create(self, owner, agent_kind, agent_command, workspace, policy,
               protected_roots, completion="process-exit", session_id=None):
        session = Session(
            session_id=session_id or new_session_id(),
            owner=owner,
            agent_kind=agent_kind,
            agent_command=agent_command,
            workspace=workspace,
            policy=policy,
            protected_roots=protected_roots,
            completion=completion,
        )
        session.add_event("created")
        self.save(session)
        return session

    def save(self, session):
        directory = self.session_dir(session.session_id)
        os.makedirs(directory, exist_ok=True)
        path = self.session_file(session.session_id)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(session.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def load(self, session_id):
        path = self.session_file(session_id)
        if not os.path.isfile(path):
            raise KeyError("no such session: %s" % (session_id,))
        with open(path) as fh:
            return Session.from_dict(json.load(fh))

    def list(self):
        if not os.path.isdir(self.sessions_dir):
            return []
        sessions = []
        for name in sorted(os.listdir(self.sessions_dir)):
            try:
                sessions.append(self.load(name))
            except (KeyError, ValueError):
                continue  # skip corrupt/foreign entries, never crash listing
        return sessions
