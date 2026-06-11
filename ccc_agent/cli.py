"""Command-line entrypoints: ccc-agent-run / ccc-agent-launch / ccc-agentctl.

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
import sys

from .branchfs import BranchfsCli, FakeBranchFS
from .ctl import Controller, ControlError
from .paths import AliasMap
from .runner import RootSpec, RunnerConfig, run_session
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


def main_run(argv=None, env=None):
    parser = argparse.ArgumentParser(
        prog="ccc-agent-run",
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
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- command to run")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no agent command given (use: ccc-agent-run ... -- cmd)")

    config = load_config(args.config, env=env)
    store, backend, alias_map, user, roots = build_runtime(config)
    workspace = args.workspace or os.getcwd()
    policy = {
        "mode": args.policy,
        "allowed_scopes": [workspace] + list(args.scope),
        "hide_patterns": list(args.hide) + list(config.get("hide_patterns",
                                                           ())),
    }
    runner_config = RunnerConfig(
        store=store, backend=backend, alias_map=alias_map, owner=user,
        agent_kind=args.agent, agent_command=command, workspace=workspace,
        policy=policy, roots=roots)
    session = run_session(runner_config, env=env)

    sys.stderr.write("ccc-agent: session %s finished: %s\n"
                     % (session.session_id, session.state))
    if session.state == "pending-review":
        sys.stderr.write(
            "ccc-agent: review with: ccc-agentctl diff %s\n"
            "ccc-agent: then: ccc-agentctl commit %s | ccc-agentctl abort %s\n"
            % (session.session_id, session.session_id, session.session_id))
    if session.state == "failed":
        return 1
    if session.exit_status not in (0, None):
        return session.exit_status
    return 0


def main_ctl(argv=None, env=None):
    parser = argparse.ArgumentParser(
        prog="ccc-agentctl",
        description="Inspect and control BranchFS agent sessions.")
    parser.add_argument("--config", help="path to config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "status", "diff", "commit", "abort", "thaw",
                 "finish", "finish-turn"):
        p = sub.add_parser(name)
        p.add_argument("session_id")
    args = parser.parse_args(argv)

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
    except ControlError as exc:
        sys.stderr.write("ccc-agentctl: %s\n" % exc)
        return 1
    return 0
