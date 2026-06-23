#!/usr/bin/env bash
# End-to-end test of ccc-agent-run in 'none' confinement mode.
# Run from domen-cuda10 as uid 2094 (domen).

AGENT_DIR=/storage/user/agent-workspace/conda-compute-cluster/ccc-agent-containment
BRANCHFS=/storage/user/agent-workspace/conda-compute-cluster/worktrees/branchfs-agent-containment/target/debug/branchfs
LD_LIB=/home/domen/conda/envs/branchfs-dev/lib
TEST_BASE=/home/domen/ccc-e2e-test
STATE_DIR=$TEST_BASE/state
PROJ_BASE=$TEST_BASE/base
PROJ_STORE=$TEST_BASE/store
CONFIG_FILE=$TEST_BASE/config.json
PASS=0; FAIL=0

# IMPORTANT: inside CCC containers, FUSE (un)mount goes through the
# ccc-fuse-sidecar shim (fusermount3), not a raw umount syscall.  Always
# sidecar-unmount live views FIRST, then stop the daemon, then rm -- or a
# killed daemon leaves a stale "Transport endpoint is not connected" mount.
cleanup() {
    # 1. sidecar-unmount any branchfs view still mounted under the test tree
    for mp in $(mount 2>/dev/null | awk -v b="$TEST_BASE" '$3 ~ b {print $3}'); do
        /usr/local/bin/fusermount3 -u "$mp" 2>/dev/null || true
    done
    # 2. stop the daemon for this store (SIGTERM lets it drop sessions cleanly)
    pkill -TERM -f "run-daemon --base $PROJ_BASE" 2>/dev/null || true
    sleep 0.5
    pkill -KILL -f "run-daemon --base $PROJ_BASE" 2>/dev/null || true
    # 3. only now is it safe to remove the tree
    rm -rf "$TEST_BASE"
}
trap cleanup EXIT

mkdir -p "$PROJ_BASE/Projects/proj-a" "$PROJ_STORE" "$STATE_DIR"
echo "initial-content" > "$PROJ_BASE/Projects/proj-a/base.txt"
echo "export PS1=x" > "$PROJ_BASE/.bashrc"

cat > "$CONFIG_FILE" << EOF
{
  "state_dir": "$STATE_DIR",
  "branchfs_bin": "$BRANCHFS",
  "roots": [{"name": "proj_base", "base": "$PROJ_BASE",
              "store": "$PROJ_STORE",
              "visible": "/storage/user", "home_subdir": ""}],
  "user": "domen",
  "workspace": "/storage/user/Projects/proj-a",
  "policy": {"mode": "workspace-auto",
              "allowed_scopes": ["/storage/user/Projects/proj-a"]},
  "confinement": "none"
}
EOF

run_agent() {
    CCC_AGENT_BRANCHFS_BIN=$BRANCHFS \
    LD_LIBRARY_PATH=$LD_LIB \
    "$AGENT_DIR/bin/ccc-agent-run" \
        --config "$CONFIG_FILE" \
        --agent fake-agent \
        -- "$@" 2>&1
}

check() {
    local desc="$1"; local cond="$2"
    if eval "$cond"; then
        echo "PASS: $desc"; PASS=$((PASS+1))
    else
        echo "FAIL: $desc"; FAIL=$((FAIL+1))
    fi
}

echo "=== Test 1: in-scope write auto-commits ==="
# Agent CWD is inside FUSE mount at the workspace subdir; use relative path
run_agent sh -c 'echo "result" > result.txt'
check "result.txt committed to base" \
    '[ -f "$PROJ_BASE/Projects/proj-a/result.txt" ]'
[ -f "$PROJ_BASE/Projects/proj-a/result.txt" ] && \
    echo "  content: $(cat "$PROJ_BASE/Projects/proj-a/result.txt")"

echo ""
echo "=== Test 2: out-of-scope write pends review (via FUSE) ==="
# ../../escape.txt goes up two dirs from workspace inside the FUSE view
# stays inside FUSE mount but is outside allowed_scopes -> pending-review
run_agent sh -c 'echo "escaped" > ../../escape.txt' || true
check "escape.txt NOT committed to base (held for review)" \
    '[ ! -f "$PROJ_BASE/escape.txt" ]'

echo ""
echo "=== Test 3: no-op run aborts cleanly ==="
run_agent true
check "noop session aborted" \
    '[ "$(LD_LIBRARY_PATH=$LD_LIB CCC_AGENT_BRANCHFS_BIN=$BRANCHFS \
          "$AGENT_DIR/bin/ccc-agentctl" --config "$CONFIG_FILE" list 2>/dev/null \
          | grep aborted | wc -l)" -ge 1 ]'

echo ""
echo "=== Session list ==="
LD_LIBRARY_PATH=$LD_LIB CCC_AGENT_BRANCHFS_BIN=$BRANCHFS \
"$AGENT_DIR/bin/ccc-agentctl" --config "$CONFIG_FILE" list 2>&1 || true

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ]
