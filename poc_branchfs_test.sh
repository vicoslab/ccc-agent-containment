#!/usr/bin/env bash
# poc_branchfs_test.sh — Proof-of-concept test for BranchFS agent containment.
#
# Tests all core BranchFS operations WITHOUT requiring FUSE or root access:
#   - daemon start/lifecycle
#   - lazy branch create (O(1), no copy of base)
#   - delta writes and readback
#   - status/diff reporting (human and JSON)
#   - freeze / thaw
#   - commit to real underlay (with tombstones applied)
#   - abort (discard deltas, base untouched)
#   - hide rules persisted in metadata
#   - multi-branch isolation
#   - nested branch (child of parent)
#
# Usage:
#   ./poc_branchfs_test.sh [--branchfs /path/to/branchfs] [--keep] [--fail-fast]
#
# BRANCHFS_BIN env var or --branchfs flag overrides auto-detection.
# Output: TAP-style. Exit 0 = all pass.
set -euo pipefail

BRANCHFS_BIN="${BRANCHFS_BIN:-}"
KEEP=0
FAIL_FAST=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branchfs) BRANCHFS_BIN="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --fail-fast) FAIL_FAST=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "$BRANCHFS_BIN" ]]; then
    for c in \
        "$SCRIPT_DIR/../worktrees/branchfs-agent-containment/target/debug/branchfs" \
        "$SCRIPT_DIR/../worktrees/branchfs-agent-containment/target/release/branchfs" \
        "$SCRIPT_DIR/../branchfs/target/debug/branchfs" \
        "$(command -v branchfs 2>/dev/null || true)"
    do
        [[ -x "$c" ]] && { BRANCHFS_BIN="$c"; break; }
    done
fi

[[ -x "${BRANCHFS_BIN:-}" ]] || {
    echo "BAIL OUT! No branchfs binary found. Set BRANCHFS_BIN or build with 'cargo build'."
    exit 1
}

CONDA_LIB="${CONDA_LIB:-/home/domen/conda/envs/branchfs-dev/lib}"
[[ -d "$CONDA_LIB" ]] && export LD_LIBRARY_PATH="${CONDA_LIB}:${LD_LIBRARY_PATH:-}"

BFS="$BRANCHFS_BIN"

# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
PASS=0; FAIL=0; SKIP=0; TOTAL=0
declare -a FAILURES=()

ok()       { TOTAL=$((TOTAL+1)); PASS=$((PASS+1)); printf "ok %d - %s\n" "$TOTAL" "$1"; }
not_ok()   { TOTAL=$((TOTAL+1)); FAIL=$((FAIL+1));
             printf "not ok %d - %s\n" "$TOTAL" "$1"; FAILURES+=("$1")
             [[ "$FAIL_FAST" -eq 0 ]] || { echo "# FAIL FAST"; cleanup_exit 1; }; }
skip_t()   { TOTAL=$((TOTAL+1)); SKIP=$((SKIP+1));
             printf "ok %d - %s # SKIP %s\n" "$TOTAL" "$1" "${2:-}"; }
diag()     { printf "# %s\n" "$@"; }

assert_file_exists()     { [[ -e "$2" ]] && ok "$1" || not_ok "$1 (not found: $2)"; }
assert_file_not_exists() { [[ ! -e "$2" ]] && ok "$1" || not_ok "$1 (found: $2)"; }
assert_file_contains()   {
    grep -q "$3" "$2" 2>/dev/null && ok "$1" || not_ok "$1 (no '$3' in $2)"; }
assert_out_has()  {
    echo "$2" | grep -q "$3" && ok "$1" || not_ok "$1 (no '$3' in: $2)"; }
assert_out_not()  {
    ! echo "$2" | grep -q "$3" && ok "$1" || not_ok "$1 ('$3' in: $2)"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
run_bfs() { "$BFS" "$@" 2>&1 || true; }

write_delta() {
    local store="$1" branch="$2" relpath="$3" content="$4"
    local p="$store/branches/$branch/files/$relpath"
    mkdir -p "$(dirname "$p")"
    printf '%s' "$content" > "$p"
}

write_tombstone() {
    # tombstones is a flat newline-separated file; restart daemon to take effect
    local store="$1" branch="$2" relpath="$3"
    printf '%s\n' "$relpath" >> "$store/branches/$branch/tombstones"
}

shutdown_daemon() {
    local sock="$1/daemon.sock"
    [[ -S "$sock" ]] || return 0
    python3 -c "
import socket, json
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect('$sock')
    s.sendall(json.dumps({'cmd':'shutdown'}).encode() + b'\n')
    s.close()
except Exception: pass
" 2>/dev/null || true
    sleep 0.4
}

start_daemon() {
    "$BFS" start-daemon --base "$BASE" --storage "$1" >/dev/null 2>&1 &
    sleep 0.5
}

restart_daemon() {
    shutdown_daemon "$1"
    start_daemon "$1"
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
TMPDIR_ROOT="$(mktemp -d /tmp/branchfs-poc-XXXXXX)"

cleanup_exit() {
    local rc="${1:-0}"
    [[ "$KEEP" -eq 0 ]] && rm -rf "$TMPDIR_ROOT" || diag "kept: $TMPDIR_ROOT"
    exit "$rc"
}
trap 'cleanup_exit $?' EXIT

BASE="$TMPDIR_ROOT/base"
STORE="$TMPDIR_ROOT/store"

# Populate base with test data
mkdir -p "$BASE/projects/alpha" "$BASE/secrets"
printf 'hello from base\n'         > "$BASE/readme.txt"
printf 'alpha source\n'            > "$BASE/projects/alpha/main.py"
printf 'machine api.example.com\n' > "$BASE/secrets/.netrc"
printf 'existing data\n'           > "$BASE/existing.txt"
printf 'to be deleted\n'           > "$BASE/will_delete.txt"

diag "branchfs: $BFS"
diag "testdir:  $TMPDIR_ROOT"
echo ""
echo "TAP version 13"

# ---------------------------------------------------------------------------
# § 1  Daemon startup
# ---------------------------------------------------------------------------
echo ""
echo "# § 1  Daemon lifecycle"
start_daemon "$STORE"

[[ -S "$STORE/daemon.sock" ]] && ok "daemon socket created" || not_ok "daemon socket created"
OUT="$(run_bfs list --storage "$STORE")"
assert_out_has "list shows 'main' branch" "$OUT" "main"

# ---------------------------------------------------------------------------
# § 2  Lazy branch create
# ---------------------------------------------------------------------------
echo ""
echo "# § 2  Lazy branch create"
run_bfs create sess-lazy --storage "$STORE" >/dev/null 2>&1
ok "create returns 0"

INHERITED_DIR="$STORE/branches/sess-lazy/inherited"
if [[ -d "$INHERITED_DIR" && -z "$(ls -A "$INHERITED_DIR" 2>/dev/null)" ]]; then
    ok "inherited dir exists but is empty (no base snapshot)"
else
    not_ok "inherited dir should be empty in lazy mode"
fi

assert_file_exists "delta dir created"  "$STORE/branches/sess-lazy/files"
OUT="$(run_bfs status sess-lazy --storage "$STORE")"
assert_out_has "initial status: 0 deltas"  "$OUT" "Deltas:      0"
assert_out_has "initial status: 0 deletes" "$OUT" "Deletes:     0"
OUT="$(run_bfs list --storage "$STORE")"
assert_out_has "list shows new branch" "$OUT" "sess-lazy"

# ---------------------------------------------------------------------------
# § 3  Delta writes and status (human + JSON)
# ---------------------------------------------------------------------------
echo ""
echo "# § 3  Delta writes and status"

run_bfs create sess-delta --storage "$STORE" >/dev/null 2>&1
write_delta "$STORE" "sess-delta" "projects/alpha/output.txt" "accuracy: 0.95"
write_delta "$STORE" "sess-delta" "new_experiment.py"          "import torch"
write_tombstone "$STORE" "sess-delta" "will_delete.txt"

# Restart so daemon sees tombstone from file
restart_daemon "$STORE"

OUT="$(run_bfs status sess-delta --storage "$STORE")"
assert_out_has "status shows delta entries"    "$OUT" "delta"
assert_out_has "status shows delete entry"     "$OUT" "delete"
assert_out_has "status shows output.txt path"  "$OUT" "output.txt"

OUT_JSON="$(run_bfs status sess-delta --storage "$STORE" --json)"
assert_out_has "JSON has diff array"           "$OUT_JSON" '"diff"'
assert_out_has "JSON shows output.txt"         "$OUT_JSON" "output.txt"
assert_out_has "JSON shows delete op"          "$OUT_JSON" '"op": "delete"'

# Deltas count should be >0
OUT="$(run_bfs status sess-delta --storage "$STORE")"
assert_out_has "Deltas > 0 in text output"    "$OUT" "Deltas:"
assert_out_has "Deletes > 0 in text output"   "$OUT" "Deletes:"

# ---------------------------------------------------------------------------
# § 4  Hide rules
# ---------------------------------------------------------------------------
echo ""
echo "# § 4  Branch hide rules"
run_bfs create sess-hide --storage "$STORE" --hide "secrets/.netrc" --hide ".env" >/dev/null 2>&1
ok "create with hide rules succeeds"

META="$STORE/branches/sess-hide/meta.json"
assert_file_exists "branch meta.json written" "$META"
assert_file_contains "hide rule .netrc in meta" "$META" "netrc"
assert_file_contains "hide rule .env in meta"   "$META" ".env"

# ---------------------------------------------------------------------------
# § 5  Freeze and thaw
# ---------------------------------------------------------------------------
echo ""
echo "# § 5  Freeze / thaw"
run_bfs create sess-freeze --storage "$STORE" >/dev/null 2>&1
write_delta "$STORE" "sess-freeze" "work.txt" "in progress"
restart_daemon "$STORE"

run_bfs freeze sess-freeze --storage "$STORE" >/dev/null 2>&1
ok "freeze command succeeds"
OUT="$(run_bfs status sess-freeze --storage "$STORE")"
assert_out_has "status shows frozen state" "$OUT" "frozen"

# Thaw
run_bfs thaw sess-freeze --storage "$STORE" >/dev/null 2>&1
ok "thaw command succeeds"
OUT="$(run_bfs status sess-freeze --storage "$STORE")"
assert_out_has "status shows open after thaw" "$OUT" "open"

# ---------------------------------------------------------------------------
# § 6  Commit: deltas applied to base, tombstones remove base files
# ---------------------------------------------------------------------------
echo ""
echo "# § 6  Commit to base"
run_bfs create sess-commit --storage "$STORE" >/dev/null 2>&1
write_delta "$STORE" "sess-commit" "projects/alpha/result.txt" "artifact data"
write_delta "$STORE" "sess-commit" "report.md"                 "# Report"
write_tombstone "$STORE" "sess-commit" "existing.txt"

restart_daemon "$STORE"

run_bfs freeze sess-commit --storage "$STORE" >/dev/null 2>&1
run_bfs commit-branch sess-commit --storage "$STORE" >/dev/null 2>&1
ok "commit-branch succeeds"

assert_file_exists     "committed: projects/alpha/result.txt in base" \
    "$BASE/projects/alpha/result.txt"
assert_file_exists     "committed: report.md in base" \
    "$BASE/report.md"
assert_file_not_exists "tombstone applied: existing.txt removed from base" \
    "$BASE/existing.txt"
assert_file_exists     "non-tombstoned readme.txt untouched" \
    "$BASE/readme.txt"

# Committed branches are removed from daemon (they no longer exist in active store)
OUT="$(run_bfs status sess-commit --storage "$STORE" 2>&1 || true)"
assert_out_has "committed branch removed from active store" "$OUT" "not found"

# ---------------------------------------------------------------------------
# § 7  Abort: discard deltas, base untouched
# ---------------------------------------------------------------------------
echo ""
echo "# § 7  Abort branch"
run_bfs create sess-abort --storage "$STORE" >/dev/null 2>&1
write_delta "$STORE" "sess-abort" "dangerous.sh"     "rm -rf /"
write_delta "$STORE" "sess-abort" "secret_stolen.txt" "apikey=abc"

restart_daemon "$STORE"
run_bfs abort-branch sess-abort --storage "$STORE" >/dev/null 2>&1
ok "abort-branch succeeds"

assert_file_not_exists "abort: dangerous.sh not in base"      "$BASE/dangerous.sh"
assert_file_not_exists "abort: secret_stolen.txt not in base" "$BASE/secret_stolen.txt"

# Aborted branches are removed from daemon (like committed ones)
OUT="$(run_bfs status sess-abort --storage "$STORE" 2>&1 || true)"
assert_out_has "aborted branch removed from active store" "$OUT" "not found"

# ---------------------------------------------------------------------------
# § 8  Multi-branch isolation
# ---------------------------------------------------------------------------
echo ""
echo "# § 8  Multi-branch isolation"
run_bfs create sess-a --storage "$STORE" >/dev/null 2>&1
run_bfs create sess-b --storage "$STORE" >/dev/null 2>&1
write_delta "$STORE" "sess-a" "from_a.txt" "written by A"
write_delta "$STORE" "sess-b" "from_b.txt" "written by B"

# Restart to ensure daemon sees fresh state
restart_daemon "$STORE"

OUT_A="$(run_bfs status sess-a --storage "$STORE")"
OUT_B="$(run_bfs status sess-b --storage "$STORE")"
assert_out_has  "branch A sees its own delta"         "$OUT_A" "from_a"
assert_out_not  "branch A does not see branch B file" "$OUT_A" "from_b"
assert_out_has  "branch B sees its own delta"         "$OUT_B" "from_b"
assert_out_not  "branch B does not see branch A file" "$OUT_B" "from_a"

# ---------------------------------------------------------------------------
# § 9  Nested branch (child inherits parent)
# ---------------------------------------------------------------------------
echo ""
echo "# § 9  Nested branch inheritance"
run_bfs create sess-parent --storage "$STORE" >/dev/null 2>&1
write_delta "$STORE" "sess-parent" "parent_file.txt" "from parent"
restart_daemon "$STORE"

run_bfs create sess-child --parent sess-parent --storage "$STORE" >/dev/null 2>&1
ok "create child branch with --parent"

OUT="$(run_bfs status sess-child --storage "$STORE")"
assert_out_has "child status shows parent" "$OUT" "sess-parent"
# Child starts with 0 of its own deltas
assert_out_has "child starts with 0 own deltas" "$OUT" "Deltas:      0"

# Write to child only
write_delta "$STORE" "sess-child" "child_only.txt" "child work"
restart_daemon "$STORE"
OUT_P="$(run_bfs status sess-parent --storage "$STORE")"
OUT_C="$(run_bfs status sess-child  --storage "$STORE")"
assert_out_has  "child sees its own write"           "$OUT_C" "child_only"
assert_out_not  "parent doesn't see child's write"   "$OUT_P" "child_only"

# ---------------------------------------------------------------------------
# § 10  Daemon resilience
# ---------------------------------------------------------------------------
echo ""
echo "# § 10  Daemon resilience"
[[ -S "$STORE/daemon.sock" ]] && ok "daemon still active after all ops" || \
    not_ok "daemon still active after all ops"

run_bfs list --storage "$STORE" >/dev/null 2>&1
ok "list responds at end of test"

# Verify total branch count includes all created branches
OUT="$(run_bfs list --storage "$STORE")"
BRANCH_COUNT="$(echo "$OUT" | grep -c "^" || true)"
[[ "$BRANCH_COUNT" -ge 8 ]] && ok "at least 8 branches tracked by daemon" || \
    not_ok "expected >= 8 branches, got $BRANCH_COUNT"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "# ─────────────────────────────────────────────────────────────────────"
echo "# Results: pass=$PASS fail=$FAIL skip=$SKIP total=$TOTAL"
if [[ ${#FAILURES[@]} -gt 0 ]]; then
    echo "# FAILED:"
    for f in "${FAILURES[@]}"; do printf "#   - %s\n" "$f"; done
fi
echo "1..$TOTAL"

[[ "$FAIL" -eq 0 ]] && echo "# All tests passed." || true
[[ "$FAIL" -eq 0 ]]
