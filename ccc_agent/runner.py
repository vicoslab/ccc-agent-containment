"""Session orchestration for ccc-agent-run.

The trusted launcher flow (first milestone: process-exit completion)::

    create session -> create+mount branch bundle -> run agent ->
    freeze -> status -> policy -> auto-commit | pending-review | abort ->
    review artifacts -> unmount

The runner is deliberately ignorant of *how* the agent is confined: in chroot
deployments the agent command is already wrapped by the privileged chroot
assembler; in dev/no-chroot mode the agent simply runs with its cwd inside
the mounted branch view.  Either way the runner owns the lifecycle and the
commit decision, and the agent process never does.
"""

import os
import subprocess

from . import artifacts
from .paths import is_within
from .policy import (ABORT, AUTO_COMMIT, NO_CHANGES, PENDING_REVIEW,
                     PolicyConfig, evaluate)
from .session import ProtectedRoot

ENV_SESSION = "CCC_AGENT_SESSION"
ENV_STATE_DIR = "CCC_AGENT_STATE_DIR"


class RootSpec(object):
    """Template for one protected root; branch/mount are filled per session."""

    __slots__ = ("name", "base", "store", "visible", "home_subdir", "mount")

    def __init__(self, name, base, store, visible, home_subdir=None,
                 mount=None):
        self.name = name
        self.base = base
        self.store = store
        self.visible = visible
        self.home_subdir = home_subdir
        self.mount = mount  # default: <state>/mounts/<session>/<name>

    def materialize(self, session_id, state_dir):
        mount = self.mount or os.path.join(state_dir, "mounts", session_id,
                                           self.name)
        return ProtectedRoot(name=self.name, base=self.base, store=self.store,
                             branch=session_id, mount=mount,
                             visible=self.visible,
                             home_subdir=self.home_subdir)


class RunnerConfig(object):
    def __init__(self, store, backend, alias_map, owner, agent_kind,
                 agent_command, workspace, policy, roots,
                 completion="process-exit"):
        self.store = store              # SessionStore
        self.backend = backend          # BranchfsCli or FakeBranchFS
        self.alias_map = alias_map
        self.owner = owner
        self.agent_kind = agent_kind
        self.agent_command = list(agent_command)
        self.workspace = workspace
        self.policy = dict(policy)
        if "allowed_scopes" not in self.policy:
            self.policy["allowed_scopes"] = [workspace]
        PolicyConfig.from_dict(self.policy)  # validate early
        self.roots = list(roots)
        self.completion = completion


def _agent_cwd(session, alias_map):
    """Map the visible workspace path into the mounted branch view."""
    workspace = alias_map.canonicalize(session.workspace)
    for root in session.protected_roots.values():
        visible = alias_map.canonicalize(root.visible)
        if is_within(workspace, visible):
            rel = os.path.relpath(workspace, visible)
            return (root.mount if rel == "." else
                    os.path.join(root.mount, rel))
    raise ValueError("workspace %s is not under any protected root"
                     % session.workspace)


def _fail(store, session, detail):
    session.add_event("error", detail)
    if session.state != "failed":
        session.transition("failed")
    store.save(session)


def collect_status(session, backend):
    return {name: backend.status(root)
            for name, root in sorted(session.protected_roots.items())}


def finalize_session(session, store, backend, alias_map):
    """freeze -> status -> policy -> artifacts -> apply decision.

    Expects the session in state ``finalizing``.  Returns the decision.
    """
    for root in session.protected_roots.values():
        backend.freeze(root)
    session.add_event("frozen-bundle")
    session.transition("frozen")
    store.save(session)

    changes_by_root = collect_status(session, backend)
    flat_changes = [c for changes in changes_by_root.values()
                    for c in changes]
    decision = evaluate(flat_changes, PolicyConfig.from_dict(session.policy),
                        alias_map)
    review = artifacts.write_review(store, session, changes_by_root, decision)
    session.add_event("review-artifacts", review)

    if decision.decision == NO_CHANGES:
        for root in session.protected_roots.values():
            backend.abort(root)
        session.add_event("closed", "no changes (no-op); branch aborted")
        session.transition("aborted")
    elif decision.decision == ABORT:
        for root in session.protected_roots.values():
            backend.abort(root)
        session.add_event("closed", "throwaway policy; branch aborted")
        session.transition("aborted")
    elif decision.decision == AUTO_COMMIT:
        try:
            for name, root in sorted(session.protected_roots.items()):
                backend.commit(root)
                session.add_event("committed-root", name)
        except Exception as exc:  # commit failure must never lose the branch
            _fail(store, session,
                  "commit failed, branch preserved for manual recovery: %s"
                  % exc)
            return decision
        session.transition("auto-committed")
    else:  # PENDING_REVIEW: branches stay frozen for human review
        session.add_event("pending", "; ".join(decision.reasons))
        session.transition("pending-review")

    store.save(session)
    return decision


def _unmount_all(session, backend):
    for root in session.protected_roots.values():
        try:
            backend.unmount(root)
        except Exception:
            pass  # unmount is cleanup; never mask the session outcome


def run_session(config, env=None, before_finalize=None):
    """Run one contained agent session to its final state.

    ``env`` defaults to ``os.environ``.  If ``CCC_AGENT_SESSION`` is already
    set, the invocation is nested inside an existing session: reuse it and
    run the command without creating a new branch bundle.
    """
    env = dict(os.environ if env is None else env)

    nested_id = env.get(ENV_SESSION)
    if nested_id:
        try:
            session = config.store.load(nested_id)
        except KeyError:
            session = None
        if session is not None:
            session.add_event("nested-run", " ".join(config.agent_command))
            config.store.save(session)
            subprocess.call(config.agent_command, env=env)
            return session

    session = config.store.create(
        owner=config.owner,
        agent_kind=config.agent_kind,
        agent_command=config.agent_command,
        workspace=config.workspace,
        policy=config.policy,
        protected_roots={},  # filled below, once the session id exists
        completion=config.completion,
    )
    session.protected_roots = {
        spec.name: spec.materialize(session.session_id,
                                    config.store.state_dir)
        for spec in config.roots
    }
    config.store.save(session)

    try:
        session.transition("mounting")
        config.store.save(session)
        for root in session.protected_roots.values():
            config.backend.start_daemon(root)
            config.backend.create_branch(root)
            config.backend.mount(root, agent=True)
        session.add_event("mounted-bundle")
    except Exception as exc:
        _fail(config.store, session, "mount failed: %s" % exc)
        _unmount_all(session, config.backend)
        return session

    try:
        cwd = _agent_cwd(session, config.alias_map)
        os.makedirs(cwd, exist_ok=True)
        run_env = dict(env)
        run_env[ENV_SESSION] = session.session_id
        run_env[ENV_STATE_DIR] = config.store.state_dir

        session.transition("running")
        config.store.save(session)
        proc = subprocess.run(config.agent_command, cwd=cwd, env=run_env)
        session.exit_status = proc.returncode
        session.add_event("agent-exit", str(proc.returncode))
    except Exception as exc:
        _fail(config.store, session, "agent launch failed: %s" % exc)
        _unmount_all(session, config.backend)
        return session

    try:
        session.transition("finalizing")
        config.store.save(session)
        if before_finalize is not None:
            before_finalize(session)
        finalize_session(session, config.store, config.backend,
                         config.alias_map)
    except Exception as exc:
        _fail(config.store, session, "finalize failed: %s" % exc)
    finally:
        _unmount_all(session, config.backend)

    return session
