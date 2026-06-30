#!/usr/bin/env bash
# Validate the PATH-shim interception path with a REAL agent binary:
#   shim (codex) -> ccc-agent run -> bwrap+branchfs -> real codex inside.
# Uses `codex --version` so no LLM/auth is needed; proves the mechanics only.
AGENT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)
BRANCHFS=/storage/user/agent-workspace/conda-compute-cluster/worktrees/branchfs-agent-containment/target/debug/branchfs
LD_LIB=/home/domen/conda/envs/branchfs-dev/lib
BWRAP=/home/domen/conda/envs/codex/bin/bwrap
REAL_CODEX=/home/domen/.local/bin/codex
T=/home/domen/ccc-intercept-test

cleanup() {
    for mp in $(mount 2>/dev/null | awk -v b="$T" '$3 ~ b {print $3}'); do
        /usr/local/bin/fusermount3 -u "$mp" 2>/dev/null || true
    done
    pkill -TERM -f "run-daemon --base $T/base" 2>/dev/null || true
    sleep 0.4
    rm -rf "$T"
}
trap cleanup EXIT

mkdir -p "$T/base/Projects/proj-a" "$T/store" "$T/state" "$T/shims"
echo base > "$T/base/Projects/proj-a/base.txt"

cat > "$T/cfg.json" <<EOF
{"state_dir":"$T/state","branchfs_bin":"$BRANCHFS","bwrap_bin":"$BWRAP",
 "confinement":"bwrap","bwrap_proc_mode":"bind","user":"domen",
 "workspace":"/storage/user/Projects/proj-a",
 "bwrap_ro_binds":["/home/domen/.local","/storage/user/conda-envs/codex","/home/domen/.codex"],
 "roots":[{"name":"r","base":"$T/base","store":"$T/store","visible":"/storage/user","home_subdir":""}],
 "policy":{"mode":"workspace-auto","allowed_scopes":["/storage/user/Projects/proj-a"]}}
EOF

# Install the shim as "codex" ahead of the real binary on PATH.
ln -sf "$AGENT_DIR/ccc_agent/assets/shims/ccc-agent-shim.sh" "$T/shims/codex"

echo "=== invoke 'codex --version' through the shim ==="
PATH="$T/shims:$(dirname "$REAL_CODEX"):$PATH" \
CCC_AGENT_CONFIG="$T/cfg.json" \
CCC_AGENT_CLI="$AGENT_DIR/bin/ccc-agent" \
CCC_AGENT_BRANCHFS_BIN="$BRANCHFS" \
LD_LIBRARY_PATH="$LD_LIB" \
    codex --version 2>&1 | sed 's/^/  /'

echo ""
echo "=== session recorded? (proves it went through ccc-agent run) ==="
LD_LIBRARY_PATH=$LD_LIB CCC_AGENT_BRANCHFS_BIN=$BRANCHFS \
"$AGENT_DIR/bin/ccc-agent" --config "$T/cfg.json" list 2>&1 | sed 's/^/  /'
