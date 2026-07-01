# Integrating real agents (codex / claude / hermes) with ccc-agent

How each agent reaches the per-turn control channel through its **native plugin
mechanism**, how the plugin is injected, and how credentials are handled.

## Turn-boundary matrix

| Invocation | Turn boundary | How finalize happens | Approval flow |
|---|---|---|---|
| `codex exec "…"` | process exit (1 turn) | supervisor **process-exit finalize** (no hook) | session-end review |
| `claude -p "…"` | process exit (1 turn) | supervisor **process-exit finalize** (no hook) | session-end review |
| `claude` (interactive) | each Stop | plugin **Stop hook** → `ccc-agent finalize-turn` | **blocking** per-turn (exit 2) |
| `codex` (interactive) | each Stop | plugin **Stop hook** → `ccc-agent finalize-turn` | best-effort per-turn (see below) |
| `hermes` (interactive) | each turn / session end | plugin **`post_llm_call` / `on_session_end`** → `ccc-agent finalize-turn` | report-only (see below) |

**Non-interactive (`exec`/`-p`) needs no hook** — one turn per process, so the
supervisor's existing end-of-process finalize is the per-turn commit.

**Claude interactive** loads a CCC plugin whose Stop hook can *block* the stop
(exit 2), so out-of-scope changes prompt the user mid-turn and commit only on
`approve-turn`.

**Codex interactive** loads a CCC plugin whose `hooks/hooks.json` registers the
blocking `Stop` event. Whether a given Codex build honours the hook's exit code
(blocking the stop) is version-dependent; treat per-turn Codex approval as
**best-effort**. If the installed Codex does not block on Stop, out-of-scope
changes defer to **session-end review** (`pending-review`) — they are never
silently committed.

**Hermes** loads a CCC bundled plugin (`HERMES_BUNDLED_PLUGINS`) whose
`post_llm_call` / `on_session_end` hooks report turn boundaries. Hermes hooks
cannot block or feed instructions back, so — like the old Codex `notify` path —
in-scope changes auto-commit per turn and out-of-scope changes defer to
session-end review.

Hooks are **best-effort turn-boundary signals only**. If a plugin fails to load,
a hook crashes, or an agent version changes the contract, the agent loses
per-turn convenience but the trusted **process-exit freeze → status → policy →
review** path still runs and never grants the agent commit authority.

## Plugin injection (no config-file overlay)

CCC hooks are delivered through each agent's **native plugin mechanism**, not by
overwriting the user's normal Codex/Claude/Hermes config. `ccc-agent setup`
records an `agent_plugins` entry per agent pointing at root-owned, read-only
package assets under `ccc_agent/assets/plugins/`. For a contained run only,
`ccc-agent run` bind-mounts the matching plugin read-only into the bwrap sandbox,
inserts any activation `argv` right after the agent executable, and exports any
`setenv`. Direct, uncontained `codex` / `claude` / `hermes` invocations load none
of this, and no user config file is edited or hidden.

When no explicit agent flag is provided, `ccc-agent run` infers the plugin from
the executable basename, including absolute paths such as `/opt/agents/bin/codex`:

```text
ccc-agent run -- codex exec "…"      # loads the Codex plugin
ccc-agent run -- /path/to/claude -p "…"  # loads the Claude plugin
```

Use `--agent <name>` only when you want an explicit override; explicit selection
wins over executable-path inference.

**Claude Code** — session-only plugin via the native `--plugin-dir` flag:

```text
ccc-agent run -- claude -p "…"
  → claude --plugin-dir /ccc-agent/plugins/claude-ccc-containment -p "…"
```

The plugin dir (`.claude-plugin/plugin.json` + `hooks/hooks.json` →
`${CLAUDE_PLUGIN_ROOT}/hooks/ccc-stop-hook.sh`) is a read-only bwrap mount of the
package asset. `--bare` disables plugins/hooks, so a contained `--bare` run skips
injection and falls back to session-end review.

**Codex** — the plugin (`.codex-plugin/plugin.json` + `hooks/hooks.json` →
`./hooks/ccc-stop-hook.sh`) is mounted read-only at the in-sandbox Codex plugin
path (`~/.codex/plugins/ccc-agent`). The `argv` field is left tunable
for the installed Codex version's enable/trust flags.

**Hermes** — the bundled plugin (`plugin.yaml` + a `register()` module) is
mounted under a read-only bundle root and activated with
`HERMES_BUNDLED_PLUGINS=/ccc-agent/plugins/hermes` and `HERMES_ACCEPT_HOOKS=1`.

Disable all injection with `ccc-agent setup --no-agent-plugins` (alias
`--no-hooks`), which sets `agent_hook_mode: "disabled"`.

## Credentials and writable agent state

`~/.codex` and `~/.claude` are **agent runtime state**, not trusted plugin
storage. They must stay writable inside the sandbox because real agents create
logs, session files, caches, lock files, and sometimes refreshed tokens there.
Do not bind the whole directories read-only.

Instead, system deployments protect the containment plugin by installing it
outside `$HOME`:

- package code, plugin manifests, and hook scripts live in a root-owned
  Python/package location under `/usr` (or another OS path exposed read-only by
  bwrap);
- `config.json` lives under `/etc/ccc-agent` and is root-owned;
- the per-agent CCC plugins are bind-mounted **read-only** into the sandbox only
  for a matching contained agent, so the untrusted agent can load but never edit
  the hook source;
- direct, uncontained `codex`/`claude`/`hermes` runs do not load CCC plugins.

The writable `~/.codex` / `~/.claude` trees are visible through the BranchFS
view. Their changes are matched by policy `ignore_patterns` and discarded unless
a human explicitly reviews/commits them.

`cred_mounts` remains available only for narrow special-case read-only overlays;
do **not** use it for whole agent config/state directories. `cred_mask` and
`cred_env` are for API-key deployments where an individual secret file can be
masked and the supervisor can pass the key via env. OAuth-subscription logins
(codex `auth.json` with `tokens`, claude `.credentials.json`) authenticate from
files, so those files must remain readable through the BranchFS view.

## Browsing / cleaning lingering sessions

A session that exits with un-committed deltas stays as a reviewable branch:

```bash
ccc-agent list                       # sessions + states
ccc-agent review <session>           # browse the diff
ccc-agent review <session> --accept  # commit all
ccc-agent review <session> --reject  # discard all
ccc-agent review <session> --commit a,b   # commit only a,b (rest discarded)
ccc-agent review <session> --emit-patch > c.patch   # line-level: prune hunks…
ccc-agent review <session> --apply-patch c.patch    # …then apply
```

Or directly via the BranchFS CLI (the branch name is the session id):

```bash
branchfs list   --storage <store>
branchfs status <session> --storage <store> --json
branchfs commit-branch <session> --storage <store>   # apply deltas to base
branchfs abort-branch  <session> --storage <store>   # discard the branch
```
