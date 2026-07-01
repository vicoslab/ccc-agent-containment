"""Tests for the ccc-agent run / ccc-agent command-line layer."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ccc_agent import cli as cli_mod
from ccc_agent.cli import load_config, main, main_ctl, main_run
from ccc_agent.session import ProtectedRoot, SessionStore


class CliHarness(object):
    def __init__(self, tmp):
        self.tmp = tmp
        self.base = os.path.join(tmp, "real", "storage_user")
        self.workspace_rel = "Projects/proj-a"
        os.makedirs(os.path.join(self.base, self.workspace_rel))
        self.config_path = os.path.join(tmp, "config.json")
        with open(self.config_path, "w") as fh:
            json.dump({
                "state_dir": os.path.join(tmp, "state"),
                "backend": "fake",
                "user": "domen",
                "home_subdir": "",
                # the fake backend exercises the pipeline without a real
                # sandbox; "none" runs the command directly (debug mode).
                "confinement": "none",
                "roots": [{
                    "name": "storage_user",
                    "base": self.base,
                    "store": os.path.join(tmp, "stores", "storage_user"),
                    "visible": "/storage/user",
                    "home_subdir": "",
                }],
            }, fh)

    def sessions(self):
        store = SessionStore(os.path.join(self.tmp, "state"))
        return sorted(s.session_id for s in store.list())


class TestLoadConfig(unittest.TestCase):
    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as fh:
                json.dump({"state_dir": "/x", "roots": []}, fh)
            config = load_config(path, env={})
            self.assertEqual(config["state_dir"], "/x")

    def test_env_var_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as fh:
                json.dump({"state_dir": "/y", "roots": []}, fh)
            config = load_config(env={"CCC_AGENT_CONFIG": path})
            self.assertEqual(config["state_dir"], "/y")

    def test_missing_config_exits(self):
        with self.assertRaises(SystemExit):
            load_config(env={})


class TestShellDefault(unittest.TestCase):
    def test_default_shell_prefers_current_parent_over_login_shell_env(self):
        with mock.patch("ccc_agent.cli._parent_shell_command",
                        return_value=["sh"]):
            self.assertEqual(
                getattr(cli_mod, "_current_shell_command")(
                    {"SHELL": "/bin/zsh"}),
                ["sh"])

    def test_default_shell_falls_back_to_shell_env(self):
        with mock.patch("ccc_agent.cli._parent_shell_command",
                        return_value=None):
            self.assertEqual(
                getattr(cli_mod, "_current_shell_command")(
                    {"SHELL": "/bin/zsh"}),
                ["/bin/zsh"])

    def test_default_shell_falls_back_to_bin_sh(self):
        with mock.patch("ccc_agent.cli._parent_shell_command",
                        return_value=None):
            self.assertEqual(getattr(cli_mod, "_current_shell_command")({}),
                             ["/bin/sh"])

    def test_parent_shell_preserves_sh_cmdline_spelling(self):
        def fake_open(path, mode="rb"):
            self.assertEqual(path, "/proc/123/cmdline")
            self.assertEqual(mode, "rb")
            return io.BytesIO(b"sh\0")

        with mock.patch("ccc_agent.cli.os.getppid", return_value=123):
            with mock.patch("builtins.open", side_effect=fake_open):
                self.assertEqual(getattr(cli_mod, "_parent_shell_command")(),
                                 ["sh"])

    def test_parent_shell_strips_login_shell_prefix(self):
        def fake_open(path, mode="rb"):
            return io.BytesIO(b"-zsh\0")

        with mock.patch("ccc_agent.cli.os.getppid", return_value=123):
            with mock.patch("builtins.open", side_effect=fake_open):
                self.assertEqual(getattr(cli_mod, "_parent_shell_command")(),
                                 ["zsh"])


class TestMainRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_contained_run_auto_commits(self):
        code = main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--agent", "fake",
            "--", "sh", "-c", "echo out > artifact.txt",
        ], env={})
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        self.assertEqual(len(self.h.sessions()), 1)

    def test_auto_commit_finish_line_summarizes_no_changes(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--agent", "fake",
                "--", "true",
            ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("finished: auto-committed (no changes)", output)

    def test_auto_commit_finish_line_summarizes_workspace_updates(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--agent", "fake",
                "--", "sh", "-c", "echo out > artifact.txt",
            ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("finished: auto-committed (1 update in workspace)",
                      output)

    def test_workspace_defaults_to_calling_directory_not_config_home(self):
        # System configs generated by ccc-agent setup historically contained a
        # broad default workspace such as /home/domen, which aliases to
        # /storage/user/<container> on CCC.  A bare `ccc-agent run codex` must
        # ignore that broad config value and use the directory where the user
        # invoked ccc-agent instead.
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["workspace"] = "/storage/user"
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        with mock.patch("ccc_agent.cli.os.getcwd",
                        return_value="/storage/user/Projects/proj-a"):
            code = main_run([
                "--config", self.h.config_path,
                "--agent", "fake",
                "--", "sh", "-c", "echo out > artifact.txt",
            ], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "artifact.txt")))

    def test_ignore_patterns_from_config_reach_the_session(self):
        # Regression: cli must carry policy.ignore_patterns from config (an
        # ignored out-of-scope write must not block the in-scope auto-commit).
        cfg = os.path.join(self._tmp.name, "cfg-ignore.json")
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["policy"] = {"mode": "workspace-auto",
                          "allowed_scopes": ["/storage/user/Projects/proj-a"],
                          "ignore_patterns": ["/storage/user/.codex"]}
        with open(cfg, "w") as fh:
            json.dump(data, fh)
        code = main_run([
            "--config", cfg,
            "--workspace", "/storage/user/Projects/proj-a",
            "--agent", "fake",
            "--", "sh", "-c",
            "mkdir -p ../../.codex && echo x > ../../.codex/foo; "
            "echo y > artifact.txt",
        ], env={})
        self.assertEqual(code, 0)
        # in-scope file committed -> auto-commit happened, i.e. the ignored
        # .codex write did NOT flag the session into pending-review
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))

    def test_run_displays_containment_banner_at_session_start(self):
        """A newly-created contained session should be obvious to the user."""
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["confinement"] = "bwrap"
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        def fake_run_session(config, env=None):
            root = config.roots[0].materialize(
                "agent-banner", config.store.state_dir,
                mount_dir=config.store.mount_dir("agent-banner"))
            session = SimpleNamespace(
                session_id="agent-banner",
                workspace="/storage/user/Projects/proj-a",
                protected_roots={"storage_user": root},
                state="auto-committed",
                events=[],
                exit_status=0,
            )
            callback = getattr(config, "on_session_start", None)
            if callback is not None:
                callback(session)
            return session

        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli.run_session", side_effect=fake_run_session):
            with contextlib.redirect_stderr(stderr):
                code = main_run([
                    "--config", self.h.config_path,
                    "--workspace", "/storage/user/Projects/proj-a",
                    "--agent", "fake",
                    "--", "true",
                ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        mount = os.path.join(self._tmp.name, "state", "agent-banner",
                             "mounts", "storage_user")
        self.assertIn("dropped into new contained BranchFS environment", output)
        self.assertIn("session: agent-banner", output)
        self.assertIn("serving visible /storage/user from BranchFS view %s"
                      % mount, output)
        self.assertNotIn("visible workspace:", output)
        self.assertNotIn("backend data path:", output)

    def test_agent_exit_code_propagates(self):
        code = main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "exit 7",
        ], env={})
        self.assertEqual(code, 7)

    def test_run_without_command_defaults_to_current_shell(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-shell", state="auto-committed",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            with mock.patch("ccc_agent.cli._parent_shell_command",
                            return_value=["/bin/zsh"]):
                code = main_run(["--config", self.h.config_path, "--"],
                                env={"SHELL": "/bin/bash"})

        self.assertEqual(code, 0)
        self.assertEqual(seen["config"].agent_command, ["/bin/zsh"])

    def test_agent_option_sets_explicit_agent_kind(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(
                session_id="agent-test",
                state="aborted",
                events=[],
                exit_status=0,
            )

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run([
                "--config", self.h.config_path,
                "--agent", "codex",
                "/opt/agents/claude", "-p", "hello",
            ], env={})

        self.assertEqual(code, 0)
        self.assertEqual(seen["config"].agent_kind, "codex")
        self.assertEqual(seen["config"].agent_command,
                         ["/opt/agents/claude", "-p", "hello"])

    def test_agent_state_binds_from_config_reach_runner(self):
        seen = {}
        binds = ["/real/codex:/home/domen/.codex",
                 "/real/claude:/home/domen/.claude",
                 "/real/hermes:/home/domen/.hermes"]
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["agent_state_binds"] = binds
        data["ensure_agent_state_dirs"] = True
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-test", state="aborted",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run(["--config", self.h.config_path, "--", "true"],
                            env={})

        self.assertEqual(code, 0)
        self.assertEqual(seen["config"].agent_state_binds, binds)
        self.assertTrue(seen["config"].ensure_agent_state_dirs)
        self.assertFalse(seen["config"].protect_agent_state)

    def test_protect_agent_state_flag_overrides_shared_default(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-test", state="aborted",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run(["--config", self.h.config_path,
                             "--protect-agent-state", "--", "true"], env={})

        self.assertEqual(code, 0)
        self.assertTrue(seen["config"].protect_agent_state)

    def test_shortcut_agent_flags_are_not_supported(self):
        for flag in ("--codex", "--claude", "--hermes"):
            with self.subTest(flag=flag):
                with mock.patch("ccc_agent.cli.run_session",
                                side_effect=AssertionError(
                                    "%s should not parse" % flag)):
                    with self.assertRaises(SystemExit) as cm:
                        main_run(["--config", self.h.config_path,
                                  flag, "codex"], env={})
                self.assertEqual(cm.exception.code, 2)


class TestMainCtl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_and_show_roundtrip(self):
        main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo x > f.txt",
        ], env={})
        self.assertEqual(main_ctl(["--config", self.h.config_path, "list"],
                                  env={}), 0)
        sid = self.h.sessions()[0]
        self.assertEqual(main_ctl(["--config", self.h.config_path, "show",
                                   sid], env={}), 0)

    def test_diff_accepts_optional_path(self):
        base_path = os.path.join(self.h.base, self.h.workspace_rel, "cli.txt")
        with open(base_path, "w") as fh:
            fh.write("old\n")
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--policy", "manual",
            "--", "sh", "-c", "printf 'old\\nnew\\n' > cli.txt",
        ], env={}), 0)
        sid = self.h.sessions()[0]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "diff", sid,
                             "cli.txt"], env={})

        self.assertEqual(code, 0)
        self.assertIn("--- a/Projects/proj-a/cli.txt", out.getvalue())
        self.assertIn("+new", out.getvalue())

    def test_ctl_error_returns_nonzero(self):
        self.assertEqual(
            main_ctl(["--config", self.h.config_path, "commit",
                      "agent-missing"], env={}), 1)


class TestUnifiedMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_op_dispatches_to_contained_run(self):
        code = main([
            "run", "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo unified > u.txt",
        ], env={})
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "u.txt")))

    def test_control_ops_are_direct(self):
        main([
            "run", "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo x > f.txt",
        ], env={})
        self.assertEqual(main(["--config", self.h.config_path, "list"],
                              env={}), 0)
        self.assertEqual(main(["list", "--config", self.h.config_path],
                              env={}), 0)

    def test_version_flag_reports_release_without_config(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--version"], env={})

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "ccc-agent v0.2\n")


class TestMainCtlCheckBeforeFinal(unittest.TestCase):
    """Exit-code contract for the hook: 2 = repair, 0 = allow/exhausted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_running_session(self, dirty_rel=None, max_repair_attempts=1):
        # Park a session in `running` against the same state dir the CLI
        # uses; FakeBranchFS status only needs the on-disk branch delta dir.
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        branch_store = os.path.join(self.h.tmp, "stores", "storage_user")
        files = os.path.join(branch_store, "branches", "agent-live", "files")
        os.makedirs(files, exist_ok=True)
        root = ProtectedRoot(
            name="storage_user", base=self.h.base, store=branch_store,
            branch="agent-live",
            mount=os.path.join(self.h.tmp, "mounts", "agent-live"),
            visible="/storage/user", home_subdir="")
        session = store.create(
            owner="domen", agent_kind="claude", agent_command=["claude"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "workspace-auto",
                    "allowed_scopes": ["/storage/user/Projects/proj-a"],
                    "max_policy_repair_attempts": max_repair_attempts},
            protected_roots={"storage_user": root}, completion="manual")
        session.transition("mounting")
        session.transition("running")
        store.save(session)
        if dirty_rel:
            with open(os.path.join(files, dirty_rel), "w") as fh:
                fh.write("x\n")
        return session.session_id

    def check(self, sid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path,
                             "check-before-final", sid], env={})
        return code, out.getvalue()

    def test_repair_then_exhausted_exit_codes(self):
        sid = self.make_running_session(dirty_rel="outside.txt")
        code, text = self.check(sid)
        self.assertEqual(code, 2)        # dirty + budget left: ask for repair
        self.assertIn("/storage/user/outside.txt", text)
        code, _text = self.check(sid)
        self.assertEqual(code, 0)        # budget exhausted: never loop

    def test_clean_running_session_exits_zero(self):
        sid = self.make_running_session()
        self.assertEqual(self.check(sid)[0], 0)

    def test_control_error_exits_one(self):
        self.assertEqual(self.check("agent-missing")[0], 1)


if __name__ == "__main__":
    unittest.main()
