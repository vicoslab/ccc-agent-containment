#!/usr/bin/env python3
"""Distinguish WHY a fresh proc mount fails in a userns: kernel
mount_too_revealing (locked /proc masks) vs AppArmor profile vs seccomp.

Signature reading:
  proc FAIL + sysfs FAIL + tmpfs OK + fresh-proc-at-new-path also FAIL
    -> kernel mount_too_revealing (the locked masks). Fix = systempaths/MaskedPaths.
  proc FAIL but sysfs OK
    -> AppArmor profile missing an fstype=proc rule. Fix = the profile.
  rec-bind /proc OK in all cases -> the standard workaround.
"""
import ctypes, ctypes.util, errno, os, sys, subprocess

MS_BIND = 0x1000; MS_REC = 0x4000; MS_PRIVATE = 0x40000
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def mount(src, tgt, fs, flags):
    r = libc.mount(src.encode() if src else None, tgt.encode(),
                   fs.encode() if fs else None, flags, None)
    return (0, 0) if r == 0 else (r, ctypes.get_errno())


def en(e):
    return errno.errorcode.get(e, "errno %d" % e)


def attempt(label, src, fs, flags, tgt):
    os.makedirs(tgt, exist_ok=True)
    r, e = mount(src, tgt, fs, flags)
    if r == 0:
        print("  %-34s -> OK" % label)
        libc.umount2(tgt.encode(), 2)
    else:
        print("  %-34s -> FAIL %s" % (label, en(e)))
    return r == 0


def inside():
    print("=== inside userns uid=%d; AppArmor profile: %s ==="
          % (os.getuid(), _aa_profile()))
    mount(None, "/", None, MS_PRIVATE | MS_REC)
    attempt("fresh tmpfs  @ /dev/shm/t",  "tmpfs", "tmpfs", 0, "/dev/shm/dt")
    attempt("fresh proc   @ /dev/shm/p",  "proc",  "proc",  0, "/dev/shm/dp")
    attempt("fresh sysfs  @ /dev/shm/s",  "sysfs", "sysfs", 0, "/dev/shm/ds")
    attempt("fresh proc   @ /proc (over)", "proc", "proc",  0, "/proc")
    attempt("rec-bind /proc @ /dev/shm/bp", "/proc", None, MS_BIND | MS_REC,
            "/dev/shm/dbp")
    for d in ("dt", "dp", "ds", "dbp"):
        try:
            os.rmdir("/dev/shm/" + d)
        except OSError:
            pass


def _aa_profile():
    for p in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            with open(p) as fh:
                return fh.read().strip()
        except OSError:
            continue
    return "(none)"


if __name__ == "__main__":
    if os.environ.get("PROC_INSIDE") == "1":
        inside()
    else:
        env = dict(os.environ, PROC_INSIDE="1")
        sys.exit(subprocess.call(
            ["unshare", "--user", "--mount", "--map-root-user",
             sys.executable, os.path.abspath(__file__)], env=env))
