"""Tests for ccc_agent.branchfs: CLI driver (with fake subprocess) and the
in-memory FakeBranchFS used by runner tests."""

import json
import os
import tempfile
import unittest

from ccc_agent.branchfs import BranchfsCli, BranchfsError, FakeBranchFS
from ccc_agent.session import ProtectedRoot


def make_root(tmp, branch="agent-20260611T000000Z-abc12345"):
    return ProtectedRoot(
        name="storage_user",
        base=os.path.join(tmp, "base"),
        store=os.path.join(tmp, "store"),
        branch=branch,
        mount=os.path.join(tmp, "mounts", "storage_user"),
        visible="/storage/user",
    )


STATUS_JSON = {
    "name": "agent-20260611T000000Z-abc12345",
    "parent": "main",
    "inheritance": "lazy",
    "state": "frozen",
    "parent_version_at_fork": 0,
    "commit_count": 0,
    "delta_entries": 2,
    "tombstones": 1,
    "diff": [
        {"op": "delta", "path": "Projects/proj-a/new.py", "kind": "file",
         "bytes": 12},
        {"op": "delta", "path": "Projects/proj-a/sub", "kind": "dir",
         "bytes": 0},
        {"op": "delete", "path": "Projects/proj-a/old.txt",
         "kind": "tombstone", "bytes": 0},
    ],
}


class RecordingRunner(object):
    """Stands in for subprocess execution inside BranchfsCli."""

    def __init__(self, outputs=None, fail_on=None):
        self.calls = []
        self.outputs = outputs or {}
        self.fail_on = fail_on or set()

    def __call__(self, argv):
        self.calls.append(list(argv))
        subcommand = argv[1]
        if subcommand in self.fail_on:
            return 1, "", "daemon not running"
        return 0, self.outputs.get(subcommand, ""), ""


class TestBranchfsCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_daemon_argv(self):
        runner = RecordingRunner()
        cli = BranchfsCli(binary="branchfs", run=runner)
        cli.start_daemon(self.root)
        self.assertEqual(runner.calls[0][:2], ["branchfs", "start-daemon"])
        self.assertIn("--base", runner.calls[0])
        self.assertIn(self.root.base, runner.calls[0])
        self.assertIn("--storage", runner.calls[0])
        self.assertIn(self.root.store, runner.calls[0])

    def test_create_branch_argv_has_no_mountpoint(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.create_branch(self.root)
        # daemon-dependent ops re-ensure the daemon first
        self.assertEqual(runner.calls[0][1], "start-daemon")
        call = runner.calls[-1]
        self.assertEqual(call[1], "create")
        self.assertIn(self.root.branch, call)
        # supervisor creates branches without any mounted view
        self.assertNotIn(self.root.mount, call)

    def test_create_branch_passes_hide_paths(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        self.root.hide_paths = [".ssh", ".netrc"]
        cli.create_branch(self.root)
        call = runner.calls[-1]
        self.assertEqual(call[1], "create")
        self.assertEqual(call.count("--hide"), 2)
        self.assertIn(".ssh", call)
        self.assertIn(".netrc", call)

    def test_mount_agent_view(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=True)
        call = runner.calls[-1]
        self.assertEqual(call[1], "mount")
        self.assertIn("--agent", call)
        self.assertIn("--branch", call)
        self.assertIn(self.root.branch, call)
        self.assertEqual(call[-1], self.root.mount)

    def test_trusted_mount_keeps_control(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=False)
        self.assertNotIn("--agent", runner.calls[-1])

    def test_mount_allow_other_for_privilege_separated_agent(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=True, allow_other=True)
        call = runner.calls[-1]
        self.assertIn("--allow-other", call)
        # mountpoint stays the final positional argument
        self.assertEqual(call[-1], self.root.mount)

    def test_mount_omits_allow_other_by_default(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=True)
        self.assertNotIn("--allow-other", runner.calls[-1])

    def test_status_parses_changes_into_visible_namespace(self):
        runner = RecordingRunner(outputs={"status": json.dumps(STATUS_JSON)})
        cli = BranchfsCli(run=runner)
        changes = cli.status(self.root)
        self.assertIn("--json", runner.calls[-1])
        by_path = {c.path: c for c in changes}
        added = by_path["/storage/user/Projects/proj-a/new.py"]
        self.assertEqual(added.op, "M")
        self.assertEqual(added.kind, "file")
        self.assertEqual(added.bytes, 12)
        self.assertEqual(added.root, "storage_user")
        deleted = by_path["/storage/user/Projects/proj-a/old.txt"]
        self.assertEqual(deleted.op, "D")

    def test_failure_raises_with_stderr(self):
        runner = RecordingRunner(fail_on={"freeze"})
        cli = BranchfsCli(run=runner)
        with self.assertRaises(BranchfsError) as ctx:
            cli.freeze(self.root)
        self.assertIn("daemon not running", str(ctx.exception))

    def test_commit_and_abort_use_trusted_branch_commands(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.commit(self.root)
        cli.abort(self.root)
        subcommands = [c[1] for c in runner.calls]
        self.assertEqual(subcommands, ["start-daemon", "commit-branch",
                                       "start-daemon", "abort-branch"])


class TestFakeBranchFS(unittest.TestCase):
    """The fake must be behaviorally close enough to drive the runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)
        os.makedirs(self.root.base)
        with open(os.path.join(self.root.base, "existing.txt"), "w") as fh:
            fh.write("base data\n")
        self.fake = FakeBranchFS()

    def tearDown(self):
        self.tmp.cleanup()

    def test_mount_creates_writable_view(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        self.assertTrue(os.path.isdir(self.root.mount))
        with open(os.path.join(self.root.mount, "agent.txt"), "w") as fh:
            fh.write("agent wrote this\n")

    def test_status_reports_writes_in_visible_namespace(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        os.makedirs(os.path.join(self.root.mount, "Projects/p"), exist_ok=True)
        with open(os.path.join(self.root.mount, "Projects/p/a.py"), "w") as fh:
            fh.write("x = 1\n")
        self.fake.record_delete(self.root, "existing.txt")
        changes = self.fake.status(self.root)
        ops = {c.path: c.op for c in changes}
        self.assertEqual(ops["/storage/user/Projects/p/a.py"], "M")
        self.assertEqual(ops["/storage/user/existing.txt"], "D")

    def test_freeze_blocks_status_free_write_simulation(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.freeze(self.root)
        self.assertEqual(self.fake.branch_state(self.root), "frozen")
        self.fake.thaw(self.root)
        self.assertEqual(self.fake.branch_state(self.root), "open")

    def test_commit_applies_deltas_and_tombstones_to_base(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        with open(os.path.join(self.root.mount, "new.txt"), "w") as fh:
            fh.write("delta\n")
        self.fake.record_delete(self.root, "existing.txt")
        self.fake.freeze(self.root)
        self.fake.commit(self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.root.base, "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root.base,
                                                     "existing.txt")))

    def test_abort_discards_deltas(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        with open(os.path.join(self.root.mount, "junk.txt"), "w") as fh:
            fh.write("discard me\n")
        self.fake.abort(self.root)
        self.assertEqual(self.fake.status(self.root), [])
        self.assertFalse(os.path.exists(os.path.join(self.root.base,
                                                     "junk.txt")))


if __name__ == "__main__":
    unittest.main()
