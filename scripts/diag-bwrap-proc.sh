#!/usr/bin/env bash
# Reproduce the bwrap fresh-proc EPERM and prove the bind-proc workaround.
BW=/home/domen/conda/envs/codex/bin/bwrap
[ -x "$BW" ] || BW=$(command -v bwrap)
echo "bwrap: $BW ($("$BW" --version))"

echo ""
echo "=== (1) your command: fresh --proc /proc ==="
"$BW" --unshare-user --uid 0 --gid 0 --unshare-pid --unshare-ipc --unshare-uts \
  --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
  /usr/bin/bash --noprofile --norc -c 'echo INNER_REACHED' 2>&1
echo "exit=$?"

echo ""
echo "=== (2) workaround: --ro-bind /proc /proc (bind container proc) ==="
"$BW" --unshare-user --uid 0 --gid 0 --unshare-pid --unshare-ipc --unshare-uts \
  --ro-bind / / --ro-bind /proc /proc --dev /dev --tmpfs /tmp \
  /usr/bin/bash --noprofile --norc -c \
  'id; touch /tmp/x && echo TMPFS_WRITE_OK; n=$(ls /proc | grep -c "^[0-9]"); echo proc_pids=$n; echo BIND_PROC_OK' 2>&1
echo "exit=$?"

echo ""
echo "=== (3) does dmesg show an AppArmor mount denial? (needs root) ==="
dmesg 2>/dev/null | grep -iE "apparmor.*(DENIED|mount)" | tail -4 \
  || echo "(dmesg restricted for uid $(id -u); run 'dmesg | grep -i apparmor' as root)"
