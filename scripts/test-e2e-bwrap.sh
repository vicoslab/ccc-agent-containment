#!/usr/bin/env bash
# End-to-end test of ccc-agent-run in 'bwrap' confinement on real FUSE + real
# bwrap.  Run from donbot-domen-cuda10 as uid 2094 (domen).
AGENT_DIR=/storage/user/agent-workspace/conda-compute-cluster/ccc-agent-containment
BRANCHFS=/storage/user/agent-workspace/conda-compute-cluster/worktrees/branchfs-agent-containment/target/debug/branchfs
LD_LIB=/home/domen/conda/envs/branchfs-dev/lib
BWRAP=/home/domen/conda/envs/codex/bin/bwrap
TEST_BASE=/home/domen/ccc-e2e-bwrap
STATE_DIR=$TEST_BASE/state
PROJ_BASE=$TEST_BASE/base
PROJ_STORE=$TEST_BASE/store
CONFIG_FILE=$TEST_BASE/config.json
PASS=0; FAIL=0

cleanup() {
    # sidecar-unmount any live view first, then stop daemon, then rm (FUSE shim)
    for mp in $(mount 2>/dev/null | awk -v b="$TEST_BASE" '$3 ~ b {print $3}'); do
        /usr/local/bin/fusermount3 -u "$mp" 2>/dev/null || true
    done
    pkill -TERM -f "run-daemon --base $PROJ_BASE" 2>/dev/null || true
    sleep 0.5
    pkill -KILL -f "run-daemon --base $PROJ_BASE" 2>/dev/null || true
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
  "bwrap_bin": "$BWRAP",
  "bwrap_proc_mode": "bind",
  "confinement": "bwrap",
  "roots": [{"name": "proj_base", "base": "$PROJ_BASE",
              "store": "$PROJ_STORE",
              "visible": "/storage/user", "home_subdir": ""}],
  "user": "domen",
  "workspace": "/storage/user/Projects/proj-a",
  "policy": {"mode": "workspace-auto",
              "allowed_scopes": ["/storage/user/Projects/proj-a"]}
}
EOF

run_agent() {
    CCC_AGENT_BRANCHFS_BIN=$BRANCHFS LD_LIBRARY_PATH=$LD_LIB \
    "$AGENT_DIR/bin/ccc-agent-run" --config "$CONFIG_FILE" --agent fake-agent \
        -- "$@" 2>&1
}
check() {
    if eval "$2"; then echo "PASS: $1"; PASS=$((PASS+1));
    else echo "FAIL: $1"; FAIL=$((FAIL+1)); fi
}

echo "=== Test 1: escape probe + in-scope write inside the sandbox ==="
# Inside the sandbox: cwd is the workspace (the view); real underlay/store
# must be invisible; write into cwd to be auto-committed.
run_agent sh -c '
  echo "pwd=$(pwd)"
  echo "ws-files=$(ls)"
  echo "underlay-hidden=$([ -e '"$PROJ_BASE"'/Projects/proj-a/base.txt ] && echo NO-LEAK || echo OK)"
  echo "store-hidden=$([ -e '"$PROJ_STORE"' ] && echo NO-LEAK || echo OK)"
  echo "home=$HOME user=$USER"
  echo "result" > result.txt
'
check "result.txt auto-committed to base" \
    '[ -f "$PROJ_BASE/Projects/proj-a/result.txt" ]'

echo ""
echo "=== Test 2: out-of-scope write (inside view, outside scope) pends ==="
run_agent sh -c 'echo escaped > /storage/user/escape.txt' || true
check "escape.txt NOT committed to base (held for review)" \
    '[ ! -f "$PROJ_BASE/escape.txt" ]'

echo ""
echo "=== Test 3: system dirs are read-only in the sandbox ==="
run_agent sh -c 'touch /usr/should-fail 2>&1; echo rc=$?' | grep -q "rc=[^0]" \
    && { echo "PASS: /usr is read-only"; PASS=$((PASS+1)); } \
    || { echo "FAIL: /usr was writable"; FAIL=$((FAIL+1)); }

echo ""
echo "=== Session list ==="
LD_LIBRARY_PATH=$LD_LIB CCC_AGENT_BRANCHFS_BIN=$BRANCHFS \
"$AGENT_DIR/bin/ccc-agentctl" --config "$CONFIG_FILE" list 2>&1 || true

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ]
