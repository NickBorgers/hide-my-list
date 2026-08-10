"""CI gate: tool surface audit.

Verifies that httpx.AsyncClient appears only in the three authorised modules
(as of Phase A):
  - app/tools/notion.py
  - app/tools/signal_client.py
  - app/ingress/signal_listener.py

Also checks that eval, exec, os.system, subprocess shell=True are not present
in app/ code.

This test fails CI if any new httpx.AsyncClient usage sneaks into a non-approved
module.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent.parent / "app"

_ALLOWED_ASYNC_CLIENT_MODULES = {
    "app/tools/notion.py",
    "app/tools/signal_client.py",
    "app/ingress/signal_listener.py",
}

_BANNED_PATTERNS = [
    (re.compile(r"\bos\.system\s*\("), "os.system"),
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"\bexec\s*\("), "exec"),
    (re.compile(r"subprocess\.[^(]+\(.*shell\s*=\s*True"), "subprocess shell=True"),
    # Bug-class 5 (deployment-gap) defense: app/ must not shell out at all.
    # Any subprocess invocation expands the constrained-tool-surface.
    # The compose-smoke test in tests/smoke/ legitimately uses subprocess
    # to drive docker compose; the audit only scans app/, so that test
    # is unaffected.
    (re.compile(r"\bsubprocess\.(run|Popen|call|check_output|check_call)\s*\("), "subprocess (any form)"),
    (re.compile(r"\bos\.popen\s*\("), "os.popen"),
    (re.compile(r"\bpty\.spawn\s*\("), "pty.spawn"),
    # Sync httpx.Client is also constrained — the async surface is the
    # only HTTP egress path. Same allowlist applies as for AsyncClient.
    (re.compile(r"\bhttpx\.Client\s*\("), "httpx.Client (sync)"),
]


def _relative(path: Path) -> str:
    """Return path relative to repo root."""
    try:
        return str(path.relative_to(APP_ROOT.parent))
    except ValueError:
        return str(path)


def test_httpx_async_client_restricted_to_allowed_modules() -> None:
    """httpx.AsyncClient must not appear outside the three allowed modules."""
    violations: list[str] = []

    for py_file in APP_ROOT.rglob("*.py"):
        rel = _relative(py_file)
        # Skip the allowed modules themselves
        if rel in _ALLOWED_ASYNC_CLIENT_MODULES:
            continue

        source = py_file.read_text(encoding="utf-8")
        if "AsyncClient" in source:
            violations.append(rel)

    assert not violations, (
        "httpx.AsyncClient found outside allowed modules:\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\nAllowed modules: "
        + str(_ALLOWED_ASYNC_CLIENT_MODULES)
    )


def test_no_banned_patterns_in_app() -> None:
    """eval, exec, os.system, subprocess shell=True must not appear in app/."""
    violations: list[str] = []

    for py_file in APP_ROOT.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        rel = _relative(py_file)
        for pattern, label in _BANNED_PATTERNS:
            if pattern.search(source):
                violations.append(f"{rel}: contains '{label}'")

    assert not violations, (
        "Banned patterns found in app/:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_app_code_is_parseable_python() -> None:
    """All .py files in app/ must parse without SyntaxError."""
    errors: list[str] = []
    for py_file in APP_ROOT.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            errors.append(f"{_relative(py_file)}: {exc}")

    assert not errors, (
        "Syntax errors found:\n" + "\n".join(f"  {e}" for e in errors)
    )
