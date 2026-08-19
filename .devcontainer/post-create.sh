#!/bin/bash
# Post-create setup for devcontainer.
# Called via postCreateCommand in devcontainer.json.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Setting up devcontainer ==="

# Configure tmux to use xterm-256color so CLI tools (e.g. Claude Code) render correctly
cat > "$HOME/.tmux.conf" << 'TMUXEOF'
set -g default-terminal "xterm-256color"
set-environment -g LANG en_US.UTF-8
TMUXEOF

# Set up gh CLI credentials from host token (written by initializeCommand)
REPO_TOKEN_FILE="$REPO_ROOT/.devcontainer/.gh-token"
if [ -s "$REPO_TOKEN_FILE" ]; then
  echo "Setting up GitHub CLI credentials..."
  GH_TOKEN=$(cat "$REPO_TOKEN_FILE")
  echo "$GH_TOKEN" | gh auth login --with-token 2>/dev/null &&
    echo "gh auth configured." || echo "Warning: gh auth login failed."
  rm -f "$REPO_TOKEN_FILE"
else
  echo "Warning: No gh token found; gh CLI credentials not available."
fi

# Set up Claude Code credentials from host keychain (written by initializeCommand)
# Claude Code reads ~/.claude/.credentials.json on Linux (no keychain available)
CLAUDE_CRED_FILE="$REPO_ROOT/.devcontainer/.claude-credentials"
if [ -s "$CLAUDE_CRED_FILE" ]; then
  echo "Setting up Claude Code credentials..."
  mkdir -p "$HOME/.claude"
  if [ ! -f "$HOME/.claude/.credentials.json" ]; then
    cp "$CLAUDE_CRED_FILE" "$HOME/.claude/.credentials.json"
    chmod 600 "$HOME/.claude/.credentials.json"
    echo "Claude Code credentials configured."
  else
    echo "Claude Code credentials already present; leaving existing file in place."
  fi
  rm -f "$CLAUDE_CRED_FILE"
else
  echo "Warning: No Claude credentials found; Claude Code credentials not available."
fi

# Create $HOME/.claude/tmp so Claude Code's hook runner (which sets TMPDIR to
# that path) can call mktemp without failing.
mkdir -p "$HOME/.claude/tmp"

# When the host username differs from the container username (e.g., host user
# "nborgers" → container user "vscode"), hook paths stored in settings.json use
# the host home prefix (e.g., /home/nborgers/.claude/hooks/...) which doesn't
# exist inside the container. Symlink the host home's .claude into place so
# those absolute paths resolve.
if [ -n "${HOST_HOME:-}" ] && [ "$HOST_HOME" != "$HOME" ]; then
  sudo mkdir -p "$HOST_HOME"
  sudo ln -sfn "$HOME/.claude" "$HOST_HOME/.claude"
  echo "Symlinked $HOST_HOME/.claude → $HOME/.claude for hook path resolution"
fi

# Merge host Claude Code config into the container-local baseline. The image
# seeds hasCompletedOnboarding in $HOME/.claude.json; a non-empty host file can
# add user-specific settings without shadowing that baseline.
if [ -n "${CLAUDE_HOST_CONFIG_FILE:-}" ] \
   && [ -s "$CLAUDE_HOST_CONFIG_FILE" ]; then
  if python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
    "$CLAUDE_HOST_CONFIG_FILE" >/dev/null 2>&1; then
    echo "Merging Claude Code config from host..."
    EXISTING_CONFIG="$HOME/.claude.json"
    python3 -c '
import json
import sys

host_path, container_path = sys.argv[1], sys.argv[2]
with open(container_path, encoding="utf-8") as fh:
    container = json.load(fh)
with open(host_path, encoding="utf-8") as fh:
    host = json.load(fh)
host.update(container)
with open(container_path, "w", encoding="utf-8") as fh:
    json.dump(host, fh, indent=2)
    fh.write("\n")
' "$CLAUDE_HOST_CONFIG_FILE" "$EXISTING_CONFIG"
    chmod 600 "$EXISTING_CONFIG"
    echo "Claude Code config merged."
  else
    echo "Warning: Host Claude Code config is not valid JSON; keeping container default."
  fi
fi

# The developer's whole host ~/.claude is bind-mounted read-write onto
# $HOME/.claude, so CLAUDE.md, settings.json, hooks, agents, skills,
# output-styles, plugins, and per-project memory need no copying here.
# devcontainer.json also mounts the repo at its host path, which is what makes
# the memory directory match: Claude Code names per-project state after the
# working directory, so a container that saw /workspaces/<repo> would look up
# an empty project. On a CI runner with no host ~/.claude, Docker creates an
# empty directory and nothing below depends on its contents.

# Append a source line to the container user's .bashrc so the host ~/.bashrc
# (bind-mounted read-only at the same absolute path) is loaded on top of the
# container's baseline. Idempotent: re-running post-create won't duplicate
# the line. The host .bashrc's internal `source .../code/util/profile` line
# resolves via the matching bind mount. Uses `-s` (non-empty) rather than
# `-f` so an empty placeholder created by init-host-credentials.sh (for
# contributors who don't have the host file) is a silent no-op.
if [ -n "${HOST_BASHRC:-}" ] \
   && [ -s "$HOST_BASHRC" ] \
   && [ "$HOST_BASHRC" != "$HOME/.bashrc" ]; then
  bashrc_mark="# host-bashrc-passthrough: $HOST_BASHRC"
  if [ ! -f "$HOME/.bashrc" ] || ! grep -qF "$bashrc_mark" "$HOME/.bashrc"; then
    {
      printf '\n%s\n' "$bashrc_mark"
      printf '[ -r "%s" ] && . "%s"\n' "$HOST_BASHRC" "$HOST_BASHRC"
    } >> "$HOME/.bashrc"
    echo "Wired host .bashrc passthrough into $HOME/.bashrc"
  fi
fi

# Helper to read pinned versions from the Dockerfile (single source of truth)
DEVCONTAINER_DOCKERFILE="$SCRIPT_DIR/Dockerfile"
get_arg_version() {
    local arg_name=$1
    local value
    value=$(grep -oP "ARG ${arg_name}=\\K[^[:space:]]+" "$DEVCONTAINER_DOCKERFILE" | tail -n 1 || true)
    if [ -z "$value" ]; then
        echo "Failed to read ${arg_name} from ${DEVCONTAINER_DOCKERFILE}" >&2
        exit 1
    fi
    printf '%s\n' "$value"
}

export PATH="$HOME/.local/bin:$PATH"

# Codex CLI is baked into the devcontainer image.
# Verify the expected version is present.
CODEX_CLI_VERSION="$(get_arg_version CODEX_CLI_VERSION)"
CURRENT_CODEX_VERSION="$(codex --version 2>/dev/null | awk '{print $2}' || true)"
if [ "$CURRENT_CODEX_VERSION" != "$CODEX_CLI_VERSION" ]; then
    echo "Warning: Codex CLI version mismatch (have: ${CURRENT_CODEX_VERSION}, want: ${CODEX_CLI_VERSION})"
    echo "Rebuilding the devcontainer image should fix this."
fi

bash "$SCRIPT_DIR/configure-codex.sh"

# Provision the project's Python environment. This is what makes `pytest
# tests/unit/` work on a fresh clone with no further setup — and it is not
# optional: postCreateCommand installs .githooks/, whose pre-commit hook runs
# ruff and the unit suite. Without these dependencies the first commit in a new
# container fails with "required command 'pytest' is not installed".
#
# uv owns the interpreter (pyproject.toml requires >=3.12; the base image's
# system Python is not pinned to that). `uv pip install` rather than `uv sync`
# so no uv.lock is written into the working tree — this avoids committing a
# lockfile that would need to be kept in sync separately from pyproject.toml.
echo "Provisioning Python environment (.venv)..."
cd "$REPO_ROOT"
uv venv --python 3.12 --allow-existing
uv pip install --quiet --python "$REPO_ROOT/.venv/bin/python" -e ".[dev]"
echo "Python environment ready: $("$REPO_ROOT/.venv/bin/python" --version)"

echo "=== Devcontainer setup complete ==="
