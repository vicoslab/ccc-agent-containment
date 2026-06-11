"""Integration test: BranchfsCli against the real branchfs binary.

Validates the Python<->Rust contract (argv shapes, status JSON parsing,
daemon lifecycle) without FUSE: branches are created and managed through
the daemon socket, agent deltas are simulated by writing into the branch
store the same way FUSE write-out would land them.

Skipped unless a branchfs binary is available via $CCC_AGENT_BRANCHFS_BIN
or at the sibling worktree's target/debug/branchfs.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from ccc_agent.branchfs import BranchfsCli
from ccc_agent.session import ProtectedRoot

# library dirs to try when the binary links libfuse3 from a conda env
_LD_CANDIDATES = (
    os.environ.get("CCC_AGENT_BRANCHFS_LDLIB", ""),
    "/home/domen/conda/envs/branchfs-dev/lib",
)


def _probe(binary, env):
    try:
        proc = subprocess.run([binary, "--help"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return False
    return proc.returncode == 0


def find_branchfs():
    """Return (binary, env) for a runnable branchfs, or (None, None)."""
    candidates = []
    env_bin = os.environ.get("CCC_AGENT_BRANCHFS_BIN")
    if env_bin:
        candidates.append(env_bin)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.normpath(os.path.join(
        here, "..", "..", "..", "branchfs-agent-containment",
        "target", "debug", "branchfs")))
    candidates.append(shutil.which("branchfs") or "")
    for candidate in candidates:
        if not (candidate and os.access(candidate, os.X_OK)):
            continue
        if _probe(candidate, None):
            return candidate, None
        for libdir in _LD_CANDIDATES:
            if not (libdir and os.path.isdir(libdir)):
                continue
            env = dict(os.environ)
            env["LD_LIBRARY_PATH"] = (libdir + ":"
                                      + env.get("LD_LIBRARY_PATH", ""))
            if _probe(candidate, env):
                return candidate, env
    return None, None


BRANCHFS_BIN, BRANCHFS_ENV = find_branchfs()


def run_with_env(argv):
    proc = subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True,
                          env=BRANCHFS_ENV)
    return proc.returncode, proc.stdout, proc.stderr


@unittest.skipUnless(BRANCHFS_BIN, "no branchfs binary available")
class TestRealBranchfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.join(self.tmp.name, "base")
        os.makedirs(os.path.join(base, "Projects"))
        with open(os.path.join(base, "Projects", "inherited.txt"), "w") as fh:
            fh.write("underlay\n")
        with open(os.path.join(base, ".netrc"), "w") as fh:
            fh.write("machine secret\n")
        self.root = ProtectedRoot(
            name="storage_user",
            base=base,
            store=os.path.join(self.tmp.name, "store"),
            branch="agent-itest",
            mount=os.path.join(self.tmp.name, "mounts", "storage_user"),
            visible="/storage/user",
            hide_paths=[".netrc"],
        )
        self.cli = BranchfsCli(binary=BRANCHFS_BIN, run=run_with_env)

    def tearDown(self):
        # stop the daemon for this store so the tmpdir can be removed
        sock = os.path.join(self.root.store, "daemon.sock")
        if os.path.exists(sock):
            subprocess.run(
                [BRANCHFS_BIN, "list", "--storage", self.root.store],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # the daemon has no mounts in these tests; ask it to shut down
            # via its socket protocol (no CLI shutdown command on purpose)
            import json
            import socket as socketlib
            try:
                conn = socketlib.socket(socketlib.AF_UNIX,
                                        socketlib.SOCK_STREAM)
                conn.connect(sock)
                conn.sendall(json.dumps({"cmd": "shutdown"}).encode()
                             + b"\n")
                conn.close()
            except OSError:
                pass
        self.tmp.cleanup()

    def simulate_agent_write(self, relpath, content):
        delta = os.path.join(self.root.store, "branches", self.root.branch,
                             "files", relpath)
        os.makedirs(os.path.dirname(delta), exist_ok=True)
        with open(delta, "w") as fh:
            fh.write(content)

    def test_full_supervisor_cycle_against_real_daemon(self):
        self.cli.start_daemon(self.root)
        self.cli.create_branch(self.root)

        # lazy create: store must not contain a copy of the inherited tree
        inherited_copy = os.path.join(self.root.store, "branches",
                                      self.root.branch, "inherited",
                                      "Projects")
        self.assertFalse(os.path.exists(inherited_copy))

        # no changes yet
        self.assertEqual(self.cli.status(self.root), [])

        # agent writes land as deltas
        self.simulate_agent_write("Projects/result.txt", "artifact\n")
        changes = self.cli.status(self.root)
        paths = {c.path for c in changes}
        self.assertIn("/storage/user/Projects/result.txt", paths)
        file_changes = [c for c in changes if c.kind == "file"]
        self.assertTrue(all(c.op == "M" for c in file_changes))

        # freeze, then trusted commit applies to the real underlay
        self.cli.freeze(self.root)
        self.cli.commit(self.root)
        committed = os.path.join(self.root.base, "Projects", "result.txt")
        with open(committed) as fh:
            self.assertEqual(fh.read(), "artifact\n")

    def test_abort_leaves_underlay_untouched(self):
        self.cli.start_daemon(self.root)
        self.cli.create_branch(self.root)
        self.simulate_agent_write("junk.bin", "discard\n")
        self.cli.abort(self.root)
        self.assertFalse(os.path.exists(
            os.path.join(self.root.base, "junk.bin")))


if __name__ == "__main__":
    unittest.main()
