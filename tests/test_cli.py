"""Tests for the ccc-agent run / ccc-agent command-line layer."""

import contextlib
import io
import json
import os
import tempfile
import unittest

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
        sessions_dir = os.path.join(self.tmp, "state", "sessions")
        if not os.path.isdir(sessions_dir):
            return []
        return sorted(os.listdir(sessions_dir))


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

    def test_agent_exit_code_propagates(self):
        code = main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "exit 7",
        ], env={})
        self.assertEqual(code, 7)

    def test_missing_command_errors(self):
        with self.assertRaises(SystemExit):
            main_run(["--config", self.h.config_path, "--"], env={})


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
