"""Unified command-line entrypoint: ``ccc-agent OP ...``.

Runtime configuration comes from a JSON file (stdlib-only trusted layer):

    {
      "state_dir": "/storage/user/.ccc-agent",
      "backend": "branchfs",            // or "fake" for dry-run/demo
      "branchfs_bin": "branchfs",
      "user": "domen",
      "home_subdir": "",
      "roots": [
        {"name": "storage_user",
         "base": "/__real/storage_user",
         "store": "/__branchfs_store/storage_user",
         "visible": "/storage/user",
         "home_subdir": ""}
      ]
    }

Search order: --config flag, $CCC_AGENT_CONFIG, /etc/ccc-agent/config.json,
/opt/ccc-agent/config/config.json.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
from importlib import resources

from .branchfs import BranchfsCli, FakeBranchFS
from .control import (ControlClient, VERDICT_COMMITTED, VERDICT_HELD,
                      VERDICT_NEEDS_APPROVAL)
from .control import ControlError as ChannelError
from .ctl import CHECK_REPAIR, Controller, ControlError
from .paths import AliasMap
from .runner import (ENV_CONTROL_SOCK, ENV_CONTROL_TOKEN, RootSpec,
                     RunnerConfig, run_session)
from .session import SessionStore

CONFIG_ENV = "CCC_AGENT_CONFIG"
CONFIG_PATHS = ("/etc/ccc-agent/config.json",
                "/opt/ccc-agent/config/config.json")


def load_config(path=None, env=None):
    env = os.environ if env is None else env
    candidates = []
    if path:
        candidates.append(path)
    if env.get(CONFIG_ENV):
        candidates.append(env[CONFIG_ENV])
    candidates.extend(CONFIG_PATHS)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate) as fh:
                config = json.load(fh)
            config.setdefault("_source", candidate)
            return config
    raise SystemExit(
        "ccc-agent: no config found (tried: %s). Provide --config or set %s."
        % (", ".join(c for c in candidates if c), CONFIG_ENV))


def build_runtime(config):
    state_dir = config.get("state_dir") or os.path.join(
        os.path.expanduser("~"), ".ccc-agent")
    store = SessionStore(state_dir)
    if config.get("backend", "branchfs") == "fake":
        backend = FakeBranchFS()
    else:
        backend = BranchfsCli(binary=config.get("branchfs_bin", "branchfs"))
    user = config.get("user") or getpass.getuser()
    alias_map = AliasMap.for_home(user,
                                  home_subdir=config.get("home_subdir", ""))
    roots = [RootSpec(name=r["name"], base=r["base"], store=r["store"],
                      visible=r["visible"],
                      home_subdir=r.get("home_subdir"),
                      mount=r.get("mount"),
                      hide_paths=r.get("hide_paths", ()))
             for r in config.get("roots", ())]
    if not roots:
        raise SystemExit("ccc-agent: config defines no protected roots")
    return store, backend, alias_map, user, roots


def main_run(argv=None, env=None, prog="ccc-agent run"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run a command inside a contained BranchFS agent session.")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--workspace",
                        help="agent workspace (default: current directory)")
    parser.add_argument("--policy", default="workspace-auto",
                        help="policy mode (default: workspace-auto)")
    parser.add_argument("--scope", action="append", default=[],
                        help="additional allowed scope (repeatable)")
    parser.add_argument("--hide", action="append", default=[],
                        help="hide/deny pattern for sensitive paths "
                             "(repeatable)")
    parser.add_argument("--agent", default="command",
                        help="agent kind label, e.g. codex, claude, hermes")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the full session event log (always shows "
                             "the error detail on failure)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- command to run")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no agent command given (use: %s ... -- cmd)" % prog)

    config = load_config(args.config, env=env)
    store, backend, alias_map, user, roots = build_runtime(config)
    workspace = args.workspace or config.get("workspace") or os.getcwd()
    config_policy = config.get("policy", {})
    policy = {
        "mode": config_policy.get("mode", args.policy),
        "allowed_scopes": ([workspace] + list(args.scope)
                           + list(config_policy.get("allowed_scopes", ()))),
        "hide_patterns": (list(args.hide) + list(config.get("hide_patterns", ()))
                          + list(config_policy.get("hide_patterns", ()))),
        "ignore_patterns": list(config_policy.get("ignore_patterns", ())),
        "max_policy_repair_attempts":
            config_policy.get("max_policy_repair_attempts", 2),
    }
    if config_policy.get("deny_patterns") is not None:
        policy["deny_patterns"] = config_policy["deny_patterns"]
    # bwrap is the real containment boundary and the deployment default; "none"
    # is a debug mode only (no isolation -- absolute-path writes bypass the
    # view), so warn loudly if it is selected.
    confinement = config.get("confinement", "bwrap")
    if confinement == "none":
        sys.stderr.write(
            "ccc-agent: WARNING confinement=none is NOT a security boundary "
            "(debug only); the agent can write outside the view. Set "
            "confinement=bwrap for real containment.\n")
    runner_config = RunnerConfig(
        store=store, backend=backend, alias_map=alias_map, owner=user,
        agent_kind=args.agent, agent_command=command, workspace=workspace,
        policy=policy, roots=roots,
        confinement=confinement,
        bwrap_bin=config.get("bwrap_bin", "bwrap"),
        bwrap_proc_mode=config.get("bwrap_proc_mode", "bind"),
        bwrap_ro_binds=config.get("bwrap_ro_binds", ()),
        bwrap_setenv=config.get("bwrap_setenv"),
        cred_mounts=config.get("cred_mounts", ()),
        cred_mask=config.get("cred_mask", ()),
        cred_env=config.get("cred_env"),
        bwrap_uid=config.get("bwrap_uid"),
        bwrap_gid=config.get("bwrap_gid"),
        agent_plugins=({} if config.get("agent_hook_mode") == "disabled"
                       else config.get("agent_plugins")))
    session = run_session(runner_config, env=env)

    sys.stderr.write("ccc-agent: session %s finished: %s\n"
                     % (session.session_id, session.state))

    # Surface WHY it failed: the failure paths in run_session record the reason
    # as an "error" event (mount/launch/finalize/commit detail, incl. branchfs
    # stderr). Always print those on failure; --verbose dumps the full timeline.
    errors = [e for e in session.events if e.get("event") == "error"]
    if session.state == "failed":
        if errors:
            for e in errors:
                sys.stderr.write("ccc-agent: error: %s\n"
                                 % e.get("detail", "(no detail recorded)"))
        else:
            sys.stderr.write("ccc-agent: failed but no error detail was "
                             "recorded; see the event log (-v) below\n")
    if args.verbose:
        sys.stderr.write("ccc-agent: event log:\n")
        for e in session.events:
            line = "  %s  %s" % (e.get("time", ""), e.get("event", ""))
            if e.get("detail") is not None:
                line += ": %s" % e["detail"]
            sys.stderr.write(line + "\n")
    if session.state == "failed":
        sys.stderr.write(
            "ccc-agent: full record: %s\n"
            "ccc-agent: inspect with: ccc-agent show %s\n"
            % (store.session_file(session.session_id), session.session_id))

    if session.state == "pending-review":
        sys.stderr.write(
            "ccc-agent: review with: ccc-agent diff %s\n"
            "ccc-agent: then: ccc-agent commit %s | ccc-agent abort %s\n"
            % (session.session_id, session.session_id, session.session_id))
    if session.state == "failed":
        return 1
    if session.exit_status not in (0, None):
        return session.exit_status
    return 0


def _ctl_socket(args, env):
    """Per-turn control ops that run from INSIDE the sandbox.  They reach the
    supervisor over the control socket (CCC_AGENT_CONTROL_SOCK/TOKEN) — the
    BranchFS store and config are deliberately not reachable here, so these
    never touch load_config/build_runtime.  Degrade safe: never block the
    agent's Stop on missing plumbing or a control error."""
    env = os.environ if env is None else env
    sock = env.get(ENV_CONTROL_SOCK)
    token = env.get(ENV_CONTROL_TOKEN)
    if not sock or not token:
        sys.stderr.write(
            "ccc-agent: no control socket; per-turn control unavailable "
            "(not inside a contained session)\n")
        return 0
    client = ControlClient(sock, token)
    try:
        if args.cmd == "finalize-turn":
            resp = client.finalize_turn()
        else:  # approve-turn
            paths = ([p for p in args.paths.split(",") if p]
                     if getattr(args, "paths", None) else None)
            resp = client.approve_turn(args.approval_token, args.decision,
                                       paths=paths)
    except ChannelError as exc:
        sys.stderr.write("ccc-agent: control error: %s\n" % exc)
        return 0
    verdict = resp.get("verdict")
    if verdict == VERDICT_NEEDS_APPROVAL:
        paths = resp.get("out_of_scope", [])
        token2 = resp.get("approval_token")
        sys.stderr.write(
            "ccc-agent: %d change(s) are OUTSIDE the agent workspace and were "
            "NOT committed:\n" % len(paths))
        for path in paths:
            sys.stderr.write("  - %s\n" % path)
        sys.stderr.write(
            "ccc-agent: ask the user how to handle these, then run ONE of:\n"
            "    ccc-agent approve-turn %s            # commit all\n"
            "    ccc-agent approve-turn %s keep       # keep, don't commit\n"
            "    ccc-agent approve-turn %s revert     # discard (you undo)\n"
            "    ccc-agent approve-turn %s --paths a,b # commit only a,b\n"
            % (token2, token2, token2, token2))
        return 2
    if verdict == VERDICT_COMMITTED:
        msg = "committed %d change(s)" % len(resp.get("committed", []))
        if resp.get("held"):
            msg += " (held %d for review)" % len(resp["held"])
        sys.stdout.write(msg + "\n")
    elif verdict == VERDICT_HELD:
        if resp.get("revert"):
            sys.stdout.write("rejected; revert these in your workspace:\n")
            for path in resp["revert"]:
                sys.stdout.write("  - %s\n" % path)
        else:
            sys.stdout.write("changes held for review (not committed)\n")
    else:
        sys.stdout.write("%s\n" % (verdict or "ok"))
    return 0


def main_ctl(argv=None, env=None, prog="ccc-agent"):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept both ``ccc-agent --config X list`` (argparse's natural shape) and
    # ``ccc-agent list --config X`` (the shape people tend to type for verbs).
    if "--config" in argv:
        idx = argv.index("--config")
        if idx > 0 and idx + 1 < len(argv):
            pair = argv[idx:idx + 2]
            del argv[idx:idx + 2]
            argv = pair + argv
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Inspect and control BranchFS agent sessions.")
    parser.add_argument("--config", help="path to config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "status", "diff", "commit", "abort", "thaw",
                 "finish", "finish-turn", "check-before-final"):
        p = sub.add_parser(name)
        p.add_argument("session_id")
    rv = sub.add_parser("review", help="post-session change review")
    rv.add_argument("session_id")
    rv.add_argument("--accept", action="store_true", help="commit everything")
    rv.add_argument("--reject", action="store_true",
                    help="discard everything (revert)")
    rv.add_argument("--commit", dest="commit_paths",
                    help="comma-separated paths to commit file-by-file "
                         "(the rest are discarded)")
    rv.add_argument("--emit-patch", action="store_true",
                    help="print a base-vs-view unified diff for line-level "
                         "review")
    rv.add_argument("--apply-patch", metavar="FILE",
                    help="apply a (possibly pruned) patch to base for "
                         "line-level commit")
    # per-turn socket ops (no session_id; identified by the socket+token)
    sub.add_parser("finalize-turn")
    ap = sub.add_parser("approve-turn")
    ap.add_argument("approval_token")
    ap.add_argument("decision", nargs="?", default="yes",
                    help="yes (commit all, default) | keep (don't commit) | "
                         "revert (discard)")
    ap.add_argument("--paths", help="comma-separated subset to commit "
                                    "file-by-file; the rest are held")
    args = parser.parse_args(argv)

    if args.cmd in ("finalize-turn", "approve-turn"):
        return _ctl_socket(args, env)

    config = load_config(args.config, env=env)
    store, backend, alias_map, _user, _roots = build_runtime(config)
    controller = Controller(store=store, backend=backend, alias_map=alias_map)

    try:
        if args.cmd == "list":
            controller.list()
        elif args.cmd == "show":
            controller.show(args.session_id)
        elif args.cmd == "status":
            controller.status(args.session_id)
        elif args.cmd == "diff":
            controller.diff(args.session_id)
        elif args.cmd == "review":
            commit_paths = ([p for p in args.commit_paths.split(",") if p]
                            if args.commit_paths else None)
            session = controller.review(
                args.session_id, accept=args.accept, reject=args.reject,
                commit_paths=commit_paths, emit_patch=args.emit_patch,
                apply_patch=args.apply_patch)
            if session.state in ("committed", "aborted"):
                sys.stderr.write("session %s now %s\n"
                                 % (session.session_id, session.state))
        elif args.cmd == "commit":
            session = controller.commit(args.session_id)
            sys.stderr.write("committed session %s\n" % session.session_id)
        elif args.cmd == "abort":
            session = controller.abort(args.session_id)
            sys.stderr.write("aborted session %s\n" % session.session_id)
        elif args.cmd == "thaw":
            controller.thaw(args.session_id)
        elif args.cmd == "finish":
            session = controller.finish(args.session_id)
            sys.stderr.write("session %s now %s\n"
                             % (session.session_id, session.state))
        elif args.cmd == "finish-turn":
            controller.finish_turn(args.session_id)
        elif args.cmd == "check-before-final":
            # exit 2 = "block the stop, repair": the only code that loops the
            # agent. Allow and exhausted both exit 0 so hooks cannot livelock.
            if controller.check_before_final(args.session_id) == CHECK_REPAIR:
                return 2
    except ControlError as exc:
        sys.stderr.write("ccc-agent: %s\n" % exc)
        return 1
    return 0


def main_softsandbox(argv=None, env=None):
    """Run the legacy soft sandbox as ``ccc-agent softsandbox``.

    The soft sandbox is still a Bash implementation because it is a diagnostic /
    PoC helper rather than the production BranchFS+bwrap path. Keeping it as a
    package asset lets the public command surface stay unified without a second
    installed executable.
    """
    argv = [] if argv is None else list(argv)
    env = os.environ if env is None else env
    script_ref = resources.files("ccc_agent").joinpath(
        "assets", "scripts", "softsandbox.sh")
    with resources.as_file(script_ref) as script:
        return subprocess.call(["bash", str(script)] + argv, env=env)


_CTL_OPS = {
    "list", "show", "status", "diff", "commit", "abort", "thaw", "finish",
    "finish-turn", "check-before-final", "review", "finalize-turn",
    "approve-turn",
}


def _print_main_help(stream=None):
    stream = sys.stdout if stream is None else stream
    stream.write(
        "usage: ccc-agent OP [options]\n\n"
        "Unified CCC agent containment CLI.\n\n"
        "Primary ops:\n"
        "  run              run a command in a contained BranchFS session\n"
        "  setup            write config, plugin entries, and optional shims\n"
        "  softsandbox      diagnostic non-FUSE soft sandbox helper\n\n"
        "Session/control ops:\n"
        "  list, show, status, diff, review, commit, abort, thaw, finish\n"
        "  finish-turn, check-before-final, finalize-turn, approve-turn\n\n"
        "Examples:\n"
        "  ccc-agent run --workspace /home/$USER/project -- codex exec 'fix bug'\n"
        "  ccc-agent list\n"
        "  ccc-agent review <session> --accept\n"
        "  ccc-agent setup --system --enable-shims\n")


def main(argv=None, env=None):
    """Dispatch the unified ``ccc-agent OP`` command surface."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_main_help()
        return 0

    op, rest = argv[0], argv[1:]
    if op in ("run", "launch"):
        return main_run(rest, env=env, prog="ccc-agent %s" % op)
    if op == "setup":
        from . import setup as setup_mod
        return setup_mod.main(rest, prog="ccc-agent setup")
    if op == "softsandbox":
        return main_softsandbox(rest, env=env)

    # Control operations are direct: ``ccc-agent list``, ``ccc-agent diff ID``.
    # Also pass through leading global flags, e.g. ``ccc-agent --config X list``.
    if op in _CTL_OPS or op.startswith("-"):
        return main_ctl(argv, env=env, prog="ccc-agent")

    sys.stderr.write("ccc-agent: unknown op %r\n\n" % op)
    _print_main_help(stream=sys.stderr)
    return 2
