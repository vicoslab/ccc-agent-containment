# ccc-agent — BranchFS containment for agents in CCC containers

Run Codex / Claude Code / Hermes / OpenCode (and any command) against CCC
storage **without giving them direct write access to real data**. The agent
works in a node-local BranchFS branch view; a trusted supervisor freezes the
branch when the agent finishes, classifies the changes against a path policy,
and only then commits to the real NFS-backed underlay — or parks the session
for human review, or discards it.

```text
ccc-agent-run -- codex exec "implement feature X"
   create session -> branch bundle -> (chroot) agent run ->
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

Review pending sessions:

```bash
ccc-agentctl list
ccc-agentctl diff   <session-id>
ccc-agentctl show   <session-id>
ccc-agentctl commit <session-id>     # apply to real storage
ccc-agentctl abort  <session-id>     # discard branch
```

Durable review artifacts (summary.md, per-root status JSON, policy decision)
land under `<state_dir>/reviews/<session-id>/`.

## Layers

| Piece | Role |
|---|---|
| `bin/ccc-agent-run` | trusted launcher: session + branch bundle + agent + finalize |
| `bin/ccc-agent-launch` | same, used by transparent shims (workspace = `$PWD`) |
| `bin/ccc-agentctl` | operator CLI: list/show/status/diff/commit/abort/thaw/finish/finish-turn |
| `ccc_agent/` | stdlib-only Python: session store, policy engine, BranchFS driver |
| `scripts/ccc-agent-chroot.sh` | privileged chroot assembly (dry-run by default) |
| `shims/ccc-agent-shim.sh` | transparent `codex`/`claude`/... PATH shims |
| `hooks/` | Stop/finish-turn adapters for Claude Code, Codex, Hermes |
| `config/` | runtime config + Claude managed-settings examples |

Design references: `docs/architecture.md` (trust boundaries, chroot layout),
`docs/policy.md` (path policy + secret hiding), and the workspace-level
`CCC_AGENT_BRANCHFS_PROTECTION_REVIEW_DESIGN.md`.

## Deployment

```text
/opt/ccc-agent/
  bin/ccc-agent-run ccc-agent-launch ccc-agentctl   (+ branchfs binary)
  ccc_agent/                                        (python package)
  scripts/ hooks/ shims/ config/
/etc/ccc-agent/config.json                          (root-owned)
```

Hard rules:

- `config.json`, the state dir pointer, and everything under `/opt/ccc-agent`
  must **not** be writable by the agent user;
- the BranchFS store and real underlay paths must not be visible inside the
  agent's view (the chroot assembler enforces this; in no-chroot dev mode the
  `.ccc-agent` deny pattern is the fallback);
- hooks report; only the supervisor/operator commits.

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
`CCC_AGENT_BRANCHFS_BIN`). Real FUSE mount + chroot `--apply` validation needs
a privileged host; see *Runtime validation* in `docs/architecture.md`.
