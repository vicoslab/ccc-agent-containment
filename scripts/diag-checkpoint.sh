#!/usr/bin/env bash
# Smoke-test branchfs `checkpoint-branch` on a LIVE FUSE mount: deltas land in
# base, the branch stays mounted+empty, and further writes keep working.
BRANCHFS=/storage/user/agent-workspace/conda-compute-cluster/worktrees/branchfs-agent-containment/target/debug/branchfs
LD_LIB=/home/domen/conda/envs/branchfs-dev/lib
T=/home/domen/ccc-checkpoint-test
BASE=$T/base STORE=$T/store MNT=$T/mnt
run() { LD_LIBRARY_PATH=$LD_LIB "$BRANCHFS" "$@"; }

cleanup() {
    /usr/local/bin/fusermount3 -u "$MNT" 2>/dev/null || true
    pkill -TERM -f "run-daemon --base $BASE" 2>/dev/null || true
    sleep 0.4
    rm -rf "$T"
}
trap cleanup EXIT

mkdir -p "$BASE/Projects/proj-a" "$STORE" "$MNT"
echo "orig" > "$BASE/Projects/proj-a/base.txt"

run start-daemon --base "$BASE" --storage "$STORE" >/dev/null
run create agent-ckpt --storage "$STORE" >/dev/null
run mount --storage "$STORE" --branch agent-ckpt --agent "$MNT" >/dev/null
echo "mounted: $(ls "$MNT")"

echo "=== turn 1: write in workspace, checkpoint ==="
echo "v1" > "$MNT/Projects/proj-a/a.txt"
run checkpoint-branch agent-ckpt --storage "$STORE"
echo "base has a.txt: $([ -f "$BASE/Projects/proj-a/a.txt" ] && cat "$BASE/Projects/proj-a/a.txt" || echo MISSING)"
echo "mount still alive: $(ls "$MNT/Projects/proj-a" 2>&1 | tr '\n' ' ')"
echo "status after checkpoint (want empty diff):"
run status agent-ckpt --storage "$STORE" --json 2>/dev/null \
  | python3 -c 'import sys,json;print("  diff=",[d["path"] for d in json.load(sys.stdin).get("diff",[])])'

echo "=== turn 2: branch continues — write again, checkpoint again ==="
echo "v2" > "$MNT/Projects/proj-a/b.txt"
echo "mount sees b.txt before checkpoint: $(cat "$MNT/Projects/proj-a/b.txt")"
run checkpoint-branch agent-ckpt --storage "$STORE"
echo "base has b.txt: $([ -f "$BASE/Projects/proj-a/b.txt" ] && cat "$BASE/Projects/proj-a/b.txt" || echo MISSING)"
echo "base still has a.txt: $([ -f "$BASE/Projects/proj-a/a.txt" ] && echo yes || echo MISSING)"

echo "=== read-through still works (inherited base.txt visible) ==="
echo "base.txt via mount: $(cat "$MNT/Projects/proj-a/base.txt")"
echo "DONE"
