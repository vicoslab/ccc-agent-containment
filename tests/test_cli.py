"""Tests for the ccc-agent-run / ccc-agentctl command-line layer."""

import json
import os
import tempfile
import unittest

from ccc_agent.cli import load_config, main_ctl, main_run


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


if __name__ == "__main__":
    unittest.main()
