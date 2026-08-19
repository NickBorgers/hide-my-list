"""Structural lint: the devcontainer gives the container the host's agent config.

A developer's Claude Code setup lives in two places: the host `~/.claude`
directory (CLAUDE.md, settings.json, hooks, agents, skills, output-styles,
plugins) and a per-project state directory named after the working directory
(`~/.claude/projects/<path-slug>/`, which holds memory, sessions, and plans).

The devcontainer reproduces both by mounting, not by copying:

- The whole host `~/.claude` is bind-mounted read-write onto the container
  user's home, so every customization crosses and anything written inside the
  container persists back to the host.
- The repo is mounted at its *host* path, so the project slug matches on both
  sides and the container resolves the same project state.

Bug class prevention: the earlier setup mounted `~/.claude` read-only at a
staging path and symlinked three items out of it. Agents, skills,
output-styles, and plugins never crossed — so plugin-provided hooks named in
settings.json failed inside the container. Project memory never crossed either,
because the container saw the repo at /workspaces/<repo> and looked up a
project slug the host had never written.

CI's `Devcontainer Build Check` runs `devcontainer build`, which builds the
image but never creates a container — so these assertions are the only
automated coverage of the mount wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEVCONTAINER_DIR = _REPO_ROOT / ".devcontainer"
_DEVCONTAINER_JSON = _DEVCONTAINER_DIR / "devcontainer.json"
_POST_CREATE = _DEVCONTAINER_DIR / "post-create.sh"
_SETUP_USER = _DEVCONTAINER_DIR / "setup-user.sh"

_LOCAL_WORKSPACE = "${localWorkspaceFolder}"


def _config() -> dict:
    return json.loads(_DEVCONTAINER_JSON.read_text(encoding="utf-8"))


def _parse_mount(spec: str) -> dict[str, str]:
    """Parse a `source=...,target=...,type=bind,...` mount string."""
    parsed: dict[str, str] = {}
    for field in spec.split(","):
        key, _, value = field.partition("=")
        parsed[key] = value if value else "true"
    return parsed


def _claude_mount() -> dict[str, str]:
    for spec in _config()["mounts"]:
        mount = _parse_mount(spec)
        if mount.get("source", "").endswith("/.claude"):
            return mount
    raise AssertionError(
        "Expected a bind mount of the host ~/.claude in devcontainer.json. "
        "Without it the container runs with none of the developer's Claude "
        "Code customizations."
    )


def test_workspace_is_mounted_at_its_host_path() -> None:
    """Claude Code keys project state by working directory path."""
    config = _config()
    assert config.get("workspaceFolder") == _LOCAL_WORKSPACE, (
        "Expected workspaceFolder=${localWorkspaceFolder}. Under the default "
        "/workspaces/<repo> the container derives a different project slug "
        "and finds no memory, sessions, or plans."
    )
    mount = _parse_mount(config["workspaceMount"])
    assert mount.get("source") == _LOCAL_WORKSPACE
    assert mount.get("target") == _LOCAL_WORKSPACE, (
        "Expected workspaceMount to place the repo at its host path so the "
        "host and container agree on the project identity."
    )


def test_host_claude_directory_is_mounted_whole_and_writable() -> None:
    """Copying a subset is what dropped skills, plugins, and memory before."""
    mount = _claude_mount()
    assert mount["source"] == "${localEnv:HOME}/.claude"
    assert "readonly" not in mount, (
        "Expected the ~/.claude mount to be writable. Claude Code writes "
        "memory, sessions, and plugin cache there; a read-only mount makes "
        "every one of those writes fail inside the container."
    )


def test_claude_mount_targets_the_container_user_home() -> None:
    """Claude Code reads $HOME/.claude, so the mount has to land there."""
    config = _config()
    remote_user = config.get("remoteUser", "vscode")
    assert _claude_mount()["target"] == f"/home/{remote_user}/.claude", (
        "Expected the host ~/.claude to be mounted onto the container user's "
        f"home (/home/{remote_user}/.claude). Anywhere else and Claude Code "
        "inside the container never reads it."
    )


def test_post_create_does_not_copy_claude_config_piecemeal() -> None:
    """Regression guard: the copy path is what kept losing new config kinds."""
    text = _POST_CREATE.read_text(encoding="utf-8")
    assert "CLAUDE_HOST_CONFIG_DIR" not in text, (
        "post-create.sh should not stage host Claude Code config item by "
        "item. Every new kind of config (skills, plugins, output-styles) had "
        "to be added to that list by hand, and memory could not be linked at "
        "all. devcontainer.json mounts the whole directory instead."
    )


def test_container_user_uid_comes_from_the_workspace_path() -> None:
    """/workspaces is empty now that the repo is mounted at its host path."""
    config = _config()
    assert "${containerWorkspaceFolder}" in config["onCreateCommand"], (
        "Expected onCreateCommand to pass ${containerWorkspaceFolder} to "
        "setup-user.sh. It reads the host UID from workspace file ownership, "
        "and the repo no longer lives under /workspaces."
    )
    assert "WORKSPACE_FOLDER=" in _SETUP_USER.read_text(encoding="utf-8"), (
        "Expected setup-user.sh to accept the workspace path argument that "
        "onCreateCommand passes."
    )


def test_post_start_chowns_the_actual_workspace() -> None:
    """A hardcoded /workspaces chown silently stopped fixing ownership."""
    assert "${containerWorkspaceFolder}" in _config()["postStartCommand"], (
        "Expected postStartCommand to chown ${containerWorkspaceFolder}. The "
        "repo is mounted at its host path, so chowning /workspaces is a no-op."
    )
