#!/usr/bin/env python3
"""Diagnose WHY bind mounts fail inside a user namespace on this container.

Distinguishes a capability problem (EPERM) from a syscall-availability problem
(new mount API blocked by seccomp while classic mount(2) is allowed) from a
filesystem/locked-mount problem (EINVAL on cross-userns rebind).

Run TWO ways:
  python3 diag-userns-mount.py            # re-execs itself inside userns
  (it calls unshare --user --mount --map-root-user on itself)
"""
import ctypes, ctypes.util, errno, os, sys, subprocess

MS_BIND = 0x1000
MS_REC = 0x4000
MS_PRIVATE = 0x40000

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def mount(source, target, fstype, flags, data):
    s = source.encode() if source else None
    t = target.encode() if target else None
    f = fstype.encode() if fstype else None
    d = data.encode() if data else None
    r = libc.mount(s, t, f, flags, d)
    return r, ctypes.get_errno()


def errname(e):
    return errno.errorcode.get(e, "errno %d" % e)


def try_bind(label, src, dst):
    os.makedirs(dst, exist_ok=True)
    r, e = mount(src, dst, None, MS_BIND, None)
    if r == 0:
        ok = os.path.exists(dst) and bool(os.listdir(dst))
        print("  raw mount(2) MS_BIND %-14s -> OK (visible=%s)" % (label, ok))
        libc.umount(dst.encode())
    else:
        print("  raw mount(2) MS_BIND %-14s -> FAIL errno=%s (%d)"
              % (label, errname(e), e))


def try_new_api(label, src, dst):
    """Test the new mount API path (open_tree + move_mount) raw via syscall."""
    OPEN_TREE_CLONE = 1
    AT_RECURSIVE = 0x8000
    MOVE_MOUNT_F_EMPTY_PATH = 0x4
    NR_open_tree = 428
    NR_move_mount = 429
    os.makedirs(dst, exist_ok=True)
    fd = libc.syscall(NR_open_tree, -100, src.encode(),
                      OPEN_TREE_CLONE | AT_RECURSIVE)
    if fd < 0:
        print("  new-API open_tree   %-14s -> FAIL errno=%s"
              % (label, errname(ctypes.get_errno())))
        return
    r = libc.syscall(NR_move_mount, fd, b"", -100, dst.encode(),
                     MOVE_MOUNT_F_EMPTY_PATH)
    if r < 0:
        print("  new-API move_mount  %-14s -> FAIL errno=%s"
              % (label, errname(ctypes.get_errno())))
    else:
        print("  new-API open_tree+move_mount %-7s -> OK" % label)
        libc.umount(dst.encode())
    os.close(fd)


def inside():
    print("=== inside userns: uid=%d euid=%d ===" % (os.getuid(), os.geteuid()))
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("CapEff"):
                print("  " + line.strip())
    # make / private so binds don't propagate (what bwrap does)
    r, e = mount(None, "/", None, MS_REC | MS_PRIVATE, None)
    print("  make-rprivate / -> %s" % ("OK" if r == 0 else errname(e)))

    os.makedirs("/dev/shm/diag-src", exist_ok=True)
    open("/dev/shm/diag-src/probe", "w").close()

    print("-- classic mount(2) --")
    try_bind("tmpfs", "/dev/shm/diag-src", "/dev/shm/diag-dst-tmpfs")
    try_bind("overlay(/usr)", "/usr", "/dev/shm/diag-dst-usr")
    try_bind("nfs(/storage)", "/storage/user", "/dev/shm/diag-dst-nfs")

    print("-- recursive classic mount(2) (MS_REC, what bwrap uses) --")
    try_bind_rec("overlay(/usr)", "/usr", "/dev/shm/diag-rec-usr")
    try_bind_rec("nfs(/storage)", "/storage/user", "/dev/shm/diag-rec-nfs")

    print("-- new mount API (open_tree/move_mount) --")
    try_new_api("tmpfs", "/dev/shm/diag-src", "/dev/shm/diag-nt-tmpfs")
    try_new_api("overlay(/usr)", "/usr", "/dev/shm/diag-nt-usr")

    print("-- full bwrap-style assembly (tmpfs root + rec-bind + pivot_root) --")
    bwrap_style()

    for d in ("tmpfs", "usr", "nfs"):
        for p in ("/dev/shm/diag-dst-%s" % d, "/dev/shm/diag-rec-%s" % d,
                  "/dev/shm/diag-nt-%s" % d):
            try:
                os.rmdir(p)
            except OSError:
                pass


def try_bind_rec(label, src, dst):
    os.makedirs(dst, exist_ok=True)
    r, e = mount(src, dst, None, MS_BIND | MS_REC, None)
    if r == 0:
        ok = os.path.exists(dst) and bool(os.listdir(dst))
        print("  rec mount(2) MS_REC  %-14s -> OK (visible=%s)" % (label, ok))
        libc.umount2(dst.encode(), 2)  # MNT_DETACH
    else:
        print("  rec mount(2) MS_REC  %-14s -> FAIL errno=%s (%d)"
              % (label, errname(e), e))


def bwrap_style():
    """Replicate bwrap: fresh tmpfs newroot, rec-bind system dirs, pivot_root."""
    NR_pivot_root = 155
    newroot = "/dev/shm/diag-newroot"
    os.makedirs(newroot, exist_ok=True)
    r, e = mount("tmpfs", newroot, "tmpfs", 0, None)
    if r != 0:
        print("  tmpfs newroot -> FAIL errno=%s" % errname(e))
        return
    binds = [("/usr", "usr"), ("/bin", "bin"), ("/lib", "lib"),
             ("/lib64", "lib64"), ("/etc", "etc")]
    failures = []
    for src, name in binds:
        if not os.path.exists(src):
            continue
        dst = os.path.join(newroot, name)
        os.makedirs(dst, exist_ok=True)
        r, e = mount(src, dst, None, MS_BIND | MS_REC, None)
        if r != 0:
            failures.append("%s=%s" % (name, errname(e)))
    if failures:
        print("  rec-bind system dirs -> FAIL: %s" % ", ".join(failures))
        libc.umount2(newroot.encode(), 2)
        return
    print("  rec-bind system dirs -> OK (%d dirs)"
          % len([b for b in binds if os.path.exists(b[0])]))
    # bind a workspace (NFS) into the new root
    wsdst = os.path.join(newroot, "workspace")
    os.makedirs(wsdst, exist_ok=True)
    r, e = mount("/storage/user", wsdst, None, MS_BIND | MS_REC, None)
    print("  rec-bind /storage/user -> %s" % ("OK" if r == 0 else errname(e)))
    # pivot_root
    os.makedirs(os.path.join(newroot, "oldroot"), exist_ok=True)
    rc = libc.syscall(NR_pivot_root, newroot.encode(),
                      os.path.join(newroot, "oldroot").encode())
    if rc != 0:
        print("  pivot_root -> FAIL errno=%s" % errname(ctypes.get_errno()))
        return
    os.chdir("/")
    has_bash = os.path.exists("/bin/bash") or os.path.exists("/usr/bin/bash")
    has_ws = os.path.isdir("/workspace")
    print("  pivot_root -> OK; /bin/bash=%s /workspace=%s" % (has_bash, has_ws))
    print("  >>> bwrap-style sandbox ASSEMBLED with zero real CAP_SYS_ADMIN <<<")


if __name__ == "__main__":
    if os.environ.get("DIAG_INSIDE") == "1":
        inside()
    else:
        env = dict(os.environ, DIAG_INSIDE="1")
        sys.exit(subprocess.call(
            ["unshare", "--user", "--mount", "--map-root-user",
             sys.executable, os.path.abspath(__file__)], env=env))
