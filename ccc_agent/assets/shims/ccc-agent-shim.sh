#!/bin/sh
# Generic transparent launch shim for agent CLIs (codex, claude, hermes,
# opencode, ...). Install by symlinking this file into a PATH directory that
# precedes the real binary, named after the tool:
#
#   ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/codex
#
# Behavior:
#   - resolves the real binary as the next match in PATH after this shim;
#   - nested agents (CCC_AGENT_SESSION set) run directly: the launcher reuses
#     the existing session, so no new branch bundle is created;
#   - CCC_AGENT_SHIM_BYPASS=1 skips containment entirely (debug only; policy
#     may forbid it on managed deployments).
set -eu

AGENT_NAME="$(basename "$0")"
SHIM_PATH="$(command -v -- "$AGENT_NAME" || true)"

# Find the real binary: first PATH entry whose $AGENT_NAME is not this shim.
REAL_BIN=""
OLD_IFS="$IFS"; IFS=:
for dir in $PATH; do
    candidate="$dir/$AGENT_NAME"
    [ -x "$candidate" ] || continue
    if [ "$candidate" != "$SHIM_PATH" ] && [ ! "$candidate" -ef "$SHIM_PATH" ]; then
        REAL_BIN="$candidate"
        break
    fi
done
IFS="$OLD_IFS"

if [ -z "$REAL_BIN" ]; then
    echo "ccc-agent-shim: no real '$AGENT_NAME' binary found in PATH" >&2
    exit 127
fi

if [ "${CCC_AGENT_SHIM_BYPASS:-0}" = "1" ]; then
    echo "ccc-agent-shim: bypass enabled, running $REAL_BIN unprotected" >&2
    exec "$REAL_BIN" "$@"
fi

if [ -n "${CCC_AGENT_SESSION:-}" ]; then
    # already inside a contained session: run directly, stay in the branch
    exec "$REAL_BIN" "$@"
fi

LAUNCH="${CCC_AGENT_LAUNCH:-ccc-agent-launch}"
if ! command -v "$LAUNCH" >/dev/null 2>&1; then
    echo "ccc-agent-shim: launcher not found at $LAUNCH" >&2
    echo "ccc-agent-shim: refusing to run '$AGENT_NAME' unprotected (set CCC_AGENT_SHIM_BYPASS=1 to override)" >&2
    exit 1
fi

echo "ccc-agent-shim: containing '$AGENT_NAME' via $LAUNCH (real: $REAL_BIN)" >&2
exec "$LAUNCH" --agent "$AGENT_NAME" -- "$REAL_BIN" "$@"
