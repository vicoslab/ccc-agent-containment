"""End-to-end (non-FUSE) tests for ccc_agent.runner using FakeBranchFS.

These mirror the Phase 2 validation list from the accepted design:
- agent writing inside the workspace auto-commits;
- agent writing outside the workspace becomes pending-review;
- agent deleting global config is recoverable;
- a no-op run closes cleanly.
"""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ccc_agent.branchfs import FakeBranchFS
from ccc_agent.paths import AliasMap
from ccc_agent.runner import RootSpec, RunnerConfig, run_session
from ccc_agent.session import SessionStore


class RunnerHarness(object):
    def __init__(self, tmp, mode="workspace-auto"):
        self.tmp = tmp
        self.state_dir = os.path.join(tmp, "state")
        self.base = os.path.join(tmp, "real", "storage_user")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"),
                    exist_ok=True)
        with open(os.path.join(self.base, ".bashrc"), "w") as fh:
            fh.write("export PS1=x\n")
        self.backend = FakeBranchFS()
        self.store = SessionStore(self.state_dir)
        self.mode = mode

    def config(self, argv, mode=None, hide_patterns=(), **extra):
        return RunnerConfig(
            store=self.store,
            backend=self.backend,
            alias_map=AliasMap.for_home("domen", home_subdir=""),
            owner="domen",
            agent_kind="fake-agent",
            agent_command=list(argv),
            workspace="/home/domen/Projects/proj-a",
            policy={
                "mode": mode or self.mode,
                "allowed_scopes": ["/home/domen/Projects/proj-a"],
                "hide_patterns": list(hide_patterns),
            },
            roots=[RootSpec(name="storage_user", base=self.base,
                            store=os.path.join(self.tmp, "stores",
                                               "storage_user"),
                            visible="/storage/user", home_subdir="")],
            **extra
        )


class TestRunSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = RunnerHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_workspace_write_auto_commits(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo done > result.txt"]))
        self.assertEqual(session.state, "auto-committed")
        self.assertEqual(session.exit_status, 0)
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "result.txt")
        self.assertTrue(os.path.isfile(committed))

    def test_out_of_scope_write_pends_review_and_underlay_untouched(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo hacked > ../../escape.txt"]))
        self.assertEqual(session.state, "pending-review")
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "escape.txt")))

    def test_global_config_delete_is_recoverable(self):
        config = self.h.config(["sh", "-c", "true"])
        session = run_session(config, before_finalize=lambda s: (
            self.h.backend.record_delete(s.protected_roots["storage_user"],
                                         ".bashrc")))
        self.assertEqual(session.state, "pending-review")
        # underlay untouched until a human commits
        self.assertTrue(os.path.isfile(os.path.join(self.h.base, ".bashrc")))

    def test_noop_run_closes_cleanly(self):
        session = run_session(self.h.config(["true"]))
        self.assertEqual(session.state, "aborted")
        self.assertTrue(any("no changes" in (e.get("detail") or "")
                            for e in session.events))

    def test_throwaway_mode_aborts_even_clean_writes(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > t.txt"], mode="throwaway"))
        self.assertEqual(session.state, "aborted")
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "Projects", "proj-a", "t.txt")))

    def test_agent_env_carries_session_id(self):
        session = run_session(self.h.config(
            ["sh", "-c", "printf %s \"$CCC_AGENT_SESSION\" > sid.txt"]))
        committed = os.path.join(self.h.base, "Projects", "proj-a", "sid.txt")
        with open(committed) as fh:
            self.assertEqual(fh.read(), session.session_id)

    def test_nonzero_exit_still_finalizes(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo partial > p.txt; exit 3"]))
        self.assertEqual(session.exit_status, 3)
        self.assertEqual(session.state, "auto-committed")

    def test_review_artifacts_written(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > ../../oops.txt"]))
        review = self.h.store.review_dir(session.session_id)
        for name in ("session.json", "status.storage_user.json",
                     "policy-decision.json", "summary.md"):
            self.assertTrue(os.path.isfile(os.path.join(review, name)),
                            "missing artifact %s" % name)
        with open(os.path.join(review, "policy-decision.json")) as fh:
            decision = json.load(fh)
        self.assertEqual(decision["decision"], "pending-review")
        self.assertEqual(decision["out_of_scope"], ["/storage/user/oops.txt"])
        with open(os.path.join(review, "summary.md")) as fh:
            summary = fh.read()
        self.assertIn(session.session_id, summary)
        self.assertIn("ccc-agentctl commit", summary)

    def test_mount_failure_marks_session_failed(self):
        class FailingMount(FakeBranchFS):
            def mount(self, root, agent=True):
                raise RuntimeError("no fuse for you")

        self.h.backend = FailingMount()
        session = run_session(self.h.config(["true"]))
        self.assertEqual(session.state, "failed")
        persisted = self.h.store.load(session.session_id)
        self.assertEqual(persisted.state, "failed")

    def test_auto_commit_unmounts_before_committing(self):
        # The real branchfs binary fails commit-branch with ENOTEMPTY if the
        # branch is still mounted (the store dir is busy).  The supervisor must
        # unmount the bundle before applying the commit decision.
        class MountedCommitFails(FakeBranchFS):
            def commit(self, root):
                if root.mount in self._mounted:
                    raise RuntimeError("Directory not empty (os error 39)")
                super(MountedCommitFails, self).commit(root)

        self.h.backend = MountedCommitFails()
        session = run_session(self.h.config(
            ["sh", "-c", "echo done > result.txt"]))
        self.assertEqual(session.state, "auto-committed")
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "result.txt")
        self.assertTrue(os.path.isfile(committed))

    def test_nested_invocation_reuses_session(self):
        outer = run_session(self.h.config(
            ["sh", "-c", "echo outer > outer.txt"]))
        before = len(self.h.store.list())
        nested_config = self.h.config(["true"])
        nested = run_session(nested_config,
                             env={"CCC_AGENT_SESSION": outer.session_id})
        self.assertEqual(nested.session_id, outer.session_id)
        self.assertEqual(len(self.h.store.list()), before)

    def test_frozen_branches_left_frozen_on_pending_review(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > ../../outside.txt"]))
        root = session.protected_roots["storage_user"]
        self.assertEqual(self.h.backend.branch_state(root), "frozen")


class TestConfinementModes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = RunnerHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_confinement_rejected(self):
        with self.assertRaises(ValueError):
            self.h.config(["true"], confinement="jail")

    def test_chroot_confinement_no_longer_supported(self):
        # chroot was removed (needed a privileged container); it must now be
        # rejected like any other unknown mode.
        with self.assertRaises(ValueError):
            self.h.config(["true"], confinement="chroot")

    def test_none_confinement_runs_command_directly(self):
        # Regression: default mode must not wrap the command.
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self.h.config(["my-agent", "--flag"]))
        self.assertEqual(seen["argv"], ["my-agent", "--flag"])


class TestBwrapConfinement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = RunnerHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bwrap_config(self, argv, **kw):
        return self.h.config(argv, confinement="bwrap",
                             bwrap_bin="/opt/ccc-agent/bin/bwrap", **kw)

    def test_bwrap_needs_no_script_or_uid(self):
        # Unlike chroot, bwrap is rootless: it must not require uid/gid/script.
        cfg = self.h.config(["true"], confinement="bwrap")
        self.assertEqual(cfg.confinement, "bwrap")

    def test_bwrap_rejects_bad_proc_mode(self):
        with self.assertRaises(ValueError):
            self.h.config(["true"], confinement="bwrap",
                          bwrap_proc_mode="magic")

    def test_bwrap_mode_builds_sandbox_and_wraps_command(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(argv, 0)

        # bwrap is same-uid, so the view must NOT be mounted allow_other.
        orig_mount = self.h.backend.mount
        mounts = []

        def spy_mount(root, agent=True, allow_other=False):
            mounts.append(allow_other)
            return orig_mount(root, agent=agent, allow_other=allow_other)

        self.h.backend.mount = spy_mount

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(["my-agent", "--flag"]))

        self.assertTrue(mounts and not any(mounts),
                        "bwrap is same-uid; allow_other must stay off")
        argv = seen["argv"]
        self.assertEqual(argv[0], "/opt/ccc-agent/bin/bwrap")
        self.assertIn("--unshare-user", argv)
        self.assertIn("--unshare-pid", argv)
        # OS exposed read-only (first --ro-bind is /usr)
        i = argv.index("--ro-bind")
        self.assertEqual(argv[i + 1], "/usr")
        # the view is bound rw at its visible path and at $HOME
        self.assertIn("/storage/user", argv)
        self.assertIn("/home/domen", argv)
        # workspace is the sandbox cwd via --chdir
        ci = argv.index("--chdir")
        self.assertEqual(argv[ci + 1], "/storage/user/Projects/proj-a")
        # the real agent command follows the -- separator
        sep = argv.index("--")
        self.assertEqual(argv[sep + 1:], ["my-agent", "--flag"])
        # no host-side cwd is forced (bwrap --chdir handles it)
        self.assertIsNone(seen["cwd"])
        self.assertTrue(any(e.get("kind") == "bwrap-launch"
                            or e.get("event") == "bwrap-launch"
                            for e in session.events))

    def test_bwrap_ro_binds_and_setenv_after_view(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        runtime = self.h.base
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                ["true"], bwrap_ro_binds=[runtime, "/no/such/path"],
                bwrap_setenv={"OPENAI_API_KEY": "sek-test"}))
        argv = seen["argv"]
        # both runtime paths are re-exposed with --ro-bind-try; the missing one
        # is tolerated by bwrap at mount time (skipped) rather than aborting the
        # sandbox, so it still appears in argv.
        self.assertIn(runtime, argv)
        self.assertIn("/no/such/path", argv)
        # the ro-bind for the runtime must come AFTER the view bind so it wins
        view_i = argv.index("/storage/user")
        ro_i = max(k for k in range(len(argv) - 1)
                   if argv[k] == "--ro-bind-try" and argv[k + 1] == runtime)
        self.assertGreater(ro_i, view_i)
        # setenv is passed through
        si = [k for k in range(len(argv) - 1)
              if argv[k] == "--setenv" and argv[k + 1] == "OPENAI_API_KEY"]
        self.assertTrue(si and argv[si[0] + 2] == "sek-test")

    def test_bwrap_binds_control_socket_and_sets_env(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(["my-agent"]))
        argv = seen["argv"]
        sock = "/run/ccc-agent/control.sock"
        # the host socket is bind-mounted to the fixed in-sandbox path
        bind_dests = [argv[k + 2] for k in range(len(argv) - 2)
                      if argv[k] == "--bind"]
        self.assertIn(sock, bind_dests)
        # the in-sandbox env points the hook at that socket + a token
        env = {argv[k + 1]: argv[k + 2] for k in range(len(argv) - 2)
               if argv[k] == "--setenv"}
        self.assertEqual(env.get("CCC_AGENT_CONTROL_SOCK"), sock)
        self.assertTrue(env.get("CCC_AGENT_CONTROL_TOKEN"))
        # everything is before the -- command separator
        sep = argv.index("--")
        self.assertEqual(argv[sep + 1:], ["my-agent"])
        self.assertTrue(any(e.get("kind") == "control-server"
                            or e.get("event") == "control-server"
                            for e in session.events))

    def test_bwrap_credentials_mount_mask_and_env(self):
        cred_dir = os.path.join(self._tmp.name, "home", ".codex")
        os.makedirs(cred_dir)
        auth = os.path.join(cred_dir, "auth.json")
        with open(auth, "w") as fh:
            json.dump({"tokens": {"access": "sek-xyz"}}, fh)

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                ["a"], cred_mounts=[cred_dir], cred_mask=[auth],
                cred_env={"OPENAI_API_KEY":
                          {"file": auth, "json_key": "tokens.access"}}))
        argv = seen["argv"]
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        # cred dir re-exposed read-only (--ro-bind-try: a missing cred dir must
        # not abort the sandbox)
        self.assertIn(("--ro-bind-try", cred_dir, cred_dir), triples)
        # secret file masked with /dev/null
        self.assertIn(("--ro-bind", "/dev/null", auth), triples)
        # credential extracted from the host auth file and passed via env
        self.assertIn(("--setenv", "OPENAI_API_KEY", "sek-xyz"), triples)

    def test_per_turn_off_starts_no_control_server(self):
        # none mode (per_turn defaults off): no control env injected.
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self.h.config(["true"]))
        self.assertNotIn("CCC_AGENT_CONTROL_SOCK", seen["env"])
        self.assertFalse(any(e.get("kind") == "control-server"
                             for e in session.events))

    def test_per_turn_opt_in_for_none_sets_host_socket_env(self):
        # `none` debug mode can opt into per-turn; the hook reaches the host
        # socket directly (no sandbox remap).
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self.h.config(["true"], per_turn=True))
        self.assertIn("CCC_AGENT_CONTROL_SOCK", seen["env"])
        self.assertTrue(seen["env"].get("CCC_AGENT_CONTROL_TOKEN"))

    def test_bwrap_proc_mode_selects_flag(self):
        cases = {"bind": "--bind", "ro": "--ro-bind", "fresh": "--proc"}
        for mode, flag in cases.items():
            seen = {}

            def fake_run(argv, **kwargs):
                seen["argv"] = list(argv)
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                run_session(self._bwrap_config(["true"], bwrap_proc_mode=mode))
            argv = seen["argv"]
            self.assertIn("/proc", argv, "proc not mounted in mode %s" % mode)
            found = any(argv[k] == flag and argv[k + 1] == "/proc"
                        for k in range(len(argv) - 1))
            self.assertTrue(found, "mode %s missing %s /proc" % (mode, flag))


if __name__ == "__main__":
    unittest.main()
