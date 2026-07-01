# ccc-agent — BranchFS containment for agents in CCC containers

Run Codex / Claude Code / Hermes / OpenCode (and any command) against CCC
storage **without giving them direct write access to real data**. The agent
works in a node-local BranchFS branch view; a trusted supervisor freezes the
branch when the agent finishes, classifies the changes against a path policy,
and only then commits to the real NFS-backed underlay — or parks the session
for human review, or discards it.

```text
ccc-agent run -- codex exec "implement feature X"
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
ccc-agent run --workspace /home/$USER/Projects/foo \
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

At each Stop boundary the agent's hook calls `ccc-agent finalize-turn` over
the control socket. In-scope changes auto-commit and the agent continues;
out-of-scope changes are reported to the user, who responds (relayed by the
agent) with one of:

```bash
ccc-agent approve-turn <token>            # accept all flagged changes
ccc-agent approve-turn <token> keep       # keep deltas, don't commit (continue)
ccc-agent approve-turn <token> revert     # reject; the agent undoes them
ccc-agent approve-turn <token> --paths a,b # commit only a,b (file-by-file)
```

### Post-session review (operator) + lingering sessions

A session that exits with un-committed changes stays as a reviewable branch:

```bash
ccc-agent list                              # sessions + states
ccc-agent review <session>                  # browse the diff
ccc-agent review <session> --accept         # commit everything
ccc-agent review <session> --reject         # discard everything
ccc-agent review <session> --commit a,b     # commit only a,b (file-by-file)
ccc-agent review <session> --emit-patch > c.patch  # line-by-line: prune hunks…
ccc-agent review <session> --apply-patch c.patch   # …then apply
```

Or directly via the BranchFS CLI (branch name == session id):

```bash
branchfs list   --storage <store>
branchfs status <session> --storage <store> --json
branchfs commit-branch <session> --storage <store>   # apply deltas to base
branchfs abort-branch  <session> --storage <store>   # discard the branch
```

Durable review artifacts (summary.md, per-root status JSON, policy decision)
land under `<state_dir>/<session-id>/reviews/`. Other non-store runtime data
for the same run is bundled nearby, e.g.
`<state_dir>/<session-id>/session/session.json`,
`<state_dir>/<session-id>/mounts/`, and
`<state_dir>/<session-id>/control/control.sock`. BranchFS stores/deltas stay at
the configured root `store` paths.

### Credentials

Agents authenticate from `~/.codex` / `~/.claude`. These directories should
remain **writable inside the BranchFS view**: Codex/Claude create logs, session
state, caches, lock files, and sometimes refreshed token material there. Binding
the whole directories read-only makes the real agents fail.

The containment plugin itself must not live there. In system deployments,
install the package/hooks as root-owned files under `/usr` (or another OS path
that bwrap exposes read-only), write `config.json` under `/etc/ccc-agent`, and
use Claude managed settings under `/etc/claude-code`. The user-writable
`~/.codex` / `~/.claude` trees are only agent state/auth input; any deltas they
produce are covered by policy `ignore_patterns` and discarded unless explicitly
reviewed.

`cred_mounts` remains available only for narrow special-case read-only overlays;
do **not** use it for the whole `~/.codex` or `~/.claude` directories. API-key
deployments can still combine `cred_mask` (overmount an individual secret file
with `/dev/null`) and `cred_env` (supervisor reads a credential and passes it as
env). OAuth-subscription logins (ChatGPT / Claude account — the common case)
authenticate from token files, so those files must remain readable through the
BranchFS view.

## Layers

| Piece | Role |
|---|---|
| `bin/ccc-agent` / `ccc-agent run` | trusted launcher: session + branch bundle + agent + finalize; also used by transparent shims (workspace = `$PWD`) |
| `ccc-agent list/show/diff/...` | operator + in-sandbox control ops: list/show/diff/commit/abort/review/finalize-turn/approve-turn |
| `ccc-agent setup` | installer/wiring op: config, plugin entries, optional transparent PATH shims |
| `ccc_agent/` | stdlib-only Python: session store, policy engine, BranchFS driver, bwrap assembler, control socket + per-turn handler |
| `shims/ccc-agent-shim.sh` | transparent `codex`/`claude`/... PATH shims |
| `assets/plugins/` | native CCC lifecycle-hook plugins (`claude-`/`codex-`/`hermes-ccc-containment`) injected read-only per contained run |
| `hooks/` | standalone Stop-hook / `notify` adapter scripts (manual/fallback registration) |
| `config/` | runtime config example + legacy claude/codex overlay examples (fallback only) |

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

It's a pip package (stdlib-only, no dependencies) with one `ccc-agent` console
script and the shell hooks/shims bundled as package data.

```bash
# system (CCC images / shared): install into the SYSTEM python so the entry
# points are conda-independent (shebang pinned to /usr/bin/python3) and visible
# inside the bwrap sandbox (which only exposes /usr):
/usr/bin/python3 -m pip install --break-system-packages \
    "git+https://github.com/vicoslab/ccc-agent.git@master"
ccc-agent setup --system --branchfs-bin /usr/local/bin/branchfs --bwrap-bin "$(command -v bwrap)"

# user / dev:
python3 -m pip install --user "git+https://github.com/vicoslab/ccc-agent.git"
ccc-agent setup --user
```

`ccc-agent setup` does what pip can't: writes `config.json` with the
`agent_plugins` map, makes the bundled plugin hook scripts executable, and
optionally installs the transparent PATH shims (`--enable-shims`). It does
**not** edit the user's `~/.codex/config.toml`, `~/.claude/settings.json`, or
live Claude managed settings. Instead, for a contained run only, `ccc-agent run`
loads CCC hooks through each agent's **native plugin mechanism**: it bind-mounts
the matching read-only plugin (`ccc_agent/assets/plugins/…`) into the sandbox,
adds Claude's `--plugin-dir`, drops the Codex plugin at the in-sandbox Codex
plugin path, and sets Hermes' `HERMES_BUNDLED_PLUGINS`. Direct, uncontained
`codex`/`claude`/`hermes` runs load none of this. By default `ccc-agent run`
infers the plugin from the command executable basename, including absolute paths.
For example, these load the matching plugin:

- `ccc-agent run -- codex exec ...`
- `ccc-agent run -- /path/to/claude -p ...`

Use `--agent <name>` only for an explicit override; explicit selection wins over
executable-path inference.
Disable injection with `--no-agent-plugins` (alias `--no-hooks`).

The plugins live under the installed package (`ccc_agent/assets/plugins/…`),
which is under `/usr` for a system install and therefore exposed read-only
inside the sandbox. See `docs/agent-integration.md` for the per-agent activation
and turn-boundary matrix.

The branchfs binary and bwrap are separate (not pip-installable); in CCC images
the runit startup installs them and runs the two commands above (see the CCC
image repo's `06_ccc_agent.sh`).

## Deployment

Hard rules:

- `config.json`, the state dir pointer, and the plugin/hook source assets
  must **not** be writable by the agent user (mounted read-only in the sandbox);
- the BranchFS store and real underlay paths must not be visible inside the
  agent's view (bwrap mode enforces this by exposing only the view; in `none`
  debug mode the `.ccc-agent` deny pattern is the only fallback);
- hooks report and at most request self-repair (`ccc-agent
  check-before-final` exits 2 with the offending paths while the per-session
  repair budget lasts); only the supervisor/operator commits.

Shims (optional, after the explicit wrapper works for you):

```bash
# Simple case: no conda env shadows /usr/local/bin.
ccc-agent setup --system --enable-shims

# Conda-compatible case: use a dedicated trusted shim directory and install
# activation hooks in the env that contains codex/claude. On every `conda
# activate`, the hook re-prepends the shim directory ahead of $CONDA_PREFIX/bin;
# the shim then skips itself and resolves the real binary from the active env.
conda activate my-agent-env
ccc-agent setup --user --enable-shims \
    --link-dir "$HOME/.local/share/ccc-agent/shims" \
    --conda-activate-shims --conda-prefix "$CONDA_PREFIX"

# Verify precedence: this should print the shim path, not $CONDA_PREFIX/bin/codex.
command -v codex
```

Do **not** install the transparent `codex`/`claude` shim directly into the same
`$CONDA_PREFIX/bin` that contains the real binary: that overwrites or masks the
real executable. Use a separate shim directory and the conda activation hook so
PATH becomes:

```text
<trusted-shim-dir>:$CONDA_PREFIX/bin:...
```

The same generic shim is symlinked as each agent name:

```bash
ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/codex
ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/claude
# nested agent calls reuse the outer session via CCC_AGENT_SESSION
```

## Tests

```bash
python3 -m unittest discover                    # all non-FUSE, stdlib-only
```

Integration against a real `branchfs` binary (still no FUSE — daemon socket
only) runs automatically when the binary is found (or set
`CCC_AGENT_BRANCHFS_BIN`). Real FUSE mount + bwrap end-to-end validation
(`scripts/test-e2e-bwrap.sh`) needs a host with `/dev/fuse` and unprivileged
user namespaces; see *Runtime validation* in `docs/architecture.md`.
