"""Guard: timezone resolution must not depend on the host's zoneinfo files.

`app/tools/time_context.py` resolves IANA timezones via `zoneinfo.ZoneInfo`.
Without the `tzdata` package, that lookup falls back to the host's
`/usr/share/zoneinfo` — absent on minimal containers and some self-hosted CI
runners, where `ZoneInfo("America/Chicago")` raises `ZoneInfoNotFoundError`.
Intake is the only graph node that calls `get_time_context()`, so the failure
surfaces as every intake turn taking its exception-fallback reply.

Bug class prevention: the nightly eval job on the homelab runner had no usable
zoneinfo source, so all intake fixtures errored in ~1ms before any model call.
These tests pin the `tzdata` dependency by running the resolution in a
subprocess with `PYTHONTZPATH` pointed at an empty value, which disables the
host-filesystem fallback and leaves `tzdata` as the only source.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _run_with_empty_tzpath(code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONTZPATH"] = ""
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_tzdata_package_is_installed() -> None:
    result = _run_with_empty_tzpath("import tzdata")
    assert result.returncode == 0, (
        "The tzdata package is not installed. It is a required dependency in "
        f"pyproject.toml — without it, hosts lacking system zoneinfo cannot "
        f"resolve any IANA timezone.\nstderr: {result.stderr}"
    )


def test_get_time_context_works_without_system_zoneinfo() -> None:
    result = _run_with_empty_tzpath(
        "from app.tools.time_context import get_time_context; "
        "ctx = get_time_context('now'); "
        "print(ctx['user_timezone'])"
    )
    assert result.returncode == 0, (
        "get_time_context() failed with the host zoneinfo path disabled. "
        "Timezone resolution must work from the tzdata package alone.\n"
        f"stderr: {result.stderr}"
    )
    assert result.stdout.strip(), "expected a resolved timezone name on stdout"
