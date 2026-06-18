"""End-to-end (non-FUSE) tests for ccc_agent.runner using FakeBranchFS.

These mirror the Phase 2 validation list from the accepted design:
- agent writing inside the workspace auto-commits;
- agent writing outside the workspace becomes pending-review;
- agent deleting global config is recoverable;
- a no-op run closes cleanly.
"""

import json
import os
import tempfile
import unittest

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

    def config(self, argv, mode=None, hide_patterns=()):
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


if __name__ == "__main__":
    unittest.main()
