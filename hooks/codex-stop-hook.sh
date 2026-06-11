#!/bin/sh
# Codex CLI Stop-hook adapter for CCC agent sessions.
#
# Register through Codex managed requirements/config (trusted path), pointing
# the Stop hook at this script. Semantics match the Claude adapter: report
# turn completion to the supervisor, never commit from a hook.
set -eu

CTL="${CCC_AGENTCTL:-/opt/ccc-agent/bin/ccc-agentctl}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
[ -x "$CTL" ] || exit 0

"$CTL" finish-turn "$CCC_AGENT_SESSION" || true
exit 0
