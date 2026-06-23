#!/usr/bin/env python3
"""Test fd-based bind mount trick inside user namespace (bwrap technique)."""
import os, subprocess, shutil, sys

test_dir = "/home/domen/ccc-fd-test"
shutil.rmtree(test_dir, ignore_errors=True)

# Open fds to source dirs BEFORE entering user namespace
fds = {}
for name, path in [("usr", "/usr"), ("storage", "/storage/user")]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    fds[name] = fd
    print(f"opened {name}={path} as fd {fd}")

os.makedirs(test_dir + "/usr", exist_ok=True)
os.makedirs(test_dir + "/storage", exist_ok=True)

script = """
set -e
echo "inside user namespace, uid=$(id -u)"
mount --bind /proc/self/fd/{usr_fd} {dst}/usr
echo bind-usr-via-fd=$?
ls {dst}/usr/bin/bash && echo "usr-bash-ok"
/bin/umount {dst}/usr 2>/dev/null || true

mount --bind /proc/self/fd/{storage_fd} {dst}/storage
echo bind-storage-via-fd=$?
ls {dst}/storage/ | head -3
/bin/umount {dst}/storage 2>/dev/null || true
""".format(usr_fd=fds["usr"], storage_fd=fds["storage"], dst=test_dir)

proc = subprocess.run(
    ["unshare", "--mount", "--map-root-user", "bash", "-c", script],
    pass_fds=tuple(fds.values()),
    capture_output=False,
)
print(f"exit={proc.returncode}")

for fd in fds.values():
    try:
        os.close(fd)
    except Exception:
        pass
shutil.rmtree(test_dir, ignore_errors=True)
