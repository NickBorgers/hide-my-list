"""Pytest entry point for the eval layer.

Skips by default. Set `ENABLE_LIVE_LLM_EVALS=true` and `EVAL_MODELS=<...>`
to run.

The bulk of the eval logic lives in `tests/evals/runner.py`. This file
is the pytest collection surface so `pytest tests/evals/` discovers the
suite (skipped without env gates) and so CI workflows can run via
pytest if preferred.
"""
from __future__ import annotations

import os

import pytest

from tests.evals.runner import (
    discover_fixtures,
    run_session,
    write_report,
)


def _enabled() -> bool:
    return os.environ.get("ENABLE_LIVE_LLM_EVALS", "").lower() in ("true", "1", "yes")


def _models() -> list[str]:
    raw = os.environ.get("EVAL_MODELS", "").strip()
    return [m.strip() for m in raw.split(",") if m.strip()]


def _budget_usd() -> float:
    raw = os.environ.get("EVAL_BUDGET_USD", "").strip()
    if not raw:
        return float("inf")
    try:
        return float(raw)
    except ValueError:
        return float("inf")


pytestmark = pytest.mark.skipif(
    not _enabled() or not _models(),
    reason="evals are opt-in: set ENABLE_LIVE_LLM_EVALS=true and EVAL_MODELS=<aliases>",
)


def test_eval_suite_baseline_all_pass() -> None:
    """Run every fixture against every model in EVAL_MODELS.

    Baseline models (EVAL_BASELINE_MODELS env, comma-separated) must
    pass every contract. Candidate models (anything in EVAL_MODELS
    that isn't baseline) are informational — their failures don't
    fail this test.
    """
    fixtures = discover_fixtures()
    if not fixtures:
        pytest.skip("No fixtures in tests/evals/fixtures/")

    models = _models()
    results = list(run_session(fixtures, models, budget_usd=_budget_usd()))

    out_dir = write_report(results)
    print(f"\nEval report: {out_dir / 'report.md'}")  # noqa: T201 — surfaced to pytest output

    baseline_raw = os.environ.get("EVAL_BASELINE_MODELS", "").strip()
    baseline_models = {m.strip() for m in baseline_raw.split(",") if m.strip()}
    if not baseline_models:
        # Treat all models as baseline if no explicit baseline set.
        baseline_models = set(models)

    failures = [
        r
        for r in results
        if r.model in baseline_models and not r.passed
    ]
    if failures:
        msgs = []
        for r in failures:
            failed_contracts = [c for c in r.contracts if not c.passed]
            if r.error:
                msgs.append(f"{r.model} / {r.fixture_id}: ERROR — {r.error}")
            else:
                for c in failed_contracts:
                    msgs.append(f"{r.model} / {r.fixture_id} / {c.kind}: {c.detail}")
        pytest.fail(
            f"Baseline models failed {len(failures)} fixture(s). "
            f"See {out_dir / 'report.md'} for the comparison table. Failures:\n"
            + "\n".join(f"  - {m}" for m in msgs[:20])
        )
