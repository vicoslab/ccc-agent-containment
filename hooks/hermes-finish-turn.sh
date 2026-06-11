#!/bin/sh
# Hermes hook adapter for CCC agent sessions.
#
# Wire from a CCC-managed Hermes plugin/profile (post_llm_call or gateway
# agent:end), e.g.:
#
#   hooks:
#     post_llm_call:
#       - exec: /opt/ccc-agent/hooks/hermes-finish-turn.sh
#
# Reports turn completion only; commit authority stays with the supervisor.
set -eu

CTL="${CCC_AGENTCTL:-/opt/ccc-agent/bin/ccc-agentctl}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
[ -x "$CTL" ] || exit 0

"$CTL" finish-turn "$CCC_AGENT_SESSION" || true
exit 0
