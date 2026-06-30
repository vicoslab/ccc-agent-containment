#!/usr/bin/env bash
# ccc-agent softsandbox — BranchFS tracking sandbox for agent containment.
#
# Provides a "tracking sandbox" without FUSE or root access:
#
#   Isolation mode (--isolate):
#     Copies the workspace to a branch delta dir, runs the agent there as CWD.
#     Agent sees existing files + any new files it writes. After exit, diffs
#     the delta dir against the original base and shows true changes. Commit
#     applies only changed/new files to real workspace.
#
#   Tracking mode (default, no --isolate):
#     Agent runs in the real workspace (full access). After exit, uses branchfs
#     status to show what changed by comparing current workspace against the
#     pre-run snapshot. Good for monitoring, not hard isolation.
#
# NOTE: Without FUSE, the agent is not fully contained. The agent can still
# access any file in the real filesystem. The sandbox provides: reviewable
# diffs, commit/abort decision, and optionally a directory-level boundary.
# Full containment requires FUSE + chroot (see scripts/ccc-agent-chroot.sh).
#
# Usage:
#   ccc-agent softsandbox [options] -- COMMAND [ARGS...]
#
# Options:
#   --workspace DIR    The folder to protect (default: $PWD)
#   --session-id ID    Session ID (auto-generated if omitted)
#   --state-dir DIR    State/branch store location (default: ~/.ccc-agent)
#   --branchfs BIN     Path to branchfs binary
#   --hide PATH        Exclude a path from agent view and diff (repeatable)
#   --isolate          Run agent in a separate branch dir (stronger boundary)
#   --no-confirm       Auto-commit if agent exits 0 (non-interactive)
#   --dry-run          Print plan without running anything
#
# Environment:
#   CCC_AGENT_SESSION         If set, assumes nested agent; runs uncontained
#   CCC_AGENT_BRANCHFS_BIN Path to branchfs binary override
set -euo pipefail

WORKSPACE="$(realpath "${PWD}")"
SESSION_ID=""
STATE_DIR="${HOME}/.ccc-agent"
BRANCHFS_BIN="${CCC_AGENT_BRANCHFS_BIN:-}"
declare -a HIDE_PATHS=()
ISOLATE=0
NO_CONFIRM=0
DRY_RUN=0
declare -a COMMAND=()

usage() { grep '^# ' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace)   WORKSPACE="$(realpath "$2")"; shift 2 ;;
        --session-id)  SESSION_ID="$2"; shift 2 ;;
        --state-dir)   STATE_DIR="$2"; shift 2 ;;
        --branchfs)    BRANCHFS_BIN="$2"; shift 2 ;;
        --hide)        HIDE_PATHS+=("$2"); shift 2 ;;
        --isolate)     ISOLATE=1; shift ;;
        --no-confirm)  NO_CONFIRM=1; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        --)            shift; COMMAND=("$@"); break ;;
        *)             echo "softsandbox: unknown: $1" >&2; exit 2 ;;
    esac
done

[[ ${#COMMAND[@]} -gt 0 ]] || {
    echo "softsandbox: no command given. Usage: ccc-agent softsandbox [opts] -- CMD" >&2
    exit 2
}

# Nested agent: reuse outer session
if [[ -n "${CCC_AGENT_SESSION:-}" ]]; then
    echo "softsandbox: nested session '${CCC_AGENT_SESSION}' detected, running uncontained" >&2
    exec "${COMMAND[@]}"
fi

# ---------------------------------------------------------------------------
# Find branchfs binary
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "$BRANCHFS_BIN" ]]; then
    for c in \
        "${SCRIPT_DIR}/../../../../worktrees/branchfs-agent-containment/target/debug/branchfs" \
        "${SCRIPT_DIR}/../../../../worktrees/branchfs-agent-containment/target/release/branchfs" \
        "${SCRIPT_DIR}/../../../../branchfs/target/debug/branchfs" \
        "/opt/ccc-agent/bin/branchfs" \
        "$(command -v branchfs 2>/dev/null || true)"
    do
        [[ -x "$c" ]] && { BRANCHFS_BIN="$(realpath "$c")"; break; }
    done
fi
[[ -x "${BRANCHFS_BIN:-}" ]] || {
    echo "softsandbox: no branchfs binary. Set --branchfs or CCC_AGENT_BRANCHFS_BIN." >&2
    exit 1
}
CONDA_LIB="${CONDA_LIB:-/home/domen/conda/envs/branchfs-dev/lib}"
[[ -d "$CONDA_LIB" ]] && export LD_LIBRARY_PATH="${CONDA_LIB}:${LD_LIBRARY_PATH:-}"
BFS="$BRANCHFS_BIN"

# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------
if [[ -z "$SESSION_ID" ]]; then
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    RAND="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
    SESSION_ID="agent-${STAMP}-${RAND}"
fi

WNAME="$(basename "$WORKSPACE")"
STORE="${STATE_DIR}/stores/${WNAME}"
DELTA_DIR="${STORE}/branches/${SESSION_ID}/files"
SESSIONS_DIR="${STATE_DIR}/sessions"

# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "ccc-agent softsandbox DRY RUN"
    echo "  Session:    $SESSION_ID"
    echo "  Workspace:  $WORKSPACE"
    echo "  Mode:       $([ $ISOLATE -eq 1 ] && echo isolate || echo tracking)"
    echo "  BranchFS:   $BFS"
    [[ ${#HIDE_PATHS[@]} -gt 0 ]] && printf "  Hide:       %s\n" "${HIDE_PATHS[@]}"
    echo "  Command:    ${COMMAND[*]}"
    echo ""
    echo "Would:"
    echo "  branchfs start-daemon --base $WORKSPACE --storage $STORE"
    echo "  branchfs create $SESSION_ID --storage $STORE"
    if [[ $ISOLATE -eq 1 ]]; then
        echo "  rsync $WORKSPACE/ $DELTA_DIR/"
        echo "  run agent in: $DELTA_DIR"
    else
        echo "  take pre-run snapshot of $WORKSPACE"
        echo "  run agent in: $WORKSPACE (direct)"
    fi
    echo "  branchfs status $SESSION_ID --storage $STORE"
    echo "  [commit/abort]"
    exit 0
fi

mkdir -p "$STORE" "$SESSIONS_DIR"

echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  CCC Agent Soft Sandbox                                      │"
echo "├──────────────────────────────────────────────────────────────┤"
printf "│  Session:   %-50s│\n" "$SESSION_ID"
printf "│  Workspace: %-50s│\n" "$WORKSPACE"
MODE="$([ $ISOLATE -eq 1 ] && echo 'isolate (agent in branch copy)' || echo 'tracking (agent in real workspace)')"
printf "│  Mode:      %-50s│\n" "$MODE"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""

# ---------------------------------------------------------------------------
# Start daemon and create branch
# ---------------------------------------------------------------------------
"$BFS" start-daemon --base "$WORKSPACE" --storage "$STORE" >/dev/null 2>&1 &
sleep 0.5

HIDE_ARGS=()
for h in "${HIDE_PATHS[@]:-}"; do
    [[ -n "$h" ]] && HIDE_ARGS+=("--hide" "$h")
done
"$BFS" create "$SESSION_ID" --storage "$STORE" "${HIDE_ARGS[@]}" >/dev/null 2>&1

# ---------------------------------------------------------------------------
# Mode-specific setup
# ---------------------------------------------------------------------------
AGENT_CWD="$WORKSPACE"

if [[ "$ISOLATE" -eq 1 ]]; then
    # Copy workspace into delta dir so agent sees existing files
    mkdir -p "$DELTA_DIR"
    RSYNC_EXCLUDES=()
    for h in "${HIDE_PATHS[@]:-}"; do
        [[ -n "$h" ]] && RSYNC_EXCLUDES+=("--exclude=$h")
    done
    rsync -a "${RSYNC_EXCLUDES[@]:-}" "$WORKSPACE/" "$DELTA_DIR/" 2>/dev/null || \
        cp -a "$WORKSPACE/." "$DELTA_DIR/" 2>/dev/null || true
    AGENT_CWD="$DELTA_DIR"
    echo "→ Agent workspace (isolated copy): $DELTA_DIR"
else
    # Tracking mode: take a snapshot of current workspace state for diff
    SNAPSHOT="${STATE_DIR}/snapshots/${SESSION_ID}"
    mkdir -p "$SNAPSHOT"
    RSYNC_EXCLUDES=()
    for h in "${HIDE_PATHS[@]:-}"; do
        [[ -n "$h" ]] && RSYNC_EXCLUDES+=("--exclude=$h")
    done
    rsync -a "${RSYNC_EXCLUDES[@]:-}" "$WORKSPACE/" "$SNAPSHOT/" 2>/dev/null || \
        cp -a "$WORKSPACE/." "$SNAPSHOT/" 2>/dev/null || true
    echo "→ Pre-run snapshot saved: $SNAPSHOT"
    echo "→ Agent will run in real workspace: $WORKSPACE"
fi

# ---------------------------------------------------------------------------
# Save session record
# ---------------------------------------------------------------------------
SESSION_FILE="$SESSIONS_DIR/${SESSION_ID}.json"
python3 -c "
import json, os
d = {
    'session_id': '$SESSION_ID',
    'workspace': '$WORKSPACE',
    'store': '$STORE',
    'delta_dir': '$DELTA_DIR',
    'agent_cwd': '$AGENT_CWD',
    'branchfs_bin': '$BFS',
    'isolate': bool($ISOLATE),
    'command': $(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" -- "${COMMAND[@]}"),
    'created_at': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'state': 'running',
}
with open('$SESSION_FILE', 'w') as f:
    json.dump(d, f, indent=2)
"

# ---------------------------------------------------------------------------
# Run agent
# ---------------------------------------------------------------------------
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "Running: ${COMMAND[*]}"
echo "────────────────────────────────────────────────────────────────"

AGENT_RC=0
(
    cd "$AGENT_CWD"
    env \
        CCC_AGENT_SESSION="$SESSION_ID" \
        CCC_AGENT_STATE_DIR="$STATE_DIR" \
        "${COMMAND[@]}"
) || AGENT_RC=$?

echo "────────────────────────────────────────────────────────────────"
echo "Agent exited: $AGENT_RC"
echo "────────────────────────────────────────────────────────────────"
echo ""

# ---------------------------------------------------------------------------
# Compute diff
# ---------------------------------------------------------------------------
if [[ "$ISOLATE" -eq 1 ]]; then
    # In isolate mode, the delta dir IS where the agent worked.
    # Remove pre-populated base files that haven't changed to get true deltas.
    echo "→ Computing true diff..."
    python3 - "$WORKSPACE" "$DELTA_DIR" "${HIDE_PATHS[@]:-}" <<'PYEOF'
import os, sys, hashlib

base_dir = sys.argv[1]
delta_dir = sys.argv[2]
hide_paths = set(sys.argv[3:])

def hidden(rel):
    return any(rel == h or rel.startswith(h + os.sep) for h in hide_paths)

def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

changed, new_files, deleted, unchanged = [], [], [], []

# Walk delta dir: classify each file relative to base
for root, dirs, files in os.walk(delta_dir):
    for name in files:
        delta_path = os.path.join(root, name)
        rel = os.path.relpath(delta_path, delta_dir)
        if hidden(rel):
            continue
        base_path = os.path.join(base_dir, rel)
        if not os.path.exists(base_path):
            new_files.append(rel)
        else:
            try:
                if file_hash(delta_path) != file_hash(base_path):
                    changed.append(rel)
                else:
                    unchanged.append(rel)
            except OSError:
                changed.append(rel)

# Walk base: files missing from delta = agent deleted them
for root, dirs, files in os.walk(base_dir):
    for name in files:
        base_path = os.path.join(root, name)
        rel = os.path.relpath(base_path, base_dir)
        if hidden(rel):
            continue
        delta_path = os.path.join(delta_dir, rel)
        if not os.path.exists(delta_path):
            deleted.append(rel)

# Remove unchanged pre-populated files from delta AFTER diff is computed,
# so branchfs status only shows actual changes.
for rel in unchanged:
    try:
        os.unlink(os.path.join(delta_dir, rel))
    except OSError:
        pass

print(f"True diff: {len(changed)} modified, {len(new_files)} new, {len(deleted)} deleted")
for f in sorted(changed):   print(f"  M {f}")
for f in sorted(new_files): print(f"  A {f}")
for f in sorted(deleted):   print(f"  D {f}")

# Write tombstones for deleted files so branchfs commit removes them from base
tombstone_file = os.path.join(delta_dir, '..', 'tombstones')
tombstone_file = os.path.normpath(tombstone_file)
if deleted:
    with open(tombstone_file, 'a') as tf:
        for rel in deleted:
            tf.write(rel + '\n')
    print(f"  (wrote {len(deleted)} tombstone(s) to branch store)")
PYEOF
fi

# Restart daemon to pick up tombstones written by diff script
python3 -c "
import socket, json
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect('$STORE/daemon.sock')
    s.sendall(json.dumps({'cmd':'shutdown'}).encode() + b'\n')
    s.close()
except Exception: pass
" 2>/dev/null || true
sleep 0.3
"$BFS" start-daemon --base "$WORKSPACE" --storage "$STORE" >/dev/null 2>&1 &
sleep 0.5

echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  Branch Diff                                                 │"
echo "├──────────────────────────────────────────────────────────────┤"

if [[ "$ISOLATE" -eq 1 ]]; then
    "$BFS" status "$SESSION_ID" --storage "$STORE" 2>&1 | sed 's/^/│  /'
else
    # Tracking mode: compute diff from snapshot
    echo "│  [tracking mode: diff based on pre-run snapshot]"
    rsync -avn --compare-dest="$SNAPSHOT/" "$WORKSPACE/" /dev/null 2>&1 | \
        grep -v "/$" | grep "^[^>]" | \
        sed 's/^/│  M /' || echo "│  (no changes detected)"
fi
echo "└──────────────────────────────────────────────────────────────┘"
echo ""

# ---------------------------------------------------------------------------
# Commit/abort decision
# ---------------------------------------------------------------------------
DELTA_COUNT="$("$BFS" status "$SESSION_ID" --storage "$STORE" 2>&1 | awk '/^Deltas:/{print $2}' || echo 0)"
DELETE_COUNT="$("$BFS" status "$SESSION_ID" --storage "$STORE" 2>&1 | awk '/^Deletes:/{print $2}' || echo 0)"

if [[ "$DELTA_COUNT" -eq 0 && "$DELETE_COUNT" -eq 0 ]]; then
    echo "No changes detected. Aborting cleanly."
    "$BFS" abort-branch "$SESSION_ID" --storage "$STORE" >/dev/null 2>&1
    python3 -c "import json; d=json.load(open('$SESSION_FILE')); d['state']='aborted'; json.dump(d,open('$SESSION_FILE','w'),indent=2)" 2>/dev/null || true
    echo "Session aborted (no changes): $SESSION_ID"
    exit "$AGENT_RC"
fi

DECISION=""
if [[ "$NO_CONFIRM" -eq 1 ]]; then
    DECISION="commit"
    echo "→ Auto-committing ($DELTA_COUNT modified, $DELETE_COUNT deleted)"
else
    echo "Changes: $DELTA_COUNT modified/new, $DELETE_COUNT deleted"
    echo ""
    echo "Options:"
    echo "  [c] commit — apply changes to: $WORKSPACE"
    echo "  [a] abort  — discard all changes"
    echo "  [s] show   — show diff again"
    echo ""
    while true; do
        read -r -p "Decision [c/a/s]: " DECISION || { DECISION="abort"; break; }
        case "$DECISION" in
            c|commit) DECISION="commit"; break ;;
            a|abort)  DECISION="abort";  break ;;
            s|show)
                "$BFS" status "$SESSION_ID" --storage "$STORE" 2>&1
                ;;
            *) echo "Please enter c, a, or s." ;;
        esac
    done
fi

echo ""
if [[ "$DECISION" == "commit" ]]; then
    "$BFS" freeze "$SESSION_ID" --storage "$STORE" >/dev/null 2>&1
    "$BFS" commit-branch "$SESSION_ID" --storage "$STORE"
    echo "✓ Committed: $SESSION_ID → $WORKSPACE"
    python3 -c "import json; d=json.load(open('$SESSION_FILE')); d['state']='committed'; json.dump(d,open('$SESSION_FILE','w'),indent=2)" 2>/dev/null || true
else
    "$BFS" abort-branch "$SESSION_ID" --storage "$STORE"
    echo "✓ Aborted: $SESSION_ID (workspace unchanged)"
    python3 -c "import json; d=json.load(open('$SESSION_FILE')); d['state']='aborted'; json.dump(d,open('$SESSION_FILE','w'),indent=2)" 2>/dev/null || true
fi

# Clean up snapshot
[[ -d "${STATE_DIR}/snapshots/${SESSION_ID:-}" ]] && rm -rf "${STATE_DIR}/snapshots/${SESSION_ID}" || true

exit "$AGENT_RC"
