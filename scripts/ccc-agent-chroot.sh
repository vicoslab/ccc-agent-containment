#!/bin/bash
# ccc-agent-chroot.sh — assemble a contained root for one agent session and
# exec a command inside it as a non-root user.
#
# The agent must only ever see BranchFS branch views of CCC data; the real
# NFS underlay, the BranchFS store, and the daemon control socket stay
# outside the chroot. This script is the privileged part of ccc-agent-run:
# it runs as root inside the (unprivileged) CCC container, builds the root
# in a private mount namespace, drops to the agent user, and execs the
# agent command.
#
# SAFE BY DEFAULT: without --apply this only prints the assembly plan.
#
# Usage:
#   ccc-agent-chroot.sh \
#     --session-id agent-20260611T120000Z-abc12345 \
#     --view /__branchfs_mounts/storage_user \
#     --user domen --uid 1000 --gid 1000 \
#     [--home-subdir ""] \
#     [--workdir /storage/user/Projects/proj-a] \
#     [--extra-view name=/path/to/view:/visible/path]... \
#     [--chroot-root /run/ccc-agent/chroots] \
#     [--apply] \
#     -- command args...
#
# Layout produced (all binds happen inside `unshare -m`, so the namespace
# disappears with the agent process and the agent cannot undo it):
#   $ROOT/usr,/bin,/sbin,/lib,/lib64,/etc,/opt   read-only binds of container
#   $ROOT/proc                                    fresh proc mount
#   $ROOT/dev                                     minimal (null zero urandom tty)
#   $ROOT/tmp                                     private session tmpfs
#   $ROOT/storage/user                            BranchFS view (rw)
#   $ROOT/home/$USER                              same view (or its home subdir)
#   $ROOT/run/ccc-agent/session                   session id marker (ro)
set -euo pipefail

SESSION_ID=""
VIEW=""
AGENT_USER=""
AGENT_UID=""
AGENT_GID=""
HOME_SUBDIR=""
WORKDIR=""
CHROOT_BASE="/run/ccc-agent/chroots"
APPLY=0
declare -a EXTRA_VIEWS=()
declare -a COMMAND=()

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --session-id) SESSION_ID="$2"; shift 2 ;;
        --view) VIEW="$2"; shift 2 ;;
        --user) AGENT_USER="$2"; shift 2 ;;
        --uid) AGENT_UID="$2"; shift 2 ;;
        --gid) AGENT_GID="$2"; shift 2 ;;
        --home-subdir) HOME_SUBDIR="$2"; shift 2 ;;
        --workdir) WORKDIR="$2"; shift 2 ;;
        --extra-view) EXTRA_VIEWS+=("$2"); shift 2 ;;
        --chroot-root) CHROOT_BASE="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; COMMAND=("$@"); break ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$SESSION_ID" ]] || { echo "--session-id is required" >&2; exit 2; }
[[ -n "$VIEW" ]] || { echo "--view is required" >&2; exit 2; }
[[ -n "$AGENT_USER" ]] || { echo "--user is required" >&2; exit 2; }
[[ ${#COMMAND[@]} -gt 0 ]] || { echo "no command given (use -- cmd...)" >&2; exit 2; }
case "$SESSION_ID" in
    */*|*..*) echo "invalid session id: $SESSION_ID" >&2; exit 2 ;;
esac

ROOT="$CHROOT_BASE/$SESSION_ID"
RO_DIRS=(/usr /bin /sbin /lib /lib64 /etc /opt)

plan() { printf 'PLAN %s\n' "$*"; }

emit_plan() {
    plan "mkdir -p $ROOT"
    for dir in "${RO_DIRS[@]}"; do
        [[ -d "$dir" || -L "$dir" ]] || continue
        plan "bind-ro $dir -> $ROOT$dir"
    done
    plan "mount proc -> $ROOT/proc"
    plan "mount tmpfs(private) -> $ROOT/tmp"
    plan "dev minimal (null zero full urandom random tty) -> $ROOT/dev"
    plan "bind-rw $VIEW -> $ROOT/storage/user"
    if [[ -n "$HOME_SUBDIR" ]]; then
        plan "bind-rw $VIEW/$HOME_SUBDIR -> $ROOT/home/$AGENT_USER"
    else
        plan "bind-rw $VIEW -> $ROOT/home/$AGENT_USER"
    fi
    for spec in "${EXTRA_VIEWS[@]:-}"; do
        [[ -n "$spec" ]] || continue
        local_view="${spec#*=}"
        src="${local_view%%:*}"
        dst="${local_view##*:}"
        plan "bind-rw $src -> $ROOT$dst"
    done
    plan "write session marker -> $ROOT/run/ccc-agent/session"
    plan "NOT exposed: real underlay, BranchFS store, daemon.sock, docker.sock"
    [[ -n "$WORKDIR" ]] && plan "workdir (inside chroot): $WORKDIR"
    plan "exec: unshare -m chroot $ROOT setpriv --reuid=$AGENT_UID --regid=$AGENT_GID --init-groups env CCC_AGENT_SESSION=$SESSION_ID HOME=/home/$AGENT_USER ${WORKDIR:+PWD=$WORKDIR }${COMMAND[*]}"
}

if [[ "$APPLY" -eq 0 ]]; then
    echo "# dry-run (use --apply as root to execute)"
    emit_plan
    exit 0
fi

[[ "$(id -u)" -eq 0 ]] || { echo "--apply requires root" >&2; exit 1; }
[[ -n "$AGENT_UID" && -n "$AGENT_GID" ]] || {
    echo "--apply requires --uid and --gid" >&2; exit 2; }
[[ -d "$VIEW" ]] || { echo "view not mounted: $VIEW" >&2; exit 1; }

# Re-exec the whole assembly inside a private mount namespace so every bind
# below is invisible outside and torn down when the agent exits.
if [[ -z "${CCC_AGENT_CHROOT_NS:-}" ]]; then
    export CCC_AGENT_CHROOT_NS=1
    exec unshare -m --propagation private -- "$0" \
        --session-id "$SESSION_ID" --view "$VIEW" \
        --user "$AGENT_USER" --uid "$AGENT_UID" --gid "$AGENT_GID" \
        --home-subdir "$HOME_SUBDIR" --workdir "$WORKDIR" \
        --chroot-root "$CHROOT_BASE" \
        $(for spec in "${EXTRA_VIEWS[@]:-}"; do
              [[ -n "$spec" ]] && printf -- '--extra-view %q ' "$spec"
          done) \
        --apply -- "${COMMAND[@]}"
fi

mkdir -p "$ROOT"

for dir in "${RO_DIRS[@]}"; do
    [[ -d "$dir" || -L "$dir" ]] || continue
    if [[ -L "$dir" ]]; then
        # e.g. /bin -> usr/bin on merged-usr systems
        target="$(readlink "$dir")"
        ln -sfn "$target" "$ROOT$dir"
        continue
    fi
    mkdir -p "$ROOT$dir"
    mount --bind "$dir" "$ROOT$dir"
    mount -o remount,bind,ro,nosuid,nodev "$ROOT$dir"
done

mkdir -p "$ROOT/proc" "$ROOT/tmp" "$ROOT/dev"
mount -t proc proc "$ROOT/proc"
mount -t tmpfs -o nosuid,nodev,mode=1777 tmpfs "$ROOT/tmp"
for node in null zero full urandom random tty; do
    touch "$ROOT/dev/$node"
    mount --bind "/dev/$node" "$ROOT/dev/$node"
done
ln -sfn /proc/self/fd "$ROOT/dev/fd"
ln -sfn /proc/self/fd/0 "$ROOT/dev/stdin"
ln -sfn /proc/self/fd/1 "$ROOT/dev/stdout"
ln -sfn /proc/self/fd/2 "$ROOT/dev/stderr"

mkdir -p "$ROOT/storage/user" "$ROOT/home/$AGENT_USER"
mount --bind "$VIEW" "$ROOT/storage/user"
if [[ -n "$HOME_SUBDIR" ]]; then
    mkdir -p "$VIEW/$HOME_SUBDIR"
    mount --bind "$VIEW/$HOME_SUBDIR" "$ROOT/home/$AGENT_USER"
else
    mount --bind "$VIEW" "$ROOT/home/$AGENT_USER"
fi

for spec in "${EXTRA_VIEWS[@]:-}"; do
    [[ -n "$spec" ]] || continue
    local_view="${spec#*=}"
    src="${local_view%%:*}"
    dst="${local_view##*:}"
    mkdir -p "$ROOT$dst"
    mount --bind "$src" "$ROOT$dst"
done

mkdir -p "$ROOT/run/ccc-agent"
printf '%s\n' "$SESSION_ID" > "$ROOT/run/ccc-agent/session"
chmod 0444 "$ROOT/run/ccc-agent/session"

# When --workdir is set, cd into it inside the chroot before exec.  env -i runs
# the command directly (no shell), so use a tiny sh trampoline: it cd's to $1,
# shifts, and execs the rest as the real agent command.
if [[ -n "$WORKDIR" ]]; then
    set -- /bin/sh -c 'cd "$1" || exit 1; shift; exec "$@"' sh "$WORKDIR" \
        "${COMMAND[@]}"
else
    set -- "${COMMAND[@]}"
fi

exec chroot "$ROOT" /usr/bin/setpriv \
    --reuid="$AGENT_UID" --regid="$AGENT_GID" --init-groups \
    /usr/bin/env -i \
    CCC_AGENT_SESSION="$SESSION_ID" \
    HOME="/home/$AGENT_USER" \
    USER="$AGENT_USER" \
    LOGNAME="$AGENT_USER" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    TERM="${TERM:-xterm}" \
    "$@"
