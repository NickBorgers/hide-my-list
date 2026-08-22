#!/bin/bash
# Runs on the HOST before the devcontainer starts.
# Extracts credentials from host keychain/CLI and writes them to files
# that postCreateCommand will consume inside the container.
# All token files are gitignored and deleted after use.

set -e
cd "$(dirname "$0")"

# Ensure every devcontainer.json bind-mount source exists before Docker
# tries to resolve it. Missing bind-mount sources are dangerous: Docker
# silently materializes them as root-owned directories on the host (on
# Linux) or errors out at container start (on macOS / Docker Desktop).
# Creating user-owned placeholders here is a cheap no-op when the files
# already exist, and guarantees the devcontainer can spin up on any
# contributor's machine. post-create.sh uses `-s` (non-empty) guards
# before wiring anything in, so empty placeholders are installed but
# never activated.

[ -e "$HOME/.claude"           ] || mkdir -p "$HOME/.claude"
[ -e "$HOME/.claude/projects"  ] || mkdir -p "$HOME/.claude/projects"
[ -e "$HOME/.gitconfig"        ] || touch    "$HOME/.gitconfig"
[ -e "$HOME/.claude.json"      ] || touch    "$HOME/.claude.json"
[ -e "$HOME/.bashrc"           ] || touch    "$HOME/.bashrc"
[ -d "$HOME/code/util"         ] || mkdir -p "$HOME/code/util"
[ -e "$HOME/code/util/profile" ] || touch    "$HOME/code/util/profile"

# GitHub CLI token
gh auth token > .gh-token 2>/dev/null || true

# Claude Code OAuth credentials
# Try file-based extraction first (Linux), fall back to macOS keychain
CLAUDE_CREDS="$HOME/.claude/.credentials.json"
if [ -f "$CLAUDE_CREDS" ]; then
    cp "$CLAUDE_CREDS" .claude-credentials
    chmod 600 .claude-credentials
elif command -v security &>/dev/null; then
    security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
        > .claude-credentials 2>/dev/null || true
fi
