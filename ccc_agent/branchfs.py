"""BranchFS backends for the trusted supervisor.

``BranchfsCli`` drives the real ``branchfs`` binary (daemon-per-store model:
the unix socket lives inside the store directory).  ``FakeBranchFS`` is a
filesystem-level simulation used by non-FUSE tests: the "mounted view" is the
branch delta directory itself, which is behaviorally adequate for exercising
the supervisor's orchestration, policy, and artifact logic.

Both backends speak in terms of :class:`ccc_agent.session.ProtectedRoot`.
"""

import json
import os
import shutil
import subprocess

from .policy import Change


class BranchfsError(Exception):
    pass


def _run_subprocess(argv):
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _changes_from_status(data, root):
    """Map a `branchfs status --json` document to Change objects in the
    agent-visible namespace."""
    changes = []
    for entry in data.get("diff", ()):
        relpath = entry.get("path", "")
        visible = root.visible.rstrip("/") + "/" + relpath.lstrip("/")
        op = "D" if entry.get("op") == "delete" else "M"
        changes.append(Change(op=op, path=visible,
                              kind=entry.get("kind", "file"),
                              bytes=entry.get("bytes", 0), root=root.name))
    return changes


class BranchfsCli(object):
    """Drives the branchfs CLI; one daemon per protected root (per store)."""

    def __init__(self, binary="branchfs", run=_run_subprocess):
        self.binary = binary
        self._run = run

    def _invoke(self, *argv):
        code, out, err = self._run([self.binary] + [str(a) for a in argv])
        if code != 0:
            raise BranchfsError("%s %s failed (%d): %s"
                                % (self.binary, argv[0], code,
                                   err.strip() or out.strip()))
        return out

    def start_daemon(self, root):
        # Idempotent: the daemon auto-exits when its last mount goes away,
        # so every daemon-dependent operation re-ensures it first (e.g.
        # `ccc-agentctl commit` long after the agent session unmounted).
        self._invoke("start-daemon", "--base", root.base,
                     "--storage", root.store)

    def create_branch(self, root, parent="main"):
        self.start_daemon(root)
        argv = ["create", root.branch, "--parent", parent,
                "--storage", root.store]
        for hidden in getattr(root, "hide_paths", None) or ():
            argv.extend(["--hide", hidden])
        self._invoke(*argv)

    def mount(self, root, agent=True, allow_other=False):
        self.start_daemon(root)
        argv = ["mount", "--storage", root.store, "--branch", root.branch]
        if agent:
            argv.append("--agent")
        if allow_other:
            # Privilege-separated chroot model: the root daemon mounts a view
            # the non-root agent uid must access.  Without allow_other FUSE
            # denies any uid but the mounting (root) one.
            argv.append("--allow-other")
        argv.append(root.mount)
        self._invoke(*argv)

    def unmount(self, root):
        self._invoke("unmount", root.mount, "--storage", root.store)

    def freeze(self, root):
        self.start_daemon(root)
        self._invoke("freeze", root.branch, "--storage", root.store)

    def thaw(self, root):
        self.start_daemon(root)
        self._invoke("thaw", root.branch, "--storage", root.store)

    def status(self, root):
        self.start_daemon(root)
        out = self._invoke("status", root.branch, "--storage", root.store,
                           "--json")
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise BranchfsError("unparseable status output: %s" % exc)
        return _changes_from_status(data, root)

    def commit(self, root):
        self.start_daemon(root)
        self._invoke("commit-branch", root.branch, "--storage", root.store)

    def abort(self, root):
        self.start_daemon(root)
        self._invoke("abort-branch", root.branch, "--storage", root.store)


class FakeBranchFS(object):
    """Non-FUSE stand-in: the mount *is* the delta directory.

    Reads do not fall through to base (unlike real BranchFS), which is fine
    for supervisor tests: they only assert on orchestration, status, policy,
    commit, and abort behavior.
    """

    def __init__(self):
        self._state = {}      # (store, branch) -> "open" | "frozen"
        self._deletes = {}    # (store, branch) -> set(relpath)
        self._mounted = {}    # mount -> (store, branch)

    # -- helpers -----------------------------------------------------------
    def _key(self, root):
        return (root.store, root.branch)

    def _files_dir(self, root):
        return os.path.join(root.store, "branches", root.branch, "files")

    def branch_state(self, root):
        return self._state.get(self._key(root), "open")

    def record_delete(self, root, relpath):
        """Simulate the agent deleting an inherited path (tombstone)."""
        self._deletes.setdefault(self._key(root), set()).add(relpath)

    # -- backend API --------------------------------------------------------
    def start_daemon(self, root):
        os.makedirs(root.store, exist_ok=True)

    def create_branch(self, root, parent="main"):
        os.makedirs(self._files_dir(root), exist_ok=True)
        self._state[self._key(root)] = "open"
        self._deletes.setdefault(self._key(root), set())

    def mount(self, root, agent=True, allow_other=False):
        files = self._files_dir(root)
        os.makedirs(files, exist_ok=True)
        parent = os.path.dirname(root.mount)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.islink(root.mount) and not os.path.exists(root.mount):
            os.symlink(files, root.mount)
        self._mounted[root.mount] = self._key(root)

    def unmount(self, root):
        self._mounted.pop(root.mount, None)
        if os.path.islink(root.mount):
            os.unlink(root.mount)

    def freeze(self, root):
        self._state[self._key(root)] = "frozen"

    def thaw(self, root):
        self._state[self._key(root)] = "open"

    def status(self, root):
        files = self._files_dir(root)
        diff = []
        if os.path.isdir(files):
            for dirpath, _dirnames, filenames in os.walk(files):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, files)
                    diff.append({"op": "delta", "path": rel, "kind": "file",
                                 "bytes": os.path.getsize(full)})
        for rel in sorted(self._deletes.get(self._key(root), ())):
            diff.append({"op": "delete", "path": rel, "kind": "tombstone",
                         "bytes": 0})
        return _changes_from_status({"diff": diff}, root)

    def commit(self, root):
        files = self._files_dir(root)
        if os.path.isdir(files):
            for dirpath, _dirnames, filenames in os.walk(files):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, files)
                    dest = os.path.join(root.base, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(full, dest)
        for rel in self._deletes.get(self._key(root), ()):
            target = os.path.join(root.base, rel)
            if os.path.isfile(target) or os.path.islink(target):
                os.unlink(target)
            elif os.path.isdir(target):
                shutil.rmtree(target)
        self._cleanup(root)

    def abort(self, root):
        self._cleanup(root)

    def _cleanup(self, root):
        files = self._files_dir(root)
        if os.path.isdir(files):
            shutil.rmtree(files)
        os.makedirs(files, exist_ok=True)
        self._deletes[self._key(root)] = set()
