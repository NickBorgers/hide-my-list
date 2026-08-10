"""Structural lint: devcontainer provisions a runnable Python environment.

The project commitment is that cloning the repo and opening the devcontainer is
enough to run the test suite — no manual setup step. `postCreateCommand` also
installs `.githooks/`, whose pre-commit hook runs ruff and `pytest tests/unit/`,
so a container without the project's dependencies produces a hook that fails on
the very first commit.

Bug class prevention: the Dockerfile installed `python3-pip` but nothing ever
installed the project itself, so a fresh container had no pytest / ruff / mypy.
`pytest tests/unit/` failed on import and every commit aborted with "required
command 'pytest' is not installed".

CI's `Devcontainer Build Check` runs `devcontainer build`, which builds the
image but never executes `postCreateCommand` — so these assertions are the only
automated coverage of the provisioning step.
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEVCONTAINER_DIR = _REPO_ROOT / ".devcontainer"
_POST_CREATE = _DEVCONTAINER_DIR / "post-create.sh"
_DEVCONTAINER_JSON = _DEVCONTAINER_DIR / "devcontainer.json"
_DOCKERFILE = _DEVCONTAINER_DIR / "Dockerfile"


def test_dockerfile_installs_uv() -> None:
    """uv must be in the image; post-create depends on it to build the venv."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG UV_VERSION=" in text, (
        "Expected a pinned ARG UV_VERSION in the devcontainer Dockerfile. "
        "post-create.sh calls `uv venv` to provision the Python environment."
    )
    assert "/usr/local/bin/uv" in text, (
        "Expected uv to be installed onto PATH in the devcontainer Dockerfile."
    )


def test_post_create_installs_project_dependencies() -> None:
    """post-create.sh must install the project with its dev extras."""
    text = _POST_CREATE.read_text(encoding="utf-8")
    assert "uv venv" in text, (
        "Expected post-create.sh to create a virtualenv via `uv venv`. Without "
        "it a fresh container has no pytest / ruff / mypy and both the test "
        "suite and the pre-commit hook fail."
    )
    assert '-e ".[dev]"' in text, (
        "Expected post-create.sh to install the project with its dev extras "
        '(`-e ".[dev]"`), which is what provides pytest, ruff, and mypy.'
    )


def test_post_create_pins_python_312() -> None:
    """The venv must be built on 3.12; pyproject requires >=3.12."""
    text = _POST_CREATE.read_text(encoding="utf-8")
    assert "--python 3.12" in text, (
        "Expected `uv venv --python 3.12` in post-create.sh. The base image's "
        "system Python is not pinned and pyproject.toml requires >=3.12."
    )


def test_devcontainer_puts_venv_on_path() -> None:
    """The venv's bin/ must be on PATH so `pytest` resolves without activation.

    The pre-commit hook runs as a plain subprocess with no shell activation, so
    an unactivated venv is invisible to it.
    """
    config = json.loads(_DEVCONTAINER_JSON.read_text(encoding="utf-8"))
    path_entry = config.get("remoteEnv", {}).get("PATH", "")
    assert ".venv/bin" in path_entry, (
        "Expected remoteEnv.PATH in devcontainer.json to include "
        "${containerWorkspaceFolder}/.venv/bin. Without it `pytest` and `ruff` "
        "are not on PATH and .githooks/pre-commit fails on every commit."
    )


def test_post_create_runs_before_hook_install() -> None:
    """postCreateCommand must provision deps before installing the hooks.

    Order matters only for the error message a contributor sees, but a hook
    installed against a container with no dependencies is a broken first commit.
    """
    config = json.loads(_DEVCONTAINER_JSON.read_text(encoding="utf-8"))
    command = config.get("postCreateCommand", "")
    post_create_pos = command.find("post-create.sh")
    hooks_pos = command.find("install-hooks.sh")
    assert post_create_pos != -1, "postCreateCommand must run post-create.sh"
    assert hooks_pos != -1, "postCreateCommand must run install-hooks.sh"
    assert post_create_pos < hooks_pos, (
        "post-create.sh (which installs the Python dependencies) must run "
        "before install-hooks.sh (which activates the hook that needs them)."
    )
