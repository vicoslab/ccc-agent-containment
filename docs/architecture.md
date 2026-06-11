# ccc-agent architecture: trust boundaries and contained roots

This document covers the runtime mechanics. The accepted cross-repo design
lives in the workspace root (`CCC_AGENT_BRANCHFS_PROTECTION_REVIEW_DESIGN.md`);
this is the implementation view.

## Trust split

```text
untrusted: the agent process tree (codex/claude/hermes/... and children)
trusted:   ccc-agent-run / ccc-agentctl / ccc_agent (supervisor)
           branchfs daemon + store
           ccc-fuse-sidecar (privileged FUSE broker)
           chroot assembler (root)
```

The agent can *produce branch deltas*. Only the supervisor can freeze,
inspect, commit, or abort them. This holds because:

1. agent-visible paths are BranchFS **agent mounts** (`--agent`): the
   `.branchfs_ctl` control file and `@branch` virtual dirs are not exposed;
2. the BranchFS store (deltas, tombstones, metadata, `daemon.sock`) lives
   outside the agent view — commit requests are only accepted over that
   socket;
3. session state (`<state_dir>/sessions`, `reviews`) is outside the view and
   additionally covered by the `.ccc-agent` deny pattern;
4. hooks invoke `ccc-agentctl finish-turn` which records an event — there is
   no hook path that commits.

## Session lifecycle (process-exit completion, first milestone)

```text
created -> mounting -> running -> finalizing -> frozen
        -> auto-committed | pending-review | committed | aborted | failed
```

- `ccc-agent-run` materializes one branch per protected root (branch name =
  session id), mounts agent views, runs the command with
  `CCC_AGENT_SESSION` set, and finalizes on exit.
- Freeze happens **after** completion (never before bounded self-repair, when
  that lands), then `branchfs status --json` per root feeds the policy engine.
- `pending-review` keeps branches frozen; the branchfs daemon may exit (it
  auto-exits with its last mount) — `ccc-agentctl commit/abort` re-ensures it
  from session metadata (`branchfs start-daemon --base ... --storage ...`).
- Commit failures never abort: the branch is preserved and the session is
  marked `failed` for manual recovery.
- Nested agents: a shim or `ccc-agent-run` invoked with `CCC_AGENT_SESSION`
  already set reuses the outer session — one review unit per task, no branch
  explosion.

## Contained root layout (chroot mode)

`scripts/ccc-agent-chroot.sh` (dry-run by default; `--apply` requires root)
assembles, inside `unshare -m` so the agent cannot undo it:

```text
/run/ccc-agent/chroots/<session-id>/
  usr bin sbin lib lib64 etc opt    read-only binds of the container image
  proc                              fresh procfs
  dev                               minimal nodes (null zero full urandom random tty)
  tmp                               private tmpfs (session-scoped)
  storage/user                      BranchFS agent view (rw)
  home/<user>                       same view or its --home-subdir (rw)
  run/ccc-agent/session             read-only session id marker
```

Deliberately absent: real `/storage/*` underlays, `/__branchfs_store`,
`daemon.sock`, `/var/run/docker.sock`, the supervisor state dir, and any
writable path that aliases the underlay. `/home/$USER` and `/storage/user`
bind the **same** view (alias rule), never two separate branches.

The agent is entered with `setpriv --reuid/--regid --init-groups` and a
scrubbed environment (`env -i` + explicit allowlist), which drops privileges
and closes the inherited-env escape route. Launcher code must not leak open
directory fds pointing outside the root.

In **no-chroot dev mode** (what `ccc-agent-run` does today) the agent runs
with its cwd inside the mounted view; visible system paths are unchanged.
That mode still gives rollback/review for everything written through the
view, but relies on CCC's container hardening for the rest — use chroot mode
for actual YOLO agents once FUSE mounts are validated on the node.

## FUSE plumbing

Normal CCC app containers have no `CAP_SYS_ADMIN`; BranchFS mounts go through
`ccc-fuse-sidecar`:

```text
branchfs (unprivileged, in-container)
  -> fusermount3 shim                  (PATH-installed by CCC base image)
  -> /run/ccc-fuse-sidecar/fuse.sock   (host sidecar, SYS_ADMIN)
  -> /dev/fuse open + mount(2), fd passed back SCM_RIGHTS
```

The sidecar stays policy-free. Path translation between the container and
sidecar namespaces is the sidecar's Docker-inspect mode (see that repo's
README); BranchFS mountpoints under `/__branchfs_mounts/...` must resolve to
the same host paths in both namespaces or be translated.

## Runtime validation (blocked in unprivileged dev containers)

Everything in `agent/tests` runs without FUSE. What still needs a privileged
host (e.g. `donbot-domen-cuda10` with `ENABLE_FUSE`):

```bash
# 1. real agent mount through the sidecar
branchfs start-daemon --base /__real/storage_user --storage /__branchfs_store/storage_user
branchfs create agent-smoke --storage /__branchfs_store/storage_user --hide .ssh
branchfs mount --storage /__branchfs_store/storage_user --branch agent-smoke --agent /__branchfs_mounts/storage_user
ls /__branchfs_mounts/storage_user            # must NOT list .ssh
# 2. chroot assembly
scripts/ccc-agent-chroot.sh --session-id smoke-1 \
  --view /__branchfs_mounts/storage_user --user $USER --uid $(id -u) --gid $(id -g) \
  --apply -- sh -c 'ls /storage/user && touch /storage/user/Projects/x'
# 3. full cycle
ccc-agent-run --workspace /storage/user/Projects/x -- sh -c 'echo ok > done.txt'
```
