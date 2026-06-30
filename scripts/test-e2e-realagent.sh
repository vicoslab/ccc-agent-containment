#!/usr/bin/env bash
# Real-agent e2e: run a minimal codex (and optionally claude) turn through
# ccc-agent run inside bwrap, and verify the file it creates lands in the base.
# Guarded: no-ops if creds/binaries are absent. Uses ONE tiny LLM turn.
#
# This exercises the `exec`/`-p` (process-exit) path: one turn per process, so
# the supervisor's end-of-process finalize is the per-turn commit. (The
# interactive Stop-hook/notify path is covered by test-e2e-perturn.sh + units.)
REPO=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)
BRANCHFS=/storage/user/agent-workspace/conda-compute-cluster/worktrees/branchfs-agent-containment/target/debug/branchfs
LD_LIB=/home/domen/conda/envs/branchfs-dev/lib
BWRAP=/home/domen/conda/envs/codex/bin/bwrap
CODEX_ENV=/storage/user/conda-envs/codex
T=/home/domen/ccc-e2e-realagent
PROJ_BASE=$T/base STORE=$T/store STATE=$T/state CFG=$T/cfg.json

cleanup() {
    for mp in $(mount 2>/dev/null | awk -v b="$T" '$3 ~ b {print $3}'); do
        /usr/local/bin/fusermount3 -u "$mp" 2>/dev/null || true
    done
    pkill -TERM -f "run-daemon --base $PROJ_BASE" 2>/dev/null || true
    sleep 0.4; rm -rf "$T"
}
trap cleanup EXIT

if [ ! -f /home/domen/.codex/auth.json ] || [ ! -x "$CODEX_ENV/bin/codex" ]; then
    echo "SKIP: codex creds/binary not available"; exit 0
fi

mkdir -p "$PROJ_BASE/Projects/proj-a" "$STORE" "$STATE" "$PROJ_BASE/.codex"
# Seed the base home with codex auth+config (production base == real home, so
# this mirrors that: codex reads its OAuth tokens from the view and writes its
# session logs there too).
cp /home/domen/.codex/auth.json "$PROJ_BASE/.codex/" 2>/dev/null || true
cp /home/domen/.codex/config.toml "$PROJ_BASE/.codex/" 2>/dev/null || true

cat > "$CFG" <<EOF
{"state_dir":"$STATE","branchfs_bin":"$BRANCHFS","bwrap_bin":"$BWRAP",
 "confinement":"bwrap","bwrap_proc_mode":"bind","user":"domen",
 "workspace":"/storage/user/Projects/proj-a",
 "bwrap_ro_binds":["$CODEX_ENV"],
 "roots":[{"name":"r","base":"$PROJ_BASE","store":"$STORE","visible":"/storage/user","home_subdir":""}],
 "policy":{"mode":"workspace-auto","allowed_scopes":["/storage/user/Projects/proj-a"],
           "ignore_patterns":["/storage/user/conda-envs","/storage/user/.codex"]}}
EOF

echo "=== real codex exec turn (in bwrap) ==="
CCC_AGENT_BRANCHFS_BIN=$BRANCHFS LD_LIBRARY_PATH=$LD_LIB \
timeout 120 "$REPO/bin/ccc-agent" run --config "$CFG" --agent codex \
  -- "$CODEX_ENV/bin/codex" exec --dangerously-bypass-approvals-and-sandbox \
     --skip-git-repo-check "Create a file named hello.txt containing exactly: hi" \
  2>&1 | tail -25

echo ""
echo "=== out-of-scope flagged this session (why pending-review) ==="
for pd in "$STATE"/reviews/*/policy-decision.json; do
    [ -f "$pd" ] && python3 -c "import json,sys; d=json.load(open('$pd')); print('decision:', d['decision']); print('out_of_scope:', d.get('out_of_scope')); print('deny:', [m.get('path') for m in d.get('deny_matches',[])])"
done
echo ""
if [ -f "$PROJ_BASE/Projects/proj-a/hello.txt" ]; then
    echo "PASS: codex created+committed hello.txt -> $(cat "$PROJ_BASE/Projects/proj-a/hello.txt")"
else
    echo "FAIL/GAP: hello.txt not in base (see output above for the reason)"
fi
echo "=== leak check ==="
pgrep -af "run-daemon --base $PROJ_BASE" | grep -v grep || echo "daemons: (none)"
