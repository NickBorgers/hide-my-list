"""The devcontainer carries host Claude Code config in without granting writes.

A developer's Claude Code setup lives in two places: the host `~/.claude`
directory (CLAUDE.md, settings.json, hooks, agents, skills, output-styles,
plugins) and per-project state under `~/.claude/projects/<path-slug>/` (memory,
sessions, plans). The container needs both, and the repo has to be mounted at
its host path for the second one to resolve — Claude Code names project state
after the working directory.

The two halves get different mounts, and the split is the security boundary:

- The whole host `~/.claude` is mounted **read-only**. Everything that can
  cause execution lives there. Repo-controlled code runs in this container, so
  it must not be able to write a hook that the host agent later runs.
- `~/.claude/projects` alone is mounted **writable**, because Claude Code
  writes memory and session files as it works. Those are data files.

Bug class prevention: the original setup mounted `~/.claude` read-only at a
staging path and symlinked three items out of it, so agents, skills,
output-styles, and plugins never crossed and plugin-provided hooks named in
settings.json failed inside the container. Project state never crossed either,
because the container saw the repo at /workspaces/<repo> and looked up a
project name the host had never written. The first fix for that made the whole
directory writable, which handed repo-controlled container code a way to
persist hooks onto the host.

CI's `Devcontainer Build Check` runs `devcontainer build`, which builds the
image but never creates a container — so these assertions are the only
automated coverage of the wiring.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEVCONTAINER_DIR = _REPO_ROOT / ".devcontainer"
_DEVCONTAINER_JSON = _DEVCONTAINER_DIR / "devcontainer.json"
_SYNC_SCRIPT = _DEVCONTAINER_DIR / "sync-claude-config.sh"
_POST_CREATE = _DEVCONTAINER_DIR / "post-create.sh"
_SETUP_USER = _DEVCONTAINER_DIR / "setup-user.sh"
_INIT_HOST = _DEVCONTAINER_DIR / "init-host-credentials.sh"

_LOCAL_WORKSPACE = "${localWorkspaceFolder}"
_CONTAINER_WORKSPACE = "/home/dev/code/hide-my-list"
_PROJECT_SLUG = "-home-dev-code-hide-my-list"


def _config() -> dict:
    return json.loads(_DEVCONTAINER_JSON.read_text(encoding="utf-8"))


def _parse_mount(spec: str) -> dict[str, str]:
    """Parse a `source=...,target=...,type=bind,...` mount string."""
    parsed: dict[str, str] = {}
    for field in spec.split(","):
        key, _, value = field.partition("=")
        parsed[key] = value if value else "true"
    return parsed


def _mount_with_source(suffix: str) -> dict[str, str]:
    for spec in _config()["mounts"]:
        mount = _parse_mount(spec)
        if mount.get("source", "").endswith(suffix):
            return mount
    raise AssertionError(
        f"Expected a bind mount whose source ends with {suffix!r} in "
        "devcontainer.json."
    )


# --- Mount wiring -----------------------------------------------------------


def test_workspace_is_mounted_at_its_host_path() -> None:
    """Claude Code keys project state by working directory path."""
    config = _config()
    assert config.get("workspaceFolder") == _LOCAL_WORKSPACE, (
        "Expected workspaceFolder=${localWorkspaceFolder}. Under the default "
        "/workspaces/<repo> the container derives a different project name "
        "and finds no memory, sessions, or plans."
    )
    mount = _parse_mount(config["workspaceMount"])
    assert mount.get("source") == _LOCAL_WORKSPACE
    assert mount.get("target") == _LOCAL_WORKSPACE, (
        "Expected workspaceMount to place the repo at its host path so the "
        "host and container agree on the project identity."
    )


def test_host_claude_directory_is_mounted_read_only() -> None:
    """Repo-controlled code runs here; it must not reach host hooks."""
    mount = _mount_with_source("/.claude")
    assert mount["source"] == "${localEnv:HOME}/.claude"
    assert "readonly" in mount, (
        "Expected the host ~/.claude mount to stay read-only. It carries "
        "settings.json, hooks/, and plugins/ — a writable mount lets code in "
        "this container persist a hook that the host agent later runs."
    )


def test_project_state_is_mounted_writable_on_its_own() -> None:
    """Memory and session writes need somewhere real to land."""
    mount = _mount_with_source("/.claude/projects")
    assert "readonly" not in mount, (
        "Expected ~/.claude/projects to be writable. Claude Code writes "
        "memory, sessions, and plans there while it works."
    )
    assert _config()["remoteEnv"]["CLAUDE_HOST_PROJECTS_DIR"] == mount["target"], (
        "Expected CLAUDE_HOST_PROJECTS_DIR to name the writable mount's "
        "target; sync-claude-config.sh reads it from the environment."
    )


def test_container_user_uid_comes_from_the_workspace_path() -> None:
    """/workspaces is empty now that the repo is mounted at its host path."""
    assert "${containerWorkspaceFolder}" in _config()["onCreateCommand"], (
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


def test_post_create_wires_host_bashrc_passthrough() -> None:
    """HOST_BASHRC mount + env var must be consumed; drop = silent shell regression."""
    text = _POST_CREATE.read_text(encoding="utf-8")
    assert "HOST_BASHRC" in text, (
        "post-create.sh must source HOST_BASHRC into the container user's "
        ".bashrc. devcontainer.json bind-mounts the host .bashrc and exports "
        "HOST_BASHRC; if post-create.sh doesn't consume it the host shell "
        "profile stops loading inside the container."
    )

def test_initialize_command_creates_the_projects_mount_source() -> None:
    """Docker materializes a missing bind source as a root-owned directory."""
    assert '"$HOME/.claude/projects"' in _INIT_HOST.read_text(encoding="utf-8"), (
        "Expected init-host-credentials.sh to create ~/.claude/projects on "
        "the host before Docker resolves the bind mount."
    )


def test_post_create_runs_the_sync_script() -> None:
    """The script only helps if postCreateCommand's chain calls it."""
    text = _POST_CREATE.read_text(encoding="utf-8")
    assert "sync-claude-config.sh" in text
    assert "CONTAINER_REPO_ROOT=" in text, (
        "Expected post-create.sh to pass CONTAINER_REPO_ROOT so the project "
        "name is derived from the workspace path rather than the caller's "
        "working directory."
    )


# --- Placement behavior -----------------------------------------------------


def _build_mounts(root: Path) -> tuple[Path, Path]:
    """Create fixtures shaped like the two bind mounts."""
    host = root / "host-claude"
    (host / "hooks").mkdir(parents=True)
    (host / "agents").mkdir()
    (host / "skills").mkdir()
    (host / "output-styles").mkdir()
    (host / "plugins" / "cache").mkdir(parents=True)
    (host / "CLAUDE.md").write_text("host instructions\n", encoding="utf-8")
    (host / "settings.json").write_text("{}\n", encoding="utf-8")
    (host / "hooks" / "on-start.sh").write_text("echo hi\n", encoding="utf-8")
    (host / "agents" / "auditor.md").write_text("agent\n", encoding="utf-8")
    (host / "skills" / "helper.md").write_text("skill\n", encoding="utf-8")
    (host / "output-styles" / "PlainTech.md").write_text("style\n", encoding="utf-8")
    (host / "plugins" / "config.json").write_text("{}\n", encoding="utf-8")

    projects = root / "host-projects"
    memory = projects / _PROJECT_SLUG / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [Logs](logs.md)\n", encoding="utf-8")
    return host, projects


def _run_sync(
    tmp_path: Path,
    *,
    home: Path,
    host_dir: Path | None,
    projects_dir: Path | None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CONTAINER_REPO_ROOT": _CONTAINER_WORKSPACE,
    }
    if host_dir is not None:
        env["CLAUDE_HOST_CONFIG_DIR"] = str(host_dir)
    if projects_dir is not None:
        env["CLAUDE_HOST_PROJECTS_DIR"] = str(projects_dir)

    return subprocess.run(
        ["bash", str(_SYNC_SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_declarative_config_is_linked(tmp_path: Path) -> None:
    """Host edits must show up in the container, so these are symlinks."""
    host, projects = _build_mounts(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    _run_sync(tmp_path, home=home, host_dir=host, projects_dir=projects)

    for item in ("CLAUDE.md", "settings.json", "hooks", "agents", "skills",
                 "output-styles"):
        link = home / ".claude" / item
        assert link.is_symlink(), (
            f"Expected $HOME/.claude/{item} to be a symlink into the host "
            "config mount. Without it the container runs without the "
            "developer's own Claude Code customizations."
        )
        assert link.resolve() == (host / item).resolve()


def test_plugins_are_copied_not_linked(tmp_path: Path) -> None:
    """Plugins carry runtime state and the config mount is read-only."""
    host, projects = _build_mounts(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    _run_sync(tmp_path, home=home, host_dir=host, projects_dir=projects)

    plugins = home / ".claude" / "plugins"
    assert plugins.is_dir() and not plugins.is_symlink(), (
        "Expected $HOME/.claude/plugins to be a writable copy. A symlink "
        "points into the read-only mount, so plugin cache writes fail."
    )
    (plugins / "cache" / "probe").write_text("ok\n", encoding="utf-8")


def test_project_state_links_into_the_writable_mount(tmp_path: Path) -> None:
    """Memory written in the container has to reach the host directory."""
    host, projects = _build_mounts(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    _run_sync(tmp_path, home=home, host_dir=host, projects_dir=projects)

    container_memory = home / ".claude" / "projects" / _PROJECT_SLUG / "memory"
    assert (container_memory / "MEMORY.md").exists(), (
        "Expected the container's project directory to resolve to the host's "
        "state for the same project. A different name leaves the container "
        "with no memory at all."
    )

    (container_memory / "new-fact.md").write_text("learned inside\n", encoding="utf-8")
    assert (projects / _PROJECT_SLUG / "memory" / "new-fact.md").exists(), (
        "Expected memory written in the container to land in the host mount."
    )


def test_missing_host_config_is_a_no_op(tmp_path: Path) -> None:
    """Contributors without a host ~/.claude, and CI runners, still build."""
    home = tmp_path / "home"
    home.mkdir()

    result = _run_sync(tmp_path, home=home, host_dir=None, projects_dir=None)

    assert result.returncode == 0
    assert not (home / ".claude" / "CLAUDE.md").exists()


def test_missing_projects_mount_skips_state_only(tmp_path: Path) -> None:
    """An absent writable mount must not cost the rest of the config."""
    host, _ = _build_mounts(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    _run_sync(tmp_path, home=home, host_dir=host, projects_dir=None)

    assert (home / ".claude" / "CLAUDE.md").is_symlink()
    assert not (home / ".claude" / "projects").exists()


def test_existing_directories_are_replaced_by_links(tmp_path: Path) -> None:
    """`ln -sfn` into a real directory links *inside* it and still exits 0."""
    host, projects = _build_mounts(tmp_path)
    home = tmp_path / "home"
    stale = home / ".claude" / "hooks"
    stale.mkdir(parents=True)
    (stale / "image-default.sh").write_text("baked into the image\n", encoding="utf-8")

    _run_sync(tmp_path, home=home, host_dir=host, projects_dir=projects)

    link = home / ".claude" / "hooks"
    assert link.is_symlink(), (
        "Expected an existing $HOME/.claude/hooks directory to be replaced by "
        "the link. Left in place, the container keeps running the image's own "
        "hooks while the script reports success."
    )
    assert link.resolve() == (host / "hooks").resolve()
    assert not (link / "hooks").exists(), "Link was nested inside the directory."


def test_existing_project_state_is_carried_into_the_host_mount(tmp_path: Path) -> None:
    """State written before the link existed must not be dropped."""
    host, projects = _build_mounts(tmp_path)
    home = tmp_path / "home"
    stale = home / ".claude" / "projects" / _PROJECT_SLUG
    stale.mkdir(parents=True)
    (stale / "session.jsonl").write_text("written before linking\n", encoding="utf-8")

    _run_sync(tmp_path, home=home, host_dir=host, projects_dir=projects)

    link = home / ".claude" / "projects" / _PROJECT_SLUG
    assert link.is_symlink()
    assert (projects / _PROJECT_SLUG / "session.jsonl").read_text(
        encoding="utf-8"
    ) == "written before linking\n", (
        "Expected container-local project state to be merged into the host "
        "mount before the directory was replaced by the link."
    )
    assert (link / "memory" / "MEMORY.md").exists(), (
        "Expected the host's own project state to still be reachable."
    )


def test_host_mount_targets_avoid_tmp() -> None:
    """The docker-in-docker feature mounts a tmpfs over /tmp at container start.

    `/usr/local/share/docker-init.sh` runs `mount -t tmpfs none /tmp`, which
    hides anything Docker bind-mounted underneath it. Staging the host config
    at /tmp/devcontainer-host meant the mounts existed but were invisible to
    every process in the container, so post-create linked nothing and the
    developer's Claude Code setup silently never arrived.
    """
    config = _config()
    offenders = [
        spec for spec in config["mounts"]
        if _parse_mount(spec).get("target", "").startswith("/tmp/")
    ]
    assert not offenders, (
        "Bind-mount targets must not live under /tmp: the docker-in-docker "
        f"feature mounts a tmpfs there and hides them. Offending: {offenders}"
    )

    staged_env = {
        key: value for key, value in config["remoteEnv"].items()
        if key.startswith("CLAUDE_HOST_") and value.startswith("/tmp/")
    }
    assert not staged_env, (
        "CLAUDE_HOST_* paths must point at the real mount targets, which "
        f"cannot be under /tmp. Offending: {staged_env}"
    )
