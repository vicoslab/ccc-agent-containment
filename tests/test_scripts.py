"""Tests for the shell scaffolding: chroot assembler (dry-run), launch shim,
hook adapters. All run unprivileged — --apply paths are exercised only as
syntax/plan checks here; runtime chroot validation needs a privileged host."""

import os
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
CHROOT_SH = os.path.join(AGENT_DIR, "scripts", "ccc-agent-chroot.sh")
SHIM_SH = os.path.join(AGENT_DIR, "shims", "ccc-agent-shim.sh")
HOOKS = [os.path.join(AGENT_DIR, "hooks", name)
         for name in ("claude-stop-hook.sh", "codex-stop-hook.sh",
                      "hermes-finish-turn.sh")]


class TestShellSyntax(unittest.TestCase):
    def test_all_scripts_parse(self):
        for script in [CHROOT_SH, SHIM_SH] + HOOKS:
            proc = subprocess.run(["bash", "-n", script],
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (script, proc.stderr))


class TestChrootDryRun(unittest.TestCase):
    def run_dry(self, *extra):
        argv = ["bash", CHROOT_SH,
                "--session-id", "agent-test-1",
                "--view", "/__branchfs_mounts/storage_user",
                "--user", "domen", "--uid", "1000", "--gid", "1000",
                *extra,
                "--", "codex", "exec", "task"]
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_plan_contains_views_and_boundaries(self):
        out = self.run_dry()
        self.assertIn("# dry-run", out)
        self.assertIn(
            "bind-rw /__branchfs_mounts/storage_user -> "
            "/run/ccc-agent/chroots/agent-test-1/storage/user", out)
        # home alias from the same view (home_subdir empty)
        self.assertIn("/home/domen", out)
        self.assertIn("NOT exposed: real underlay, BranchFS store", out)
        self.assertIn("setpriv --reuid=1000 --regid=1000", out)
        self.assertIn("CCC_AGENT_SESSION=agent-test-1", out)

    def test_home_subdir_plan(self):
        out = self.run_dry("--home-subdir", "domen")
        self.assertIn(
            "bind-rw /__branchfs_mounts/storage_user/domen -> "
            "/run/ccc-agent/chroots/agent-test-1/home/domen", out)

    def test_dry_run_performs_no_mounts(self):
        # the dry run must not create the chroot directory
        out = self.run_dry()
        self.assertTrue(out)
        self.assertFalse(os.path.exists(
            "/run/ccc-agent/chroots/agent-test-1"))

    def test_rejects_path_traversal_session_id(self):
        proc = subprocess.run(
            ["bash", CHROOT_SH, "--session-id", "../escape",
             "--view", "/v", "--user", "u", "--", "true"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(proc.returncode, 0)


class TestShim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.shimdir = os.path.join(tmp, "shims")
        self.realdir = os.path.join(tmp, "real")
        os.makedirs(self.shimdir)
        os.makedirs(self.realdir)
        os.symlink(SHIM_SH, os.path.join(self.shimdir, "codex"))
        self.real = os.path.join(self.realdir, "codex")
        with open(self.real, "w") as fh:
            fh.write("#!/bin/sh\necho REAL:$0:$*\n")
        os.chmod(self.real, 0o755)
        self.launcher = os.path.join(tmp, "ccc-agent-launch")
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/sh\necho LAUNCH:$*\n")
        os.chmod(self.launcher, 0o755)
        self.env = {
            "PATH": "%s:%s:/usr/bin:/bin" % (self.shimdir, self.realdir),
            "CCC_AGENT_LAUNCH": self.launcher,
        }

    def tearDown(self):
        self._tmp.cleanup()

    def run_shim(self, env_extra=None, args=("do", "thing")):
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.run(["codex", *args], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

    def test_shim_wraps_with_launcher(self):
        proc = self.run_shim()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LAUNCH:--agent codex -- %s do thing" % self.real,
                      proc.stdout)

    def test_nested_session_runs_real_binary_directly(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SESSION": "agent-x"})
        self.assertIn("REAL:", proc.stdout)
        self.assertNotIn("LAUNCH:", proc.stdout)

    def test_bypass_env(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SHIM_BYPASS": "1"})
        self.assertIn("REAL:", proc.stdout)
        self.assertIn("bypass", proc.stderr)

    def test_missing_launcher_refuses_unprotected_run(self):
        env = dict(self.env)
        env["CCC_AGENT_LAUNCH"] = "/nonexistent/launcher"
        proc = subprocess.run(["codex", "x"], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("REAL:", proc.stdout)
        self.assertIn("refusing", proc.stderr)


class TestHooksAreNoopsOutsideSessions(unittest.TestCase):
    def test_hooks_exit_zero_without_session(self):
        for hook in HOOKS:
            proc = subprocess.run(["sh", hook], env={"PATH": "/usr/bin:/bin"},
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (hook, proc.stderr))


if __name__ == "__main__":
    unittest.main()
