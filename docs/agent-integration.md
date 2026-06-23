# Integrating real agents (codex / claude) with ccc-agent

How each agent reaches the per-turn control channel, how to register the hook,
and how credentials are handled.

## Turn-boundary matrix

| Invocation | Turn boundary | How finalize happens | Approval flow |
|---|---|---|---|
| `codex exec "…"` | process exit (1 turn) | supervisor **process-exit finalize** (no hook) | session-end review |
| `claude -p "…"` | process exit (1 turn) | supervisor **process-exit finalize** (no hook) | session-end review |
| `claude` (interactive) | each Stop | **Stop hook** → `ccc-agentctl finalize-turn` | **blocking** per-turn (exit 2) |
| `codex` (interactive) | each turn | **`notify`** → `ccc-agentctl finalize-turn` | report-only (see below) |

**Non-interactive (`exec`/`-p`) needs no hook** — one turn per process, so the
supervisor's existing end-of-process finalize is the per-turn commit.

**Claude interactive** uses a real Stop hook that can *block* the stop (exit 2),
so out-of-scope changes prompt the user mid-turn and commit only on
`approve-turn`.

**Codex interactive** has no Stop hook — only a `notify` program, which codex
runs fire-and-forget (it ignores the exit code and does not wait). So codex
interactive gets per-turn **in-scope auto-commit**, but out-of-scope changes
**cannot be blocked mid-turn** and defer to **session-end review**
(`pending-review`). Use `codex exec` or Claude Code if you need blocking
per-turn approval for codex-style workflows.

## Hook registration

**Claude Code** — `hooks.Stop` in a trusted settings file the agent can't
rewrite (managed settings, or launcher-injected `--settings`). See
`config/claude-managed-settings.example.json`:

```json
{ "hooks": { "Stop": [ { "hooks": [
  { "type": "command", "command": "/opt/ccc-agent/hooks/claude-stop-hook.sh" }
] } ] } }
```

**Codex** — `notify` in `~/.codex/config.toml` (see
`config/codex-config.example.toml`):

```toml
notify = ["/opt/ccc-agent/hooks/codex-notify.sh"]
```

## Credentials

The agent's config/state dirs (`~/.codex`, `~/.claude`) are hidden by the
branch view, so they're re-exposed read-only and secrets are handled per
`config.json`:

- `cred_mounts` — dirs re-bound read-only from the real home.
- `cred_mask` — secret files overmounted with `/dev/null` (never enter the box).
- `cred_env` — the supervisor reads the host auth file and passes the credential
  as an env var (`{file, json_key}` reads a dotted key from JSON; `{env: NAME}`
  passes through; a literal string is used as-is).

**API key vs OAuth (important):** env-passing + masking only works when the
agent authenticates by **API key** (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`). If
the agent is logged in via an **OAuth subscription** (ChatGPT / Claude account —
codex `auth.json` with `tokens`, claude `.credentials.json`), there is no API
key to pass and the agent authenticates from the **token file**, which must
therefore remain readable in the sandbox: bind the dir via `cred_mounts` and do
**not** mask the token file. In the naive/accidental threat model this is
acceptable (it's the user's own agent using the user's own subscription, same
uid). Prefer API keys when you want the token to never enter the sandbox.

## Browsing / cleaning lingering sessions

A session that exits with un-committed deltas stays as a reviewable branch:

```bash
ccc-agentctl list                       # sessions + states
ccc-agentctl review <session>           # browse the diff
ccc-agentctl review <session> --accept  # commit all
ccc-agentctl review <session> --reject  # discard all
ccc-agentctl review <session> --commit a,b   # commit only a,b (rest discarded)
ccc-agentctl review <session> --emit-patch > c.patch   # line-level: prune hunks…
ccc-agentctl review <session> --apply-patch c.patch    # …then apply
```

Or directly via the BranchFS CLI (the branch name is the session id):

```bash
branchfs list   --storage <store>
branchfs status <session> --storage <store> --json
branchfs commit-branch <session> --storage <store>   # apply deltas to base
branchfs abort-branch  <session> --storage <store>   # discard the branch
```
