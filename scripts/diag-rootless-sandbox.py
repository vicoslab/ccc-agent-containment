#!/usr/bin/env python3
"""Prove a FULL rootless sandbox (bwrap technique) with zero container caps:
user+mount+pid namespaces, recursive binds, fresh /proc, pivot_root, then run
an escape-probe.  This is what ccc-agent-chroot.sh should become.

  python3 diag-rootless-sandbox.py
"""
import ctypes, ctypes.util, errno, os, sys, subprocess

MS_BIND = 0x1000; MS_REC = 0x4000; MS_PRIVATE = 0x40000
MS_NOSUID = 0x2; MS_NODEV = 0x4; MS_NOEXEC = 0x8
NR_pivot_root = 155
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def mount(src, tgt, fs, flags, data=None):
    r = libc.mount(src.encode() if src else None,
                   tgt.encode() if tgt else None,
                   fs.encode() if fs else None, flags,
                   data.encode() if data else None)
    if r != 0:
        e = ctypes.get_errno()
        raise OSError(e, "mount(%s->%s): %s" % (src, tgt, errno.errorcode.get(e, e)))


def inside():
    root = "/dev/shm/rootless-root"
    os.makedirs(root, exist_ok=True)
    mount(None, "/", None, MS_REC | MS_PRIVATE)
    mount("tmpfs", root, "tmpfs", 0)
    # recursive read-only-ish system dirs (ro applied via remount in real impl)
    for d in ("usr", "bin", "sbin", "lib", "lib64", "etc", "opt"):
        src = "/" + d
        if os.path.islink(src) or not os.path.exists(src):
            continue
        dst = os.path.join(root, d)
        os.makedirs(dst, exist_ok=True)
        mount(src, dst, None, MS_BIND | MS_REC)
    # the agent workspace view (here: real /storage/user stands in for the
    # branchfs FUSE view, which is itself a submount carried by MS_REC)
    ws = os.path.join(root, "storage/user")
    os.makedirs(ws, exist_ok=True)
    mount("/storage/user", ws, None, MS_BIND | MS_REC)
    # fresh /proc -- only possible because we unshared the PID namespace.
    # In Docker the proc masks are LOCKED so a fresh proc is "too revealing"
    # (EPERM); fall back to rec-binding the existing (masked) /proc.
    procdst = os.path.join(root, "proc")
    os.makedirs(procdst, exist_ok=True)
    proc_mode = "fresh"
    try:
        mount("proc", procdst, "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    except OSError:
        proc_mode = "rec-bind-host"
        mount("/proc", procdst, None, MS_BIND | MS_REC)
    globals()["_proc_mode"] = proc_mode
    # tmpfs /tmp and minimal /dev
    for d, fl in (("tmp", MS_NOSUID | MS_NODEV), ("dev", MS_NOSUID)):
        p = os.path.join(root, d); os.makedirs(p, exist_ok=True)
        mount("tmpfs", p, "tmpfs", fl)
    # pivot_root
    os.makedirs(os.path.join(root, "oldroot"), exist_ok=True)
    if libc.syscall(NR_pivot_root, root.encode(),
                    os.path.join(root, "oldroot").encode()) != 0:
        e = ctypes.get_errno()
        raise OSError(e, "pivot_root: %s" % errno.errorcode.get(e, e))
    os.chdir("/")
    # detach the old root so it cannot be reached
    libc.umount2(b"/oldroot", 2)  # MNT_DETACH
    os.rmdir("/oldroot")

    # ---- escape probe ----
    print("=== ESCAPE PROBE (uid=%d inside ns, proc=%s) ==="
          % (os.getuid(), globals().get("_proc_mode")))
    pids = sorted((int(p) for p in os.listdir("/proc") if p.isdigit()))
    print("  PIDs visible in /proc: count=%d max=%d (PID-ns control isolation "
          "holds regardless; listing leak only if host PIDs show)"
          % (len(pids), max(pids)))
    # can we read another host process's cmdline? (info leak on shared host)
    leak_cmdline = 0; denied = 0
    for p in pids:
        try:
            with open("/proc/%d/cmdline" % p, "rb") as fh:
                if fh.read().strip():
                    leak_cmdline += 1
        except OSError:
            denied += 1
    print("  other-process cmdline readable: %d readable / %d denied" %
          (leak_cmdline, denied))
    # can we actually signal a host process? (control, not just info)
    can_signal = False
    for p in pids:
        if p not in (1, os.getpid()):
            try:
                os.kill(p, 0); can_signal = True
            except OSError:
                pass
            break
    print("  can signal a non-self host PID: %s"
          % ("yes-LEAK" if can_signal else "no-OK"))
    print("  /proc/1/root reachable: %s"
          % ("yes-LEAK" if _can_stat("/proc/1/root/etc/hostname") else "no-OK"))
    print("  real underlay hidden (/oldroot gone): %s"
          % ("OK" if not os.path.exists("/oldroot") else "LEAK"))
    print("  /storage/user is the view: %s"
          % ("OK" if os.path.isdir("/storage/user") else "MISSING"))
    print("  /bin/bash present: %s" % os.path.exists("/bin/bash"))
    print("  >>> ROOTLESS SANDBOX ASSEMBLED, zero container CAP_SYS_ADMIN <<<")


def _can_stat(p):
    try:
        os.stat(p); return True
    except OSError:
        return False


if __name__ == "__main__":
    if os.environ.get("ROOTLESS_INSIDE") == "1":
        inside()
    else:
        env = dict(os.environ, ROOTLESS_INSIDE="1")
        # --fork so the unshared-PID child is reaped properly; --mount-proc is
        # NOT used (we mount proc ourselves after pivot_root)
        sys.exit(subprocess.call(
            ["unshare", "--user", "--mount", "--pid", "--fork",
             "--map-root-user", sys.executable, os.path.abspath(__file__)],
            env=env))
