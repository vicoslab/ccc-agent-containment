#!/bin/sh
# Codex CLI Stop-hook adapter for CCC agent sessions.
#
# Register through Codex managed requirements/config (trusted path), pointing
# the Stop hook at this script. Semantics match the Claude adapter: run the
# bounded self-repair check (exit 2 + instructions on stderr asks the harness
# to continue so the agent reverts), then report turn completion. Never
# commit from a hook.
set -eu

CTL="${CCC_AGENTCTL:-/opt/ccc-agent/bin/ccc-agentctl}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
[ -x "$CTL" ] || exit 0

rc=0
"$CTL" check-before-final "$CCC_AGENT_SESSION" 1>&2 || rc=$?
[ "$rc" -ne 2 ] || exit 2   # dirty + budget left: agent should repair

"$CTL" finish-turn "$CCC_AGENT_SESSION" || true
exit 0
