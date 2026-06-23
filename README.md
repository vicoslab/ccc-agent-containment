# ccc-agent — BranchFS containment for agents in CCC containers

Run Codex / Claude Code / Hermes / OpenCode (and any command) against CCC
storage **without giving them direct write access to real data**. The agent
works in a node-local BranchFS branch view; a trusted supervisor freezes the
branch when the agent finishes, classifies the changes against a path policy,
and only then commits to the real NFS-backed underlay — or parks the session
for human review, or discards it.

```text
ccc-agent-run -- codex exec "implement feature X"
   create session -> branch bundle -> (bwrap) agent run ->
   freeze -> status -> policy -> auto-commit | pending-review | abort
```

Everything in this directory is **non-invasive scaffolding**: nothing here is
wired into CCC image startup by default. Deploy by copying to `/opt/ccc-agent`
(see *Deployment*).

## Quick start (explicit wrapper)

```bash
# config: protected roots, state dir, branchfs binary (see config/config.example.json)
export CCC_AGENT_CONFIG=/etc/ccc-agent/config.json

# run any command contained; workspace defaults to $PWD
ccc-agent-run --workspace /home/$USER/Projects/foo \
              --policy workspace-auto \
              --agent codex \
              -- codex exec "implement feature X"
```

Outcome per policy:

- all changes inside the workspace, no deny rule hit → **auto-committed**;
- anything outside scope / deny match (`.ssh`, `.env`, `.git/hooks`, ...) →
  **pending-review** (branch stays frozen, nothing touched the underlay);
- `--policy throwaway` → branch aborted at completion;
- no changes → session closes as a no-op.

### Per-turn review (interactive, in the agent UI)

At each Stop boundary the agent's hook calls `ccc-agentctl finalize-turn` over
the control socket. In-scope changes auto-commit and the agent continues;
out-of-scope changes are reported to the user, who responds (relayed by the
agent) with one of:

```bash
ccc-agentctl approve-turn <token>            # accept all flagged changes
ccc-agentctl approve-turn <token> keep       # keep deltas, don't commit (continue)
ccc-agentctl approve-turn <token> revert     # reject; the agent undoes them
ccc-agentctl approve-turn <token> --paths a,b # commit only a,b (file-by-file)
```

### Post-session review (operator) + lingering sessions

A session that exits with un-committed changes stays as a reviewable branch:

```bash
ccc-agentctl list                              # sessions + states
ccc-agentctl review <session>                  # browse the diff
ccc-agentctl review <session> --accept         # commit everything
ccc-agentctl review <session> --reject         # discard everything
ccc-agentctl review <session> --commit a,b     # commit only a,b (file-by-file)
ccc-agentctl review <session> --emit-patch > c.patch  # line-by-line: prune hunks…
ccc-agentctl review <session> --apply-patch c.patch   # …then apply
```

Or directly via the BranchFS CLI (branch name == session id):

```bash
branchfs list   --storage <store>
branchfs status <session> --storage <store> --json
branchfs commit-branch <session> --storage <store>   # apply deltas to base
branchfs abort-branch  <session> --storage <store>   # discard the branch
```

Durable review artifacts (summary.md, per-root status JSON, policy decision)
land under `<state_dir>/reviews/<session-id>/`.

### Credentials

Agents authenticate from `~/.codex` / `~/.claude`. Those are re-exposed into the
sandbox via `cred_mounts` (read-only), secret files can be masked with
`cred_mask` (overmounted `/dev/null`), and an API key can be passed by env via
`cred_env`. **OAuth-subscription logins** (ChatGPT / Claude account — the common
case) authenticate from the **token file**, which must stay readable in the
sandbox (don't mask it); env-passing only replaces API-key auth. See
[`docs/agent-integration.md`](docs/agent-integration.md) for hook registration
(claude Stop hook, codex `notify`) and the full credential/turn-boundary matrix.

## Layers

| Piece | Role |
|---|---|
| `bin/ccc-agent-run` | trusted launcher: session + branch bundle + agent + finalize |
| `bin/ccc-agent-launch` | same, used by transparent shims (workspace = `$PWD`) |
| `bin/ccc-agentctl` | operator + in-sandbox CLI: list/show/diff/commit/abort/review/finalize-turn/approve-turn |
| `ccc_agent/` | stdlib-only Python: session store, policy engine, BranchFS driver, bwrap assembler, control socket + per-turn handler |
| `shims/ccc-agent-shim.sh` | transparent `codex`/`claude`/... PATH shims |
| `hooks/` | Stop-hook (claude/codex) + codex `notify` adapters that signal `finalize-turn` over the control socket |
| `config/` | runtime config + claude managed-settings + codex config examples |

Confinement modes (`confinement` in config.json):

- **`bwrap`** (default, the real boundary): runs the agent in a rootless
  bubblewrap user+mount+pid namespace — OS read-only, the BranchFS view
  read-write at its visible path, the real underlay/store hidden. No container
  `CAP_SYS_ADMIN` needed (just unprivileged user namespaces). `bwrap_bin` and
  `bwrap_proc_mode` (`bind`|`ro`|`fresh`) are configurable.
- **`none`** (debug only — *not* a security boundary): runs the agent with its
  cwd inside the view but nothing else isolated; absolute-path writes bypass the
  view. Use only to exercise the policy/commit pipeline without bwrap.

Design references: `docs/architecture.md` (trust boundaries, sandbox layout),
`docs/policy.md` (path policy + secret hiding), and the workspace-level
`CCC_AGENT_BRANCHFS_PROTECTION_REVIEW_DESIGN.md`.

## Install

It's a pip package (stdlib-only, no dependencies) with console-script entry
points and the shell hooks/shims bundled as package data.

```bash
# system (CCC images / shared): install into the SYSTEM python so the entry
# points are conda-independent (shebang pinned to /usr/bin/python3) and visible
# inside the bwrap sandbox (which only exposes /usr):
/usr/bin/python3 -m pip install --break-system-packages \
    "git+https://github.com/vicoslab/ccc-agent-containment.git@master"
ccc-agent-setup --system --branchfs-bin /usr/local/bin/branchfs --bwrap-bin "$(command -v bwrap)"

# user / dev:
python3 -m pip install --user "git+https://github.com/vicoslab/ccc-agent-containment.git"
ccc-agent-setup --user
```

`ccc-agent-setup` does what pip can't: writes `config.json`, registers the
Claude Stop hook (managed settings for `--system`, `~/.claude/settings.json`
for `--user`) and the codex `notify` hook, and optionally installs the
transparent PATH shims (`--enable-shims`). The hooks it registers point into
the installed package (`ccc_agent/assets/hooks/…`), which lives under `/usr`
for a system install and is therefore exposed read-only inside the sandbox.

The branchfs binary and bwrap are separate (not pip-installable); in CCC images
the runit startup installs them and runs the two commands above (see the CCC
image repo's `06_ccc_agent_containment.sh`). The legacy
`install-ccc-agent-plugin.sh` remains as a hand-rolled user-level installer.

## Deployment

Hard rules:

- `config.json`, the state dir pointer, and everything under `/opt/ccc-agent`
  must **not** be writable by the agent user;
- the BranchFS store and real underlay paths must not be visible inside the
  agent's view (bwrap mode enforces this by exposing only the view; in `none`
  debug mode the `.ccc-agent` deny pattern is the only fallback);
- hooks report and at most request self-repair (`ccc-agentctl
  check-before-final` exits 2 with the offending paths while the per-session
  repair budget lasts); only the supervisor/operator commits.

Shims (optional, after the explicit wrapper works for you):

```bash
ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/codex
ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/claude
# nested agent calls reuse the outer session via CCC_AGENT_SESSION
```

## Tests

```bash
cd agent && python3 -m unittest discover          # all non-FUSE, stdlib-only
```

Integration against a real `branchfs` binary (still no FUSE — daemon socket
only) runs automatically when the binary is found (or set
`CCC_AGENT_BRANCHFS_BIN`). Real FUSE mount + bwrap end-to-end validation
(`scripts/test-e2e-bwrap.sh`) needs a host with `/dev/fuse` and unprivileged
user namespaces; see *Runtime validation* in `docs/architecture.md`.
