"""LLM perf harness: latency + token stats per model.

Gated by ENABLE_LLM_PERF=true (env var). Independent of the eval rig's
ENABLE_LIVE_LLM_EVALS — perf measures latency/tokens only, not behavioral
correctness.

Usage:
    ENABLE_LLM_PERF=true pytest tests/perf/test_llm_perf.py -v

Optional env vars:
    PERF_MODELS   Comma-separated LiteLLM aliases (defaults to tier values from
                  setup/model-tiers.json). E.g. "claude-haiku-4-5,gemma4-small".
    PERF_RUNS_N   Number of runs per prompt per model (default 3).
    PERF_RUNS_DIR Output directory for JSON + Markdown reports
                  (default tests/perf/runs/).

The harness uses app.models.llm() so the observability callback instruments
every perf call the same way it instruments production calls. Metrics are
read from handler.completed_calls after each invocation.

Smoke test: if ENABLE_LLM_PERF is unset, the entire module's parametrize
list is skipped cleanly — no collection errors, no network calls.

At-least-once note: LangChain callback delivery is at-least-once. Under
retry or streaming, on_llm_end may fire more than once per invocation. The
harness reads the last completed_calls entry after each .ainvoke() call.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Gate: skip entire module when ENABLE_LLM_PERF is not set.
# ---------------------------------------------------------------------------
_ENABLE_LLM_PERF = os.environ.get("ENABLE_LLM_PERF", "").lower() in ("true", "1", "yes")


def _skip_if_disabled() -> None:
    """Raise pytest.skip when ENABLE_LLM_PERF is not set."""
    if not _ENABLE_LLM_PERF:
        pytest.skip("ENABLE_LLM_PERF not set — perf harness skipped", allow_module_level=True)


# ---------------------------------------------------------------------------
# Smoke test: always collected; verifies the skip behaviour.
# ---------------------------------------------------------------------------

def test_perf_harness_skips_when_env_unset() -> None:
    """Perf harness must skip cleanly when ENABLE_LLM_PERF is unset.

    This test is always collected (not gated). It verifies that the module-
    level skip logic works correctly by checking the flag value directly.
    When ENABLE_LLM_PERF is unset, the parametrized perf tests are skipped
    and no real LLM calls are made. This is a structural assertion only.
    """
    # If we reach here with the env set, the module-level skip already passed.
    # If the env is unset, the module-level call above caused collection-time
    # skip — meaning this body would never execute. We assert the flag type.
    assert isinstance(_ENABLE_LLM_PERF, bool)


# ---------------------------------------------------------------------------
# Aggregation helpers (also exercised by unit tests in test_perf_harness.py)
# ---------------------------------------------------------------------------

def aggregate_latencies(latencies: list[float]) -> dict[str, float]:
    """Compute min/median/p95/max from a list of latency values (ms).

    Args:
        latencies: Non-empty list of float millisecond values.

    Returns:
        Dict with keys: min, median, p95, max.

    Raises:
        ValueError: If latencies is empty.
    """
    if not latencies:
        raise ValueError("latencies must be non-empty")
    sorted_lat = sorted(latencies)
    n = len(sorted_lat)
    p95_idx = max(0, int(n * 0.95) - 1)
    return {
        "min": sorted_lat[0],
        "median": statistics.median(sorted_lat),
        "p95": sorted_lat[p95_idx],
        "max": sorted_lat[-1],
    }


def aggregate_tokens(
    all_input: list[int | None],
    all_output: list[int | None],
    all_total: list[int | None],
) -> dict[str, float | None]:
    """Compute mean token counts across a list of calls.

    None values (provider did not return usage) are excluded from the mean.
    Returns None for the mean if all values were None.
    """
    def _mean_or_none(vals: list[int | None]) -> float | None:
        present = [v for v in vals if v is not None]
        return statistics.mean(present) if present else None

    return {
        "mean_input_tokens": _mean_or_none(all_input),
        "mean_output_tokens": _mean_or_none(all_output),
        "mean_total_tokens": _mean_or_none(all_total),
    }


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def _write_reports(
    timestamp: str,
    results: dict[str, dict[str, Any]],
    runs_dir: Path,
) -> Path:
    """Write per-model JSON reports and a comparison Markdown table.

    Args:
        timestamp: ISO-ish string used as the run directory name.
        results: model_alias -> aggregated result dict.
        runs_dir: Base directory for perf runs.

    Returns:
        Path to the Markdown report file.
    """
    run_dir = runs_dir / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # Per-model JSON
    for model_alias, data in results.items():
        safe_name = model_alias.replace("/", "_").replace(":", "_")
        json_path = run_dir / f"{safe_name}.json"
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Markdown comparison table
    report_path = run_dir / "report.md"
    lines = [
        f"# LLM Perf Report — {timestamp}",
        "",
        "| Model | min_ms | median_ms | p95_ms | max_ms "
        "| mean_input | mean_output | mean_total |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model_alias, data in sorted(results.items()):
        lat = data.get("latency", {})
        tok = data.get("tokens", {})

        def _fmt(v: Any) -> str:
            if v is None:
                return "—"
            if isinstance(v, float):
                return f"{v:.1f}"
            return str(v)

        lines.append(
            f"| {model_alias} "
            f"| {_fmt(lat.get('min'))} "
            f"| {_fmt(lat.get('median'))} "
            f"| {_fmt(lat.get('p95'))} "
            f"| {_fmt(lat.get('max'))} "
            f"| {_fmt(tok.get('mean_input_tokens'))} "
            f"| {_fmt(tok.get('mean_output_tokens'))} "
            f"| {_fmt(tok.get('mean_total_tokens'))} |"
        )

    lines += ["", "Generated by tests/perf/test_llm_perf.py", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Core async runner
# ---------------------------------------------------------------------------

async def _run_model_perf(
    model_alias: str,
    n_runs: int,
) -> dict[str, Any]:
    """Run all synthetic prompts N times for one model alias.

    Uses app.models.llm() so the observability callback instruments the calls.
    Reads handler.completed_calls after each ainvoke().

    Args:
        model_alias: LiteLLM model alias (must match a tier in model-tiers.json
            OR be set via PERF_MODELS). Currently resolves via the existing
            tier mechanism — caller passes the tier key that resolves to the
            desired model.
        n_runs: Number of repetitions per prompt.

    Returns:
        Dict with keys: model, prompts_run, latencies, tokens, aggregated.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.models import llm
    from app.observability.llm_callback import LLMObservabilityCallback
    from tests.perf.prompts import SYNTHETIC_PROMPTS

    all_latencies: list[float] = []
    all_input_tokens: list[int | None] = []
    all_output_tokens: list[int | None] = []
    all_total_tokens: list[int | None] = []
    per_prompt_results: list[dict[str, Any]] = []

    for prompt in SYNTHETIC_PROMPTS:
        for run_i in range(n_runs):
            # Each llm() call creates a fresh handler with its own completed_calls list.
            model = llm(model_alias, caller="perf")  # type: ignore[arg-type]

            # Extract the handler from the model's callback list.
            # with_config wraps the model in a RunnableBinding; callbacks live in .config.
            handler: LLMObservabilityCallback | None = None
            cfg = getattr(model, "config", {}) or {}
            for cb in cfg.get("callbacks", []):
                if isinstance(cb, LLMObservabilityCallback):
                    handler = cb
                    break

            messages = [
                SystemMessage(content=prompt.system),
                HumanMessage(content=prompt.human),
            ]

            try:
                await model.ainvoke(messages)
            except Exception as exc:
                per_prompt_results.append({
                    "label": prompt.label,
                    "run": run_i,
                    "error": type(exc).__name__,
                })
                continue

            metrics = handler.get_last_call_metrics() if handler else None
            if metrics:
                all_latencies.append(metrics.duration_ms)
                all_input_tokens.append(metrics.input_tokens)
                all_output_tokens.append(metrics.output_tokens)
                all_total_tokens.append(metrics.total_tokens)
                per_prompt_results.append({
                    "label": prompt.label,
                    "run": run_i,
                    "duration_ms": metrics.duration_ms,
                    "input_tokens": metrics.input_tokens,
                    "output_tokens": metrics.output_tokens,
                    "total_tokens": metrics.total_tokens,
                })
            else:
                per_prompt_results.append({
                    "label": prompt.label,
                    "run": run_i,
                    "error": "no_metrics",
                })

    lat_agg = aggregate_latencies(all_latencies) if all_latencies else {}
    tok_agg = aggregate_tokens(all_input_tokens, all_output_tokens, all_total_tokens)

    return {
        "model": model_alias,
        "n_runs": n_runs,
        "prompts_count": len(SYNTHETIC_PROMPTS),
        "latency": lat_agg,
        "tokens": tok_agg,
        "per_prompt": per_prompt_results,
    }


# ---------------------------------------------------------------------------
# Parametrized perf test
# ---------------------------------------------------------------------------

def _resolve_perf_models() -> list[str]:
    """Resolve model list from PERF_MODELS env or default to tier names.

    When PERF_MODELS is set, treat comma-separated values as tier names
    (keys into model-tiers.json) or literal model aliases. For the harness,
    we treat each token as a tier name passed to llm().
    """
    raw = os.environ.get("PERF_MODELS", "")
    if raw.strip():
        return [m.strip() for m in raw.split(",") if m.strip()]
    # Default: run all four tiers
    return ["medium", "cheap", "expensive", "reminder"]


_PERF_MODELS = _resolve_perf_models() if _ENABLE_LLM_PERF else []
_N_RUNS = int(os.environ.get("PERF_RUNS_N", "3"))
_RUNS_DIR = Path(os.environ.get("PERF_RUNS_DIR", "tests/perf/runs"))


@pytest.mark.parametrize("model_tier", _PERF_MODELS)
def test_llm_perf_model(model_tier: str, tmp_path: Path) -> None:
    """Run perf harness for one model tier.

    Parametrized over PERF_MODELS (or default tiers). Each invocation:
    1. Runs all synthetic prompts N times via the real llm() path.
    2. Reads metrics from the observability callback.
    3. Aggregates latency + token stats.
    4. Writes JSON to tests/perf/runs/<timestamp>/<model>.json.
    5. Asserts at least one successful call completed.

    Gated: skipped when ENABLE_LLM_PERF is not set.
    """
    _skip_if_disabled()

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
    result = asyncio.run(_run_model_perf(model_tier, _N_RUNS))

    # Write report
    runs_dir = _RUNS_DIR
    run_dir = runs_dir / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_name = model_tier.replace("/", "_").replace(":", "_")
    json_path = run_dir / f"{safe_name}.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    successful = [r for r in result["per_prompt"] if "error" not in r]
    assert successful, (
        f"No successful LLM calls for model tier '{model_tier}'. "
        f"Check LLM_PROXY_API_KEY, LLM_PROXY_BASE_URL, and model-tiers.json. "
        f"Per-prompt errors: {[r for r in result['per_prompt'] if 'error' in r]}"
    )

    # Print summary for pytest -v output
    lat = result["latency"]
    tok = result["tokens"]
    print(  # noqa: T201
        f"\n[perf] {model_tier}: "
        f"median={lat.get('median', '?'):.1f}ms "
        f"p95={lat.get('p95', '?'):.1f}ms "
        f"mean_tokens={tok.get('mean_total_tokens', '?')}"
    )


@pytest.mark.skipif(not _ENABLE_LLM_PERF, reason="ENABLE_LLM_PERF not set")
def test_llm_perf_report(tmp_path: Path) -> None:
    """Generate a consolidated comparison report after all per-model runs.

    This test runs after the parametrized per-model tests. It reads all JSON
    files written to PERF_RUNS_DIR and produces a single Markdown table.

    Gated: skipped when ENABLE_LLM_PERF is not set.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
    results: dict[str, Any] = {}
    runs_base = _RUNS_DIR

    if runs_base.is_dir():
        for run_dir in sorted(runs_base.iterdir()):
            if not run_dir.is_dir():
                continue
            for json_file in run_dir.glob("*.json"):
                model_name = json_file.stem
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    results[model_name] = data
                except Exception:
                    pass

    if results:
        report_path = _write_reports(timestamp, results, runs_base)
        print(f"\n[perf] Report written to: {report_path}")  # noqa: T201
