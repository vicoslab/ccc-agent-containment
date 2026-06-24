"""Session orchestration for ccc-agent-run.

The trusted launcher flow (first milestone: process-exit completion)::

    create session -> create+mount branch bundle -> run agent ->
    freeze -> status -> policy -> auto-commit | pending-review | abort ->
    review artifacts -> unmount

Confinement is a property of *how* the agent is launched, not of the lifecycle:
``bwrap`` mode wraps the command in a rootless user+mount+pid namespace (the
real boundary); ``none`` mode just runs it with its cwd inside the view (debug
only -- not a boundary).  Either way the runner owns the lifecycle and the
commit decision, and the agent process never does.
"""

import binascii
import json
import os
import shutil
import subprocess

from . import artifacts
from .control import ControlServer
from .paths import is_within
from .policy import (ABORT, AUTO_COMMIT, NO_CHANGES, PENDING_REVIEW,
                     PolicyConfig, evaluate, filter_ignored)
from .session import ProtectedRoot
from .turn import TurnController

ENV_SESSION = "CCC_AGENT_SESSION"
ENV_STATE_DIR = "CCC_AGENT_STATE_DIR"
ENV_CONTROL_SOCK = "CCC_AGENT_CONTROL_SOCK"
ENV_CONTROL_TOKEN = "CCC_AGENT_CONTROL_TOKEN"

# Where the per-turn control socket is bind-mounted INSIDE the bwrap sandbox
# (the host-side socket lives under the state dir, outside the sandbox).
SANDBOX_CONTROL_SOCK = "/run/ccc-agent/control.sock"


class RootSpec(object):
    """Template for one protected root; branch/mount are filled per session."""

    __slots__ = ("name", "base", "store", "visible", "home_subdir", "mount",
                 "hide_paths")

    def __init__(self, name, base, store, visible, home_subdir=None,
                 mount=None, hide_paths=()):
        self.name = name
        self.base = base
        self.store = store
        self.visible = visible
        self.home_subdir = home_subdir
        self.mount = mount  # default: <state>/mounts/<session>/<name>
        self.hide_paths = list(hide_paths)

    def materialize(self, session_id, state_dir):
        mount = self.mount or os.path.join(state_dir, "mounts", session_id,
                                           self.name)
        return ProtectedRoot(name=self.name, base=self.base, store=self.store,
                             branch=session_id, mount=mount,
                             visible=self.visible,
                             home_subdir=self.home_subdir,
                             hide_paths=self.hide_paths)


# "bwrap" is the real containment boundary (rootless user+mount+pid namespace).
# "none" runs the agent with only its cwd inside the view and is NOT a security
# boundary -- absolute-path writes bypass the view entirely; keep it for
# debugging the policy/commit pipeline without bwrap, never for confinement.
CONFINEMENT_MODES = ("none", "bwrap")
BWRAP_PROC_MODES = ("bind", "ro", "fresh")


class RunnerConfig(object):
    def __init__(self, store, backend, alias_map, owner, agent_kind,
                 agent_command, workspace, policy, roots,
                 completion="process-exit", confinement="none",
                 bwrap_bin="bwrap", bwrap_proc_mode="bind",
                 bwrap_ro_binds=(), bwrap_setenv=None, per_turn=None,
                 cred_mounts=(), cred_mask=(), cred_env=None,
                 bwrap_uid=None, bwrap_gid=None):
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
        if confinement not in CONFINEMENT_MODES:
            raise ValueError("unknown confinement %r (expected one of %s)"
                             % (confinement, ", ".join(CONFINEMENT_MODES)))
        self.confinement = confinement
        self.bwrap_bin = bwrap_bin
        if bwrap_proc_mode not in BWRAP_PROC_MODES:
            raise ValueError("unknown bwrap_proc_mode %r (expected one of %s)"
                             % (bwrap_proc_mode, ", ".join(BWRAP_PROC_MODES)))
        self.bwrap_proc_mode = bwrap_proc_mode
        # Extra read-only paths to re-expose inside the sandbox AFTER the view
        # binds (so the agent's own runtime + creds, which live under the real
        # $HOME/storage the view hides, become reachable again).  setenv passes
        # config/API-key env into the otherwise --clearenv'd sandbox.
        self.bwrap_ro_binds = list(bwrap_ro_binds)
        self.bwrap_setenv = dict(bwrap_setenv or {})
        # bwrap needs no extra container privilege and no uid/gid: it mints
        # namespace-scoped CAP_SYS_ADMIN from an unprivileged user namespace
        # and runs the agent as the same host uid (mapped to 0 inside).
        # Per-turn (Stop-boundary) commit via the control socket; defaults on
        # for bwrap (the interactive case) and off otherwise.  `none` can opt in
        # for debugging (the hook reaches the host socket directly).
        self.per_turn = (confinement == "bwrap") if per_turn is None else per_turn
        # Credential handling: cred_mounts = config/state dirs to re-expose
        # read-only from the real home; cred_mask = secret files to hide
        # (overmounted with /dev/null); cred_env = {ENV_VAR: {file, json_key}}
        # the supervisor reads on the host and passes to the agent as env, so
        # the auth file never enters the sandbox.
        self.cred_mounts = list(cred_mounts)
        self.cred_mask = list(cred_mask)
        self.cred_env = dict(cred_env or {})
        self.bwrap_uid = bwrap_uid
        self.bwrap_gid = bwrap_gid


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


def _primary_root(session, alias_map):
    """The protected root whose visible path contains the workspace."""
    workspace = alias_map.canonicalize(session.workspace)
    for root in session.protected_roots.values():
        if is_within(workspace, alias_map.canonicalize(root.visible)):
            return root
    raise ValueError("workspace %s is not under any protected root"
                     % session.workspace)


# System paths exposed read-only inside the bwrap sandbox.  The agent sees the
# OS read-only and its BranchFS view read-write, and nothing else (no real
# underlay, no other /storage mounts, no daemon/docker sockets).  On merged-/usr
# systems /bin,/sbin,/lib,/lib64 are symlinks into /usr and must be recreated as
# symlinks, not bound as dirs.
BWRAP_RO_DIRS = ("/usr", "/etc", "/opt")
BWRAP_USRMERGE_DIRS = ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")


def _optional_ro_bind(entry):
    """Return a safe optional read-only bind triple, or None to skip it.

    Entries are ``src`` or ``src:dest``.  Optional agent-runtime/config binds
    often name paths under ``/home/<user>`` which CCC may implement as symlinks
    into ``/storage/user``.  If bwrap is asked to mount on the symlink itself
    *after* the BranchFS view is already bound over /home or /storage, the
    destination symlink is resolved inside /newroot and can point at a path that
    does not exist there.  Resolve symlinked optional binds on the trusted host
    first and bind the real target path read-only instead.

    Missing optional paths are skipped here instead of passed through as
    ``--ro-bind-try``.  In particular, a broken symlink has no valid target and
    should not be added to the sandbox command at all.
    """
    src, dest = entry.split(":", 1) if ":" in entry else (entry, entry)
    explicit_dest = ":" in entry

    resolved_src = os.path.realpath(src)
    if not os.path.exists(resolved_src):
        return None

    if explicit_dest:
        resolved_dest = os.path.realpath(dest)
        # If the requested destination itself traverses a symlink, bind onto
        # that target instead.  If the target is absent, the symlink is broken
        # for our purposes and the optional bind should be skipped.
        if os.path.normpath(resolved_dest) != os.path.normpath(dest):
            if not os.path.exists(resolved_dest):
                return None
            dest = resolved_dest
    else:
        dest = resolved_src

    return (resolved_src, dest)


def _bwrap_command(session, config, control=None):
    """Build a bubblewrap command that confines the agent rootlessly.

    This needs no container CAP_SYS_ADMIN and no privileged helper: bwrap
    creates an unprivileged user+mount+pid namespace,
    recursively binds the OS read-only, overlays the BranchFS view read-write
    at its visible path (hiding the real underlay), and execs the agent as the
    same host uid mapped to 0.  No network/proc isolation is enforced (per
    design); /proc is bound from the container by default.
    """
    alias_map = config.alias_map
    primary = _primary_root(session, alias_map)
    workdir = alias_map.canonicalize(session.workspace)
    home = "/home/%s" % config.owner

    # Map to the REAL uid inside the namespace (not 0): the view files are owned
    # by this uid, and some agents (claude) refuse to run as root.  Overridable
    # via bwrap_uid/bwrap_gid for agents that genuinely want in-sandbox root.
    uid = str(config.bwrap_uid if config.bwrap_uid is not None else os.getuid())
    gid = str(config.bwrap_gid if config.bwrap_gid is not None else os.getgid())
    argv = [config.bwrap_bin,
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--uid", uid, "--gid", gid,
            "--die-with-parent", "--clearenv"]

    for d in BWRAP_RO_DIRS:
        if os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    for d in BWRAP_USRMERGE_DIRS:
        if os.path.islink(d):
            argv += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            argv += ["--ro-bind", d, d]

    # /proc: fresh mount fails under Docker's locked proc masks unless the
    # deployment unmasks them (systempaths=unconfined); default to binding the
    # container's /proc, which always works and is benign in a single-user box.
    if config.bwrap_proc_mode == "fresh":
        argv += ["--proc", "/proc"]
    elif config.bwrap_proc_mode == "ro":
        argv += ["--ro-bind", "/proc", "/proc"]
    else:
        argv += ["--bind", "/proc", "/proc"]
    argv += ["--dev", "/dev", "--tmpfs", "/tmp"]

    # the BranchFS view, read-write, at its visible path and at $HOME; the
    # --bind overlays (and thus hides) the real underlay at the same path.
    argv += ["--bind", primary.mount, alias_map.canonicalize(primary.visible)]
    if primary.home_subdir:
        argv += ["--bind", os.path.join(primary.mount, primary.home_subdir),
                 home]
    else:
        argv += ["--bind", primary.mount, home]
    for name, root in sorted(session.protected_roots.items()):
        if root is primary:
            continue
        argv += ["--bind", root.mount, alias_map.canonicalize(root.visible)]

    # Re-expose the agent runtime + creds read-only.  Each entry is "src" (bind
    # at the same path) or "src:dest" (bind src at dest).  IMPORTANT: a dest
    # UNDER a view (/storage/user, /home/<user>) makes bwrap mkdir mountpoints
    # INTO the FUSE view — creating spurious dir-deltas and churning inodes
    # (ESTALE) — so runtime that the agent doesn't need at a fixed in-view path
    # should bind to a dest outside the views (e.g. /opt/ccc-agent, /ccc-agent).
    # Optional symlinked same-path binds (e.g. ~/.claude -> /storage/user/...)
    # are resolved on the host and bound at their real target path, because the
    # destination symlink may point somewhere different or missing once /home and
    # /storage have been overlaid with BranchFS views inside bwrap.
    for entry in config.bwrap_ro_binds:
        bind = _optional_ro_bind(entry)
        if bind is not None:
            src, dest = bind
            argv += ["--ro-bind", src, dest]

    # Credentials: re-expose the agent's config/state dirs read-only from the
    # real home, then MASK the secret files (overmount /dev/null).  The real
    # credential is passed via env below — the auth file never enters the box.
    for src in config.cred_mounts:
        bind = _optional_ro_bind(src)
        if bind is not None:
            argv += ["--ro-bind", bind[0], bind[1]]
    for masked in config.cred_mask:
        if os.path.exists(masked):  # only mask a secret that's actually present
            argv += ["--ro-bind", "/dev/null", masked]

    # Per-turn control socket: bind the host socket to a fixed in-sandbox path
    # so the Stop hook can signal the supervisor.  `control` is (host_sock,
    # token) or None.
    if control is not None:
        host_sock, token = control
        argv += ["--bind", host_sock, SANDBOX_CONTROL_SOCK]

    argv += ["--setenv", ENV_SESSION, session.session_id,
             "--setenv", "HOME", home,
             "--setenv", "USER", config.owner,
             "--setenv", "LOGNAME", config.owner,
             "--setenv", "PATH",
             "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "--setenv", "TERM", os.environ.get("TERM", "xterm")]
    if control is not None:
        argv += ["--setenv", ENV_CONTROL_SOCK, SANDBOX_CONTROL_SOCK,
                 "--setenv", ENV_CONTROL_TOKEN, control[1]]
    # Credentials via env (read from the host auth files; never bound in).
    for var, spec in sorted(config.cred_env.items()):
        value = _extract_cred(spec)
        if value:
            argv += ["--setenv", var, value]
    for key, value in sorted(config.bwrap_setenv.items()):
        argv += ["--setenv", key, str(value)]
    argv += ["--chdir", workdir, "--"]
    argv += config.agent_command
    return argv


def _extract_cred(spec):
    """Resolve a credential for env passing.  ``spec`` is either a literal
    string, or {"env": NAME} (pass through from the supervisor env), or
    {"file": path, "json_key": "a.b.c"} (read a dotted key from a JSON auth
    file on the host).  Returns the value, or None if unavailable."""
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return None
    if spec.get("env"):
        return os.environ.get(spec["env"])
    path = spec.get("file")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return None
    for part in str(spec.get("json_key", "")).split("."):
        if not part:
            continue
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data if isinstance(data, str) else None


def _fail(store, session, detail):
    session.add_event("error", detail)
    if session.state != "failed":
        session.transition("failed")
    store.save(session)


def collect_status(session, backend):
    return {name: backend.status(root)
            for name, root in sorted(session.protected_roots.items())}


def apply_change_from_store(root, change, alias_map):
    """Apply one reviewed change to the base by reading its delta from the
    BranchFS store (used at session end, when the branch is unmounted).  This
    is *selective*: only the changes we pass get applied, so ignored noise and
    out-of-scope deltas left in the branch are never written to base — unlike
    branchfs ``commit-branch`` which would apply the whole branch."""
    visible = alias_map.canonicalize(root.visible)
    rel = os.path.relpath(alias_map.canonicalize(change.path), visible)
    delta = os.path.join(root.store, "branches", root.branch, "files", rel)
    base = os.path.join(root.base, rel)
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


def finalize_session(session, store, backend, alias_map):
    """freeze -> status -> policy -> artifacts -> apply decision.

    Expects the session in state ``finalizing``.  Returns the decision.
    """
    for root in session.protected_roots.values():
        backend.freeze(root)
    session.add_event("frozen-bundle")
    session.transition("frozen")
    store.save(session)

    policy_config = PolicyConfig.from_dict(session.policy)
    changes_by_root = {name: filter_ignored(changes, policy_config, alias_map)
                       for name, changes in collect_status(session,
                                                           backend).items()}
    flat_changes = [c for changes in changes_by_root.values()
                    for c in changes]
    decision = evaluate(flat_changes, policy_config, alias_map)
    review = artifacts.write_review(store, session, changes_by_root, decision)
    session.add_event("review-artifacts", review)

    # Apply the decision against unmounted branches.  The real branchfs binary
    # cannot commit/abort a branch whose store is still busy with a live mount
    # (commit-branch fails with ENOTEMPTY), and a pending-review branch is
    # inspected later through the store, not this mount.  Unmount here so every
    # terminal path operates on a quiescent branch; run_session's finally is a
    # harmless idempotent backstop.
    _unmount_all(session, backend)
    session.add_event("unmounted-bundle")

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
        # Selectively apply only the reviewed in-scope changes (the same set
        # used for the decision), then discard the branch.  This avoids
        # branchfs commit-branch applying the *whole* branch — which would
        # commit ignored config-dir churn and choke (ENOTEMPTY) on stale .nfs
        # deltas the agent left in non-workspace areas.
        try:
            for name, root in sorted(session.protected_roots.items()):
                for change in changes_by_root.get(name, ()):
                    apply_change_from_store(root, change, alias_map)
                backend.abort(root)  # discard the branch + any unreviewed noise
                session.add_event("committed-root", name)
        except Exception as exc:  # failure must never lose the branch
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
    # Re-exposed credential dirs sit under the home view; the mountpoints bwrap
    # creates would otherwise show as spurious out-of-scope deltas, so ignore
    # their canonical paths in classification.
    if config.cred_mounts:
        ignore = session.policy.setdefault("ignore_patterns", [])
        for src in config.cred_mounts:
            canonical = config.alias_map.canonicalize(src)
            if canonical not in ignore:
                ignore.append(canonical)
    config.store.save(session)

    control_server = None

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

        # Per-turn control channel: start the supervisor-side server (outside
        # the sandbox) BEFORE launching the agent, so the socket exists for the
        # bwrap bind and the Stop hook can signal it.  The host socket path +
        # token go into the agent env (bwrap remaps the path to the in-sandbox
        # mount); finalize at process exit still runs as the session-end pass.
        control = None
        if config.per_turn:
            token = binascii.hexlify(os.urandom(16)).decode("ascii")
            host_sock = os.path.join(config.store.state_dir, "control",
                                     session.session_id + ".sock")
            turn_ctl = TurnController(session, config.store, config.backend,
                                      config.alias_map)
            control_server = ControlServer(host_sock, turn_ctl.handle, token)
            control_server.start()
            session.add_event("control-server", host_sock)
            run_env[ENV_CONTROL_SOCK] = host_sock
            run_env[ENV_CONTROL_TOKEN] = token
            control = (host_sock, token)

        session.transition("running")
        config.store.save(session)
        if config.confinement == "bwrap":
            # bwrap assembles the namespace itself and --chdir's into the
            # workspace inside the sandbox, so no host-side cwd is set here.
            argv = _bwrap_command(session, config, control=control)
            session.add_event("bwrap-launch", argv[0])
            proc = subprocess.run(argv, env=run_env)
        else:
            proc = subprocess.run(config.agent_command, cwd=cwd, env=run_env)
        session.exit_status = proc.returncode
        session.add_event("agent-exit", str(proc.returncode))
    except Exception as exc:
        _fail(config.store, session, "agent launch failed: %s" % exc)
        if control_server is not None:
            control_server.stop()
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
        if control_server is not None:
            control_server.stop()
        _unmount_all(session, config.backend)

    return session
