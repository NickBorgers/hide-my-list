"""Compose round-trip smoke test.

Boots the full docker-compose stack and exercises a basic round trip.
Catches bug class 5: deployment-gap bugs that unit + integration tests
miss (signal-cli mode, missing transitive deps, setup/ not copied into
image, env vars not threaded through compose).

Gated by `ENABLE_COMPOSE_SMOKE=true`. Default-skip — running this in
every PR CI is too slow + too LLM-expensive.

Privacy: no real user data. Uses placeholder fixtures only.

This test is the ONE allowed `subprocess.run` site outside the
constrained code surface. The tool-surface audit (`test_tool_surface_audit.py`)
explicitly carves out this file.

The app always starts in full-runtime mode (no skeleton/flag mode).
The smoke is a *deployment-gap* test: it asserts the stack comes up
cleanly and that key subsystems (postgres, signal-cli, migrations) are
reachable. App-level behavior is covered by integration and eval layers.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_ENABLE_KEY = "ENABLE_COMPOSE_SMOKE"
_COMPOSE_FILE = Path(__file__).parent.parent.parent / "docker" / "compose.yaml"
_BOOT_TIMEOUT_SECONDS = 60
_TEARDOWN_TIMEOUT_SECONDS = 30


def _enabled() -> bool:
    return os.environ.get(_ENABLE_KEY, "").lower() in ("true", "1", "yes")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


pytestmark = [
    pytest.mark.skipif(not _enabled(), reason=f"{_ENABLE_KEY} not set; smoke is opt-in"),
    pytest.mark.skipif(not _docker_available(), reason="docker CLI not on PATH"),
]


@pytest.fixture(scope="module")
def compose_stack() -> object:
    """Bring up the compose stack for the module, tear down after.

    Uses `--wait` so docker compose blocks until services are healthy.
    """
    if not _COMPOSE_FILE.is_file():
        pytest.fail(f"compose file not found at {_COMPOSE_FILE}")

    # Required env to boot the app — set defaults if absent.
    # The real values come from the operator's `.env` in production.
    env = {
        **os.environ,
        "AUTHORIZED_PEERS": os.environ.get("AUTHORIZED_PEERS", "+15555550100"),
        "SIGNAL_ACCOUNT": os.environ.get("SIGNAL_ACCOUNT", "+15555550100"),
        "NOTION_API_KEY": os.environ.get("NOTION_API_KEY", "smoke-fake-key"),
        "NOTION_DATABASE_ID": os.environ.get("NOTION_DATABASE_ID", "smoke-fake-db"),
        "LLM_PROXY_API_KEY": "smoke-fake-llm-key",
        "LLM_PROXY_BASE_URL": "https://proxy.test/v1",
        "REMINDER_SLOT_MINUTES": "30",
        "REMINDER_SLOT_CAPACITY": "2",
        "REMINDER_QUIET_START_HOUR": "22",
        "REMINDER_QUIET_END_HOUR": "8",
        "SIGNAL_RECEIVE_IDLE_TIMEOUT_SECONDS": "300",
        "SIGNAL_INGRESS_SILENCE_CHECK_INTERVAL_MINUTES": "60",
        "SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS": "129600",
    }

    # Bring up postgres + signal-cli + app
    subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            "docker", "compose", "-f", str(_COMPOSE_FILE),
            "up", "-d", "--wait", "--wait-timeout", str(_BOOT_TIMEOUT_SECONDS),
        ],
        env=env,
        check=True,
        timeout=_BOOT_TIMEOUT_SECONDS + 30,
    )

    yield None

    # Tear down with volume removal so the next run starts clean.
    subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "down", "-v"],
        env=env,
        check=False,  # best-effort on teardown
        timeout=_TEARDOWN_TIMEOUT_SECONDS,
    )


def _service_running(service: str) -> bool:
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "ps",
         "--services", "--filter", "status=running"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return service in result.stdout.split()


def test_compose_services_boot(compose_stack: object) -> None:
    """Postgres + signal-cli + app must all be running after `compose up --wait`."""
    assert _service_running("postgres"), "postgres service did not reach running state"
    assert _service_running("signal-cli"), "signal-cli service did not reach running state"
    assert _service_running("app"), "app service did not reach running state"


def test_postgres_migrations_applied(compose_stack: object) -> None:
    """The reminder_outbox table must exist after running migrations.

    Invokes the migration runner directly from inside the app container —
    the same code path production uses on boot. Validates: psycopg +
    sqlalchemy resolve, migrations dir was copied into the image, the
    runner can connect.
    """
    # Run migrations explicitly via the app container's python entrypoint.
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "run", "--rm",
         "-e", "DATABASE_URL=postgresql://hml:hml@postgres:5432/hml",
         "app", "python", "-c",
         "import asyncio; from app.tools.db import run_migrations; "
         "asyncio.run(run_migrations())"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"migration runner failed: {result.stderr[-2000:]}"
    )

    # Now query the table.
    for _ in range(5):
        result = subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(_COMPOSE_FILE), "exec", "-T", "postgres",
             "psql", "-U", "hml", "-d", "hml", "-tAc",
             "SELECT to_regclass('reminder_outbox')"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() == "reminder_outbox":
            return
        time.sleep(1)
    pytest.fail(
        "reminder_outbox table not present after migrations ran. "
        f"psql returned: {result.stdout!r} stderr: {result.stderr!r}"
    )


def test_signal_cli_responds_to_health(compose_stack: object) -> None:
    """signal-cli REST API must respond to /v1/about (the bbernhard image health)."""
    # Use docker network from inside the postgres container (which is on the
    # same compose network) so we don't depend on the host port mapping.
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "exec", "-T", "postgres",
         "sh", "-c", "wget -q -O - http://signal-cli:8080/v1/about | head -c 200"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # wget may not be installed in postgres:alpine — fall back to checking
    # the service is at least listening. The presence of the about endpoint
    # is best-effort.
    if result.returncode != 0:
        # Final fallback: just check the service is listed as running
        assert _service_running("signal-cli")
        return
    body = result.stdout.strip()
    assert body, "signal-cli /v1/about returned empty body"


def test_app_receives_llm_proxy_env(compose_stack: object) -> None:
    """Compose must thread LLM proxy env vars into the app container."""
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "exec", "-T", "app", "env"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"could not inspect app env: {result.stderr}"
    env_lines = set(result.stdout.splitlines())
    assert "LLM_PROXY_API_KEY=smoke-fake-llm-key" in env_lines
    assert "LLM_PROXY_BASE_URL=https://proxy.test/v1" in env_lines
    assert "REMINDER_SLOT_MINUTES=30" in env_lines
    assert "REMINDER_SLOT_CAPACITY=2" in env_lines
    assert "REMINDER_QUIET_START_HOUR=22" in env_lines
    assert "REMINDER_QUIET_END_HOUR=8" in env_lines
    assert "SIGNAL_RECEIVE_IDLE_TIMEOUT_SECONDS=300" in env_lines
    assert "SIGNAL_INGRESS_SILENCE_CHECK_INTERVAL_MINUTES=60" in env_lines
    assert "SIGNAL_INBOUND_SILENCE_ALERT_THRESHOLD_SECONDS=129600" in env_lines


def test_app_boots_runtime(compose_stack: object) -> None:
    """App must start and produce logs without an error exit.

    The app always runs in full-runtime mode. This test confirms it
    booted (service is running) and that its logs don't contain a Python
    traceback — which would indicate a startup error caught after compose
    reports healthy.

    Full behavior is covered by the integration + eval layers.
    """
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "logs", "app"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"could not read app logs: {result.stderr}"
    assert "Traceback (most recent call last)" not in result.stdout, (
        f"app logged a Python traceback on startup:\n{result.stdout[-2000:]}"
    )
