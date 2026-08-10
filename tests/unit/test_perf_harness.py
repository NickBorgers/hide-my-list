"""Unit tests for the perf harness aggregator functions.

Tests aggregate_latencies() and aggregate_tokens() in isolation using
synthetic data — no LLM calls.

Also verifies:
- aggregate_latencies raises ValueError on empty input
- p95 computation is correct for small and large lists
- Token means handle None values gracefully
- The perf module's gate flag is a boolean (smoke test for skip logic)
"""
from __future__ import annotations

import pytest

from tests.perf.test_llm_perf import aggregate_latencies, aggregate_tokens

# ---------------------------------------------------------------------------
# aggregate_latencies
# ---------------------------------------------------------------------------

def test_aggregate_latencies_single_value() -> None:
    result = aggregate_latencies([500.0])
    assert result["min"] == 500.0
    assert result["median"] == 500.0
    assert result["p95"] == 500.0
    assert result["max"] == 500.0


def test_aggregate_latencies_two_values() -> None:
    result = aggregate_latencies([100.0, 200.0])
    assert result["min"] == 100.0
    assert result["max"] == 200.0
    assert result["median"] == 150.0


def test_aggregate_latencies_small_list() -> None:
    latencies = [100.0, 200.0, 300.0, 400.0, 500.0]
    result = aggregate_latencies(latencies)
    assert result["min"] == 100.0
    assert result["max"] == 500.0
    assert result["median"] == 300.0


def test_aggregate_latencies_p95_is_correct() -> None:
    # 20 values: 0, 10, 20, ..., 190
    latencies = [float(i * 10) for i in range(20)]
    result = aggregate_latencies(latencies)
    # p95_idx = int(20 * 0.95) - 1 = int(19) - 1 = 18
    # sorted[18] = 180.0
    assert result["p95"] == 180.0
    assert result["min"] == 0.0
    assert result["max"] == 190.0


def test_aggregate_latencies_empty_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        aggregate_latencies([])


def test_aggregate_latencies_unsorted_input() -> None:
    result = aggregate_latencies([300.0, 100.0, 500.0, 200.0, 400.0])
    assert result["min"] == 100.0
    assert result["max"] == 500.0
    assert result["median"] == 300.0


def test_aggregate_latencies_returns_float_types() -> None:
    result = aggregate_latencies([100.0, 200.0, 300.0])
    assert isinstance(result["min"], float)
    assert isinstance(result["max"], float)


# ---------------------------------------------------------------------------
# aggregate_tokens
# ---------------------------------------------------------------------------

def test_aggregate_tokens_all_present() -> None:
    result = aggregate_tokens(
        all_input=[10, 20, 30],
        all_output=[5, 10, 15],
        all_total=[15, 30, 45],
    )
    assert result["mean_input_tokens"] == pytest.approx(20.0)
    assert result["mean_output_tokens"] == pytest.approx(10.0)
    assert result["mean_total_tokens"] == pytest.approx(30.0)


def test_aggregate_tokens_all_none() -> None:
    result = aggregate_tokens(
        all_input=[None, None],
        all_output=[None, None],
        all_total=[None, None],
    )
    assert result["mean_input_tokens"] is None
    assert result["mean_output_tokens"] is None
    assert result["mean_total_tokens"] is None


def test_aggregate_tokens_mixed_none() -> None:
    # Only non-None values should contribute to the mean
    result = aggregate_tokens(
        all_input=[10, None, 30],
        all_output=[5, None, 15],
        all_total=[15, None, 45],
    )
    assert result["mean_input_tokens"] == pytest.approx(20.0)  # mean(10, 30)
    assert result["mean_output_tokens"] == pytest.approx(10.0)  # mean(5, 15)
    assert result["mean_total_tokens"] == pytest.approx(30.0)  # mean(15, 45)


def test_aggregate_tokens_single_non_none() -> None:
    result = aggregate_tokens(
        all_input=[None, 50, None],
        all_output=[None, 25, None],
        all_total=[None, 75, None],
    )
    assert result["mean_input_tokens"] == pytest.approx(50.0)
    assert result["mean_output_tokens"] == pytest.approx(25.0)
    assert result["mean_total_tokens"] == pytest.approx(75.0)


def test_aggregate_tokens_empty_lists() -> None:
    result = aggregate_tokens(
        all_input=[],
        all_output=[],
        all_total=[],
    )
    assert result["mean_input_tokens"] is None
    assert result["mean_output_tokens"] is None
    assert result["mean_total_tokens"] is None


# ---------------------------------------------------------------------------
# Gate flag smoke test
# ---------------------------------------------------------------------------

def test_enable_llm_perf_flag_is_boolean() -> None:
    """The ENABLE_LLM_PERF gate flag must be a bool (not truthy-str)."""
    from tests.perf.test_llm_perf import _ENABLE_LLM_PERF
    assert isinstance(_ENABLE_LLM_PERF, bool)


def test_perf_harness_skip_behaviour_documented() -> None:
    """When ENABLE_LLM_PERF is unset, _PERF_MODELS should be empty.

    This asserts the skip-by-default contract: no parametrize IDs means
    no perf test collection, which means no real LLM calls.
    """
    import os
    env_val = os.environ.get("ENABLE_LLM_PERF", "")
    enabled = env_val.lower() in ("true", "1", "yes")

    from tests.perf.test_llm_perf import _PERF_MODELS
    if not enabled:
        # When the env var is absent/falsy, _PERF_MODELS must be empty
        # so no parametrized test cases are generated.
        assert _PERF_MODELS == [], (
            f"ENABLE_LLM_PERF is unset but _PERF_MODELS={_PERF_MODELS!r}. "
            "The harness would run real LLM calls unexpectedly."
        )
    # When enabled, _PERF_MODELS can be any non-empty list — that's fine.
