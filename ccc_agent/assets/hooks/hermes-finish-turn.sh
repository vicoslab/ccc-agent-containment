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
# Deliberately no check-before-final here: post_llm_call cannot block/feed
# instructions back, and an advisory check would silently burn the session's
# repair budget. Long-running Hermes sessions are closed by a human/operator
# via `ccc-agentctl finish`, which freezes and applies policy.
set -eu

CTL="${CCC_AGENTCTL:-ccc-agentctl}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
command -v "$CTL" >/dev/null 2>&1 || exit 0

"$CTL" finish-turn "$CCC_AGENT_SESSION" || true
exit 0
