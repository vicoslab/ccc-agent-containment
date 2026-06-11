"""Tests for ccc_agent.ctl: the human/operator session control surface."""

import io
import json
import os
import tempfile
import unittest

from ccc_agent import ctl
from ccc_agent.branchfs import FakeBranchFS
from ccc_agent.paths import AliasMap
from ccc_agent.runner import RootSpec, RunnerConfig, run_session
from ccc_agent.session import SessionStore


class CtlHarness(object):
    def __init__(self, tmp):
        self.tmp = tmp
        self.state_dir = os.path.join(tmp, "state")
        self.base = os.path.join(tmp, "real", "storage_user")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"),
                    exist_ok=True)
        self.backend = FakeBranchFS()
        self.store = SessionStore(self.state_dir)
        self.alias_map = AliasMap.for_home("domen", home_subdir="")

    def run_agent(self, argv, mode="workspace-auto"):
        return run_session(RunnerConfig(
            store=self.store, backend=self.backend, alias_map=self.alias_map,
            owner="domen", agent_kind="fake", agent_command=list(argv),
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/home/domen/Projects/proj-a"]},
            roots=[RootSpec(name="storage_user", base=self.base,
                            store=os.path.join(self.tmp, "stores",
                                               "storage_user"),
                            visible="/storage/user", home_subdir="")],
        ))

    def controller(self):
        return ctl.Controller(store=self.store, backend=self.backend,
                              alias_map=self.alias_map)


class TestController(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CtlHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def pending_session(self):
        # out-of-scope write => pending-review with frozen branch
        return self.h.run_agent(["sh", "-c", "echo x > ../../outside.txt"])

    def test_list_renders_sessions(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().list(out=out)
        text = out.getvalue()
        self.assertIn(session.session_id, text)
        self.assertIn("pending-review", text)

    def test_show_dumps_json(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().show(session.session_id, out=out)
        data = json.loads(out.getvalue())
        self.assertEqual(data["session_id"], session.session_id)

    def test_diff_prints_stored_changes(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)
        self.assertIn("/storage/user/outside.txt", out.getvalue())

    def test_commit_pending_session(self):
        session = self.pending_session()
        self.h.controller().commit(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(self.h.base,
                                                    "outside.txt")))

    def test_abort_pending_session(self):
        session = self.pending_session()
        self.h.controller().abort(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "aborted")
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "outside.txt")))

    def test_commit_terminal_session_rejected(self):
        session = self.h.run_agent(["sh", "-c", "echo ok > fine.txt"])
        self.assertEqual(session.state, "auto-committed")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().commit(session.session_id)

    def test_finish_turn_records_event(self):
        session = self.pending_session()
        self.h.controller().finish_turn(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertTrue(any(e["event"] == "turn-finished"
                            for e in reloaded.events))

    def test_finish_finalizes_running_session(self):
        # simulate a long-running session that a hook/human finishes
        store = self.h.store
        backend = self.h.backend
        from ccc_agent.session import ProtectedRoot
        root = ProtectedRoot(
            name="storage_user", base=self.h.base,
            store=os.path.join(self._tmp.name, "stores", "storage_user"),
            branch="agent-longrun", mount=os.path.join(self._tmp.name,
                                                       "mounts", "longrun"),
            visible="/storage/user", home_subdir="")
        session = store.create(
            owner="domen", agent_kind="hermes-gateway",
            agent_command=["hermes", "serve"],
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": "workspace-auto",
                    "allowed_scopes": ["/home/domen/Projects/proj-a"]},
            protected_roots={"storage_user": root}, completion="manual")
        backend.start_daemon(root)
        backend.create_branch(root)
        backend.mount(root)
        with open(os.path.join(root.mount, "served.txt"), "w") as fh:
            fh.write("output\n")
        # walk the session into running state like a launcher would
        session.transition("mounting")
        session.transition("running")
        store.save(session)

        self.h.controller().finish(session.session_id)
        reloaded = store.load(session.session_id)
        self.assertEqual(reloaded.state, "pending-review")

    def test_unknown_session_raises(self):
        with self.assertRaises(ctl.ControlError):
            self.h.controller().show("agent-missing")


if __name__ == "__main__":
    unittest.main()
