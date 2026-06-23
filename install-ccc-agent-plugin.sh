#!/usr/bin/env bash
# install-ccc-agent-plugin.sh — Install the CCC agent containment plugin.
#
# Installs the CCC agent containment stack at user level (no root required).
# On CCC images this script should be placed in /opt/ccc-agent-plugin/ and
# sourced from the container profile, or run once during image setup.
#
# What gets installed:
#   ~/.local/share/ccc-agent/   — supervisor Python package + scripts
#   ~/.local/bin/               — shims: ccc-agent-run, ccc-agent-softsandbox,
#                                         ccc-agentctl
#   ~/.config/ccc-agent/        — user config (branchfs_bin, state_dir, roots)
#
# Claude Code hooks are registered in ~/.claude/settings.json (Stop hook).
# Codex hooks are registered in ~/.config/codex/config.json if codex found.
#
# Usage:
#   bash install-ccc-agent-plugin.sh [--prefix PREFIX] [--branchfs BIN] \
#                                    [--workspace DIR] [--dry-run] [--uninstall]
#
# Options:
#   --prefix DIR     Install root (default: ~/.local/share/ccc-agent)
#   --bin-dir DIR    Where to install CLI shims (default: ~/.local/bin)
#   --branchfs BIN   Path to branchfs binary (auto-detected if omitted)
#   --workspace DIR  Default workspace to protect (default: ~/Projects)
#   --state-dir DIR  Where to keep sessions/branches (default: ~/.ccc-agent)
#   --hide PATH      Additional secret path to hide (repeatable)
#   --dry-run        Print plan without changing anything
#   --uninstall      Remove previously installed files
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PREFIX="${HOME}/.local/share/ccc-agent"
BIN_DIR="${HOME}/.local/bin"
BRANCHFS_BIN=""
DEFAULT_WORKSPACE="${HOME}/Projects"
STATE_DIR="${HOME}/.ccc-agent"
DRY_RUN=0
UNINSTALL=0
NO_HOOKS=0
declare -a EXTRA_HIDE=()
declare -a DEFAULT_HIDE=(".ssh" ".gnupg" ".netrc" ".aws" ".kube" ".env" ".env.*")

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)    PREFIX="$2"; shift 2 ;;
        --bin-dir)   BIN_DIR="$2"; shift 2 ;;
        --branchfs)  BRANCHFS_BIN="$2"; shift 2 ;;
        --workspace) DEFAULT_WORKSPACE="$2"; shift 2 ;;
        --state-dir) STATE_DIR="$2"; shift 2 ;;
        --hide)      EXTRA_HIDE+=("$2"); shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --no-hooks)  NO_HOOKS=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   grep '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Find branchfs
# ---------------------------------------------------------------------------
if [[ -z "$BRANCHFS_BIN" ]]; then
    for c in \
        "${SCRIPT_DIR}/../worktrees/branchfs-agent-containment/target/debug/branchfs" \
        "${SCRIPT_DIR}/../worktrees/branchfs-agent-containment/target/release/branchfs" \
        "${SCRIPT_DIR}/../branchfs/target/debug/branchfs" \
        "/opt/ccc-agent/bin/branchfs" \
        "$(command -v branchfs 2>/dev/null || true)"
    do
        [[ -x "$c" ]] && { BRANCHFS_BIN="$(realpath "$c")"; break; }
    done
fi

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if [[ "$UNINSTALL" -eq 1 ]]; then
    echo "Uninstalling CCC agent plugin..."
    for f in ccc-agent-run ccc-agent-launch ccc-agent-softsandbox ccc-agentctl; do
        rm -f "$BIN_DIR/$f" && echo "  removed $BIN_DIR/$f" || true
    done
    rm -rf "$PREFIX" && echo "  removed $PREFIX" || true
    echo "Done. Config and sessions preserved at: $STATE_DIR"
    echo "Remove manually if desired: rm -rf $STATE_DIR ~/.config/ccc-agent"
    exit 0
fi

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
ALL_HIDE=("${DEFAULT_HIDE[@]}" "${EXTRA_HIDE[@]}")
HIDE_JSON="$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" -- "${ALL_HIDE[@]}")"

echo ""
echo "CCC Agent Containment Plugin Installer"
echo "────────────────────────────────────────────────────────────────"
echo "  Install prefix: $PREFIX"
echo "  Bin dir:        $BIN_DIR"
echo "  State dir:      $STATE_DIR"
echo "  Workspace:      $DEFAULT_WORKSPACE"
echo "  BranchFS:       ${BRANCHFS_BIN:-NOT FOUND}"
echo "  Hidden paths:   ${ALL_HIDE[*]}"
[[ "$DRY_RUN" -eq 1 ]] && echo "  *** DRY RUN ***"
echo "────────────────────────────────────────────────────────────────"
echo ""

do_it() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[DRY RUN] $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Install supervisor package
# ---------------------------------------------------------------------------
echo "Step 1: Install supervisor Python package..."
do_it mkdir -p "$PREFIX/lib"
do_it cp -r "$SCRIPT_DIR/ccc_agent" "$PREFIX/lib/"
echo "  Installed ccc_agent package to $PREFIX/lib/"

# ---------------------------------------------------------------------------
# Install scripts and hooks
# ---------------------------------------------------------------------------
echo ""
echo "Step 2: Install scripts..."
do_it mkdir -p "$PREFIX/bin" "$PREFIX/hooks" "$PREFIX/shims" "$PREFIX/config"

for script in ccc-agent-run ccc-agent-launch ccc-agentctl; do
    do_it cp "$SCRIPT_DIR/bin/$script" "$PREFIX/bin/$script"
    do_it chmod +x "$PREFIX/bin/$script"
done

# softsandbox
do_it cp "$SCRIPT_DIR/bin/ccc-agent-softsandbox" "$PREFIX/bin/ccc-agent-softsandbox"
do_it chmod +x "$PREFIX/bin/ccc-agent-softsandbox"

# hooks
for hook in hooks/*.sh; do
    [[ -f "$hook" ]] && do_it cp "$SCRIPT_DIR/$hook" "$PREFIX/hooks/" || true
done

# shims
do_it cp "$SCRIPT_DIR/shims/ccc-agent-shim.sh" "$PREFIX/shims/"
do_it chmod +x "$PREFIX/shims/ccc-agent-shim.sh"

echo "  Scripts installed to $PREFIX/"

# ---------------------------------------------------------------------------
# Create wrapper scripts in $BIN_DIR that inject PYTHONPATH
# ---------------------------------------------------------------------------
echo ""
echo "Step 3: Create PATH wrappers in $BIN_DIR..."
do_it mkdir -p "$BIN_DIR"

for tool in ccc-agent-run ccc-agent-launch ccc-agentctl ccc-agent-softsandbox; do
    WRAPPER="$BIN_DIR/$tool"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        cat > "$WRAPPER" <<WRAPPER_EOF
#!/usr/bin/env bash
# Auto-generated by install-ccc-agent-plugin.sh
export PYTHONPATH="$PREFIX/lib:\${PYTHONPATH:-}"
export CCC_AGENT_PREFIX="$PREFIX"
export CCC_AGENT_STATE_DIR="${STATE_DIR}"
export CCC_AGENT_SOFTSANDBOX_BIN="${BRANCHFS_BIN:-}"
exec "$PREFIX/bin/$tool" "\$@"
WRAPPER_EOF
        chmod +x "$WRAPPER"
    else
        echo "[DRY RUN] would write $WRAPPER"
    fi
done
echo "  Wrappers installed in $BIN_DIR"

# ---------------------------------------------------------------------------
# Write user config
# ---------------------------------------------------------------------------
echo ""
echo "Step 4: Write user config..."
CONFIG_DIR="${HOME}/.config/ccc-agent"
CONFIG_FILE="$CONFIG_DIR/config.json"
do_it mkdir -p "$CONFIG_DIR"

if [[ "$DRY_RUN" -eq 0 ]]; then
    # Build home subdir (e.g. user name from /home/$USER -> subdir of /storage/user)
    HOME_SUBDIR="$(id -un)"

    python3 -c "
import json, os
config = {
    '_comment': 'CCC agent containment config. Edit as needed.',
    'state_dir': '$STATE_DIR',
    'backend': 'branchfs',
    'branchfs_bin': '${BRANCHFS_BIN:-}',
    'user': '$(id -un)',
    'home_subdir': '${HOME_SUBDIR}',
    'roots': [
        {
            'name': 'storage_user',
            'base': os.path.expanduser('~'),
            'store': '$STATE_DIR/stores/home',
            'visible': os.path.expanduser('~'),
            'home_subdir': '',
            'hide_paths': ${HIDE_JSON},
        }
    ],
    'policy': {
        'mode': 'workspace-auto',
        'default_workspace': '$DEFAULT_WORKSPACE',
    },
    'hide_patterns': ['.env', '.env.*', '*.pem', 'id_rsa*', 'id_ed25519*'],
}
with open('$CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2)
print('  Config written to $CONFIG_FILE')
"
else
    echo "[DRY RUN] would write $CONFIG_FILE"
fi

# ---------------------------------------------------------------------------
# Register Claude Code Stop hook
# ---------------------------------------------------------------------------
echo ""
echo "Step 5: Register Claude Code Stop hook..."
if [[ "$NO_HOOKS" -eq 1 ]]; then
    echo "  Skipped (--no-hooks)"
else
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
HOOK_CMD="$PREFIX/hooks/claude-stop-hook.sh"

if [[ "$DRY_RUN" -eq 0 ]]; then
    python3 - <<PYEOF
import json, os, sys

settings_path = '$CLAUDE_SETTINGS'
hook_cmd = '$HOOK_CMD'

# Load existing settings
if os.path.exists(settings_path):
    with open(settings_path) as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            settings = {}
else:
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}

# Build hook entry
hook_entry = {
    "type": "command",
    "command": hook_cmd
}
hook_matcher = {"hooks": [hook_entry]}

# Find or create Stop hooks
hooks = settings.setdefault("hooks", {})
stop_hooks = hooks.setdefault("Stop", [])

# Check if already registered (avoid duplicate)
for matcher in stop_hooks:
    for h in matcher.get("hooks", []):
        if h.get("command") == hook_cmd:
            print("  Claude Stop hook already registered.")
            sys.exit(0)

stop_hooks.append(hook_matcher)
settings["hooks"]["Stop"] = stop_hooks

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
print(f"  Claude Stop hook registered in {settings_path}")
PYEOF
else
    echo "[DRY RUN] would add Stop hook to $CLAUDE_SETTINGS"
fi

# ---------------------------------------------------------------------------
# Register Codex stop hook (if codex found)
# ---------------------------------------------------------------------------
echo ""
echo "Step 6: Register Codex hook (if codex present)..."
if command -v codex >/dev/null 2>&1; then
    CODEX_CONFIG="${HOME}/.config/codex/config.json"
    CODEX_HOOK_CMD="$PREFIX/hooks/codex-stop-hook.sh"

    if [[ "$DRY_RUN" -eq 0 ]]; then
        python3 - <<CODEX_PYEOF
import json, os

config_path = '$CODEX_CONFIG'
hook_cmd = '$CODEX_HOOK_CMD'

os.makedirs(os.path.dirname(config_path), exist_ok=True)
if os.path.exists(config_path):
    with open(config_path) as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError:
            config = {}
else:
    config = {}

# Codex hook format
hooks = config.setdefault("hooks", {})
stop_hooks = hooks.setdefault("stop", [])

# Check if already registered
for h in stop_hooks:
    if h.get("command") == hook_cmd:
        print("  Codex stop hook already registered.")
        exit(0)

stop_hooks.append({"command": hook_cmd})
config["hooks"]["stop"] = stop_hooks

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)
print(f"  Codex stop hook registered in {config_path}")
CODEX_PYEOF
    else
        echo "[DRY RUN] would add stop hook to $CODEX_CONFIG"
    fi
else
    echo "  codex not found in PATH — skipping codex hook registration"
fi
fi  # end NO_HOOKS guard

# ---------------------------------------------------------------------------
# Write activation snippet for shell profile
# ---------------------------------------------------------------------------
echo ""
echo "Step 7: Shell profile snippet..."
PROFILE_SNIPPET="${HOME}/.config/ccc-agent/profile.sh"
if [[ "$DRY_RUN" -eq 0 ]]; then
    cat > "$PROFILE_SNIPPET" <<PROFILE_EOF
# CCC Agent Containment — auto-generated by install-ccc-agent-plugin.sh
# Source this from ~/.bashrc or ~/.profile:
#   source ~/.config/ccc-agent/profile.sh

export PATH="$BIN_DIR:\$PATH"
export PYTHONPATH="$PREFIX/lib:\${PYTHONPATH:-}"
export CCC_AGENT_PREFIX="$PREFIX"
export CCC_AGENT_STATE_DIR="$STATE_DIR"
export CCC_AGENT_SOFTSANDBOX_BIN="${BRANCHFS_BIN:-}"
PROFILE_EOF
    echo "  Profile snippet written to: $PROFILE_SNIPPET"
    echo "  Add to your shell profile: source $PROFILE_SNIPPET"
else
    echo "[DRY RUN] would write $PROFILE_SNIPPET"
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "Installation complete!"
echo ""
echo "Quick test:"
echo "  source $PROFILE_SNIPPET"
echo "  ccc-agent-run --help"
echo "  ccc-agent-softsandbox --workspace /tmp/test-folder -- bash -c 'echo hello > hello.txt'"
echo ""
if [[ -z "${BRANCHFS_BIN:-}" ]]; then
    echo "⚠  branchfs binary not found. Build it with:"
    echo "   cd ${SCRIPT_DIR}/../worktrees/branchfs-agent-containment"
    echo "   conda run -n branchfs-dev cargo build"
    echo "   Then re-run: $0 --branchfs <path>"
fi
echo ""
echo "Docs: ${SCRIPT_DIR}/docs/architecture.md"
echo "────────────────────────────────────────────────────────────────"
