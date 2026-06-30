# ccc-agent path policy and secret hiding

Two distinct mechanisms, often confused — keep them apart:

1. **Hide paths** (BranchFS, *preventive*): literal relative paths masked from
   the agent's branch view. The agent can never read them. Configured per
   protected root (`roots[].hide_paths`), enforced by the BranchFS resolver
   (`branchfs create --hide ...`).
2. **Deny/hide patterns** (policy engine, *detective*): globs evaluated at
   freeze time against the canonicalized change list. A match never blocks the
   agent while it runs; it downgrades the decision to `pending-review`.

## Decision model

After freeze, every changed path (from `branchfs status`, mapped into the
agent-visible namespace) is canonicalized — `/home/$USER/...` and
`/storage/user/...` collapse to one namespace — then:

```text
no changes                                      -> no-op close (abort branch)
mode=throwaway                                  -> abort
mode=manual | read-only-review                  -> pending-review
mode=workspace-auto | training-run:
    every path within allowed_scopes
    and no deny/hide pattern matches            -> auto-commit
    otherwise                                   -> pending-review
```

`allowed_scopes` defaults to the declared workspace. Scopes may be declared
via either alias (`/home/...` or `/storage/user/...`); canonicalization makes
them equivalent.

## Pattern semantics (`ccc_agent.policy.path_matches`)

- pattern without `/` — matches any single path component:
  `.env`, `id_rsa*`, `*.pem`
- pattern with `/`, not absolute — matches that component sequence anywhere,
  plus everything below it: `.git/hooks` matches `proj/.git/hooks/pre-commit`
- absolute pattern — fnmatch against the whole canonical path:
  `/storage/group/*`

Default deny set (see `policy.DEFAULT_DENY_PATTERNS`): SSH/GPG material,
`.env*`, key/credential files, `.netrc`, `.aws`, `.kube/config`,
`.docker/config.json`, `.git/config`, `.git/hooks`, shell startup files,
`.condarc`, and `.ccc-agent` (supervisor state). Override per deployment via
`deny_patterns` in policy config; extend per run with `ccc-agent run --hide`.

## Secret hiding: what is and is not guaranteed

With `hide_paths` on a root (e.g. `.ssh`, `.netrc`, `.aws`):

- the agent cannot read or list those inherited paths — resolution and
  readdir treat them as nonexistent (subtree included);
- main and other branches are unaffected; the underlay is untouched;
- if the agent *creates* a file at a hidden path, that is its own delta
  (shadow): visible to the agent, reported by status, and **flagged by the
  matching deny pattern** so it cannot auto-commit over the real secret.

Not guaranteed:

- secrets inside the workspace the user explicitly exposed (an `.env` the
  project itself contains is readable unless listed in `hide_paths`; the
  deny pattern still forces review if the agent modifies it);
- patterns in `hide_paths` — BranchFS hiding is literal-prefix only by
  design (O(1), no tree scans). Use well-known literal locations there and
  globs in `deny_patterns`/`hide_patterns`;
- anything outside the protected roots.

Defense in depth, in order: hide at the filesystem (can't read), deny at
policy (can't auto-commit), review artifacts (humans see exactly what was
touched).

## Policy modes

| mode | use case |
|---|---|
| `workspace-auto` | default: agent edits its project, auto-commit when clean |
| `manual` | high-stakes data; always a human decision |
| `read-only-review` | audits/dry-runs: report, never commit automatically |
| `training-run` | scopes = declared artifact dirs (checkpoints, logs) |
| `throwaway` | exploration; discard at completion unless a human commits first |

## Bounded self-repair (`ccc-agent check-before-final`)

When a harness supports a blocking Stop hook (Claude Code, Codex), the hook
runs `ccc-agent check-before-final <session>` before reporting the turn.
The check classifies **live** status — no freeze, no commit — and only looks
at scope and deny/hide hygiene; mode semantics (`manual`,
`read-only-review`, ...) still apply at finalize:

- **clean** → exit 0 (`check-clean` event); the stop proceeds and the normal
  finalize flow decides per mode;
- **dirty, repair budget left** → exit 2 with the offending paths printed;
  the harness blocks the stop and the agent reverts in-session
  (`repair-requested` event, `repair_attempts` incremented);
- **dirty, budget exhausted** → exit 0 (`repair-budget-exhausted` event) so
  hooks can never livelock the agent; the violations simply land in
  `pending-review` at finalize.

`max_policy_repair_attempts` (default 2) bounds the loop; the count is
stored per session, in supervisor state the agent cannot touch.
