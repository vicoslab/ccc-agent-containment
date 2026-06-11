#!/bin/sh
# Claude Code Stop-hook adapter for CCC agent sessions.
#
# Register from a TRUSTED (read-only to the agent) settings path, e.g.
# managed settings or launcher-injected --settings:
#
#   {
#     "hooks": {
#       "Stop": [{
#         "hooks": [{
#           "type": "command",
#           "command": "/opt/ccc-agent/hooks/claude-stop-hook.sh"
#         }]
#       }]
#     }
#   }
#
# Hooks REPORT lifecycle events; they never freeze, commit, or abort.
set -eu

CTL="${CCC_AGENTCTL:-/opt/ccc-agent/bin/ccc-agentctl}"

if [ -z "${CCC_AGENT_SESSION:-}" ]; then
    # not a contained session (e.g. human-run claude outside ccc-agent-run)
    exit 0
fi

if [ ! -x "$CTL" ]; then
    echo "ccc claude hook: ccc-agentctl not found at $CTL" >&2
    exit 0   # never block the agent's stop on hook plumbing problems
fi

"$CTL" finish-turn "$CCC_AGENT_SESSION" || true
exit 0
