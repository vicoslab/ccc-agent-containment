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
# Hooks REPORT lifecycle events and may BLOCK the stop for bounded
# self-repair; they never freeze, commit, or abort.
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

# Bounded self-repair: ask the supervisor whether the live branch is clean.
# Exit 2 means "dirty, repair budget left": Claude Code blocks the stop and
# feeds the instructions (stderr) back to the agent, which reverts the
# flagged paths in-session.  Any other outcome — clean, budget exhausted,
# control error — must NOT block the stop; finalize parks dirty sessions
# as pending-review for a human instead.
rc=0
"$CTL" check-before-final "$CCC_AGENT_SESSION" 1>&2 || rc=$?
if [ "$rc" -eq 2 ]; then
    exit 2
fi

"$CTL" finish-turn "$CCC_AGENT_SESSION" || true
exit 0
