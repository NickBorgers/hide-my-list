"""Unit tests for app/observability/llm_callback.py.

Tests the LLMObservabilityCallback in isolation (no real LLM calls).
Covers:
- on_chat_model_start + on_llm_end records duration + tokens correctly
- on_llm_error records duration + error type
- Missing token metadata doesn't crash (graceful fallback to None)
- Multiple concurrent calls (different run_ids) don't collide
- Privacy: no prompt or response text appears in logged fields
- get_last_call_metrics() returns correct snapshot
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from app.observability.llm_callback import (
    LLMObservabilityCallback,
    _elapsed_ms,
    _safe_int,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler(
    tier: str = "medium",
    model: str = "claude-sonnet-4-6",
    caller: str | None = "test",
) -> LLMObservabilityCallback:
    return LLMObservabilityCallback(tier=tier, model=model, caller=caller)


def _make_llm_result(
    *,
    token_usage: dict[str, int] | None = None,
    usage_metadata: dict[str, int] | None = None,
) -> MagicMock:
    """Build a minimal LLMResult mock with optional token metadata."""
    result = MagicMock()

    if token_usage is not None:
        result.llm_output = {"token_usage": token_usage}
    else:
        result.llm_output = None

    if usage_metadata is not None:
        gen = MagicMock()
        gen.message = MagicMock()
        gen.message.usage_metadata = usage_metadata
        result.generations = [[gen]]
    else:
        result.generations = []

    return result


# ---------------------------------------------------------------------------
# _safe_int helper
# ---------------------------------------------------------------------------

def test_safe_int_converts_int() -> None:
    assert _safe_int(42) == 42


def test_safe_int_converts_float() -> None:
    assert _safe_int(3.7) == 3


def test_safe_int_returns_none_for_none() -> None:
    assert _safe_int(None) is None


def test_safe_int_returns_none_for_string_non_numeric() -> None:
    assert _safe_int("abc") is None


def test_safe_int_converts_numeric_string() -> None:
    assert _safe_int("100") == 100


# ---------------------------------------------------------------------------
# _elapsed_ms helper
# ---------------------------------------------------------------------------

def test_elapsed_ms_returns_zero_for_none() -> None:
    assert _elapsed_ms(None) == 0.0


def test_elapsed_ms_positive_for_past_start() -> None:
    import time
    start = time.monotonic() - 0.5  # 500ms ago
    elapsed = _elapsed_ms(start)
    assert elapsed >= 400.0  # allow some slack


# ---------------------------------------------------------------------------
# on_chat_model_start
# ---------------------------------------------------------------------------

def test_on_chat_model_start_records_start_time() -> None:
    handler = _make_handler()
    run_id = uuid.uuid4()

    log_events: list[dict] = []

    def _capture(logger: Any, method: str, event_dict: dict) -> dict:
        log_events.append(dict(event_dict))
        return event_dict

    with patch("app.observability.llm_callback.log"):
        asyncio.run(handler.on_chat_model_start(
            serialized={},
            messages=[[MagicMock(), MagicMock()]],
            run_id=run_id,
        ))

    run_key = str(run_id)
    assert run_key in handler._start_times
    assert run_key in handler._correlation_ids


def test_on_chat_model_start_emits_start_event() -> None:
    handler = _make_handler(tier="expensive", caller="selection")
    run_id = uuid.uuid4()

    with patch("app.observability.llm_callback.log") as mock_log:
        asyncio.run(handler.on_chat_model_start(
            serialized={},
            messages=[[MagicMock(), MagicMock(), MagicMock()]],
            run_id=run_id,
        ))
        mock_log.info.assert_called_once()
        call_kwargs = mock_log.info.call_args
        # event name is positional arg 0
        assert call_kwargs[0][0] == "llm.call.start"
        kwargs = call_kwargs[1]
        assert kwargs["tier"] == "expensive"
        assert kwargs["caller"] == "selection"
        assert kwargs["messages_count"] == 3
        assert "correlation_id" in kwargs
        # Privacy: no content fields
        assert "messages" not in kwargs
        assert "prompt" not in kwargs
        assert "content" not in kwargs


# ---------------------------------------------------------------------------
# on_llm_end: token extraction shape A (llm_output["token_usage"])
# ---------------------------------------------------------------------------

def test_on_llm_end_extracts_tokens_from_llm_output() -> None:
    handler = _make_handler()
    run_id = uuid.uuid4()

    asyncio.run(handler.on_chat_model_start(
        serialized={}, messages=[[MagicMock()]], run_id=run_id,
    ))

    result = _make_llm_result(token_usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

    with patch("app.observability.llm_callback.log"):
        asyncio.run(handler.on_llm_end(response=result, run_id=run_id))

    assert len(handler.completed_calls) == 1
    metrics = handler.completed_calls[0]
    assert metrics.input_tokens == 10
    assert metrics.output_tokens == 20
    assert metrics.total_tokens == 30
    assert metrics.duration_ms >= 0.0


# ---------------------------------------------------------------------------
# on_llm_end: token extraction shape B (usage_metadata on message)
# ---------------------------------------------------------------------------

def test_on_llm_end_extracts_tokens_from_usage_metadata() -> None:
    handler = _make_handler()
    run_id = uuid.uuid4()

    asyncio.run(handler.on_chat_model_start(
        serialized={}, messages=[[MagicMock()]], run_id=run_id,
    ))

    result = _make_llm_result(
        usage_metadata={"input_tokens": 15, "output_tokens": 25}
    )

    with patch("app.observability.llm_callback.log"):
        asyncio.run(handler.on_llm_end(response=result, run_id=run_id))

    metrics = handler.completed_calls[0]
    assert metrics.input_tokens == 15
    assert metrics.output_tokens == 25
    assert metrics.total_tokens == 40  # derived from parts


# ---------------------------------------------------------------------------
# on_llm_end: missing token metadata — graceful fallback to None
# ---------------------------------------------------------------------------

def test_on_llm_end_graceful_when_no_token_metadata() -> None:
    handler = _make_handler()
    run_id = uuid.uuid4()

    asyncio.run(handler.on_chat_model_start(
        serialized={}, messages=[[MagicMock()]], run_id=run_id,
    ))

    result = MagicMock()
    result.llm_output = None
    result.generations = []

    with patch("app.observability.llm_callback.log"):
        asyncio.run(handler.on_llm_end(response=result, run_id=run_id))

    metrics = handler.completed_calls[0]
    assert metrics.input_tokens is None
    assert metrics.output_tokens is None
    assert metrics.total_tokens is None
    # Must not raise — graceful degradation


# ---------------------------------------------------------------------------
# on_llm_end: emits correct log event fields
# ---------------------------------------------------------------------------

def test_on_llm_end_emits_end_event_with_correct_fields() -> None:
    handler = _make_handler(tier="medium", caller="intake")
    run_id = uuid.uuid4()

    asyncio.run(handler.on_chat_model_start(
        serialized={}, messages=[[MagicMock()]], run_id=run_id,
    ))

    result = _make_llm_result(token_usage={"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13})

    with patch("app.observability.llm_callback.log") as mock_log:
        asyncio.run(handler.on_llm_end(response=result, run_id=run_id))

    # on_llm_end should call log.info (not log.warning)
    mock_log.info.assert_called_once()
    call_args = mock_log.info.call_args
    assert call_args[0][0] == "llm.call.end"
    kwargs = call_args[1]
    assert kwargs["tier"] == "medium"
    assert kwargs["caller"] == "intake"
    assert kwargs["input_tokens"] == 5
    assert kwargs["output_tokens"] == 8
    assert kwargs["total_tokens"] == 13
    assert isinstance(kwargs["duration_ms"], float)
    # Privacy: no content fields
    assert "response" not in kwargs
    assert "text" not in kwargs
    assert "content" not in kwargs
    assert "message" not in kwargs


# ---------------------------------------------------------------------------
# on_llm_error
# ---------------------------------------------------------------------------

def test_on_llm_error_emits_error_event() -> None:
    handler = _make_handler(tier="cheap", caller="check_in")
    run_id = uuid.uuid4()

    asyncio.run(handler.on_chat_model_start(
        serialized={}, messages=[[MagicMock()]], run_id=run_id,
    ))

    error = ConnectionError("timeout")

    with patch("app.observability.llm_callback.log") as mock_log:
        asyncio.run(handler.on_llm_error(error=error, run_id=run_id))

    mock_log.warning.assert_called_once()
    call_args = mock_log.warning.call_args
    assert call_args[0][0] == "llm.call.error"
    kwargs = call_args[1]
    assert kwargs["error_type"] == "ConnectionError"
    assert kwargs["tier"] == "cheap"
    assert kwargs["caller"] == "check_in"
    assert isinstance(kwargs["duration_ms"], float)
    # No error message text logged
    assert "error" not in kwargs
    assert "message" not in kwargs


def test_on_llm_error_cleans_up_run_state() -> None:
    handler = _make_handler()
    run_id = uuid.uuid4()

    asyncio.run(handler.on_chat_model_start(
        serialized={}, messages=[[MagicMock()]], run_id=run_id,
    ))
    assert str(run_id) in handler._start_times

    with patch("app.observability.llm_callback.log"):
        asyncio.run(handler.on_llm_error(error=ValueError("x"), run_id=run_id))

    assert str(run_id) not in handler._start_times
    assert str(run_id) not in handler._correlation_ids


# ---------------------------------------------------------------------------
# Multiple concurrent calls: different run_ids must not collide
# ---------------------------------------------------------------------------

def test_concurrent_calls_do_not_collide() -> None:
    """Two calls with different run_ids produce independent metrics."""
    handler = _make_handler()
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()

    async def _two_calls() -> None:
        # Start both
        await handler.on_chat_model_start(
            serialized={}, messages=[[MagicMock()]], run_id=run_id_a,
        )
        await handler.on_chat_model_start(
            serialized={}, messages=[[MagicMock(), MagicMock()]], run_id=run_id_b,
        )

        # End A
        result_a = _make_llm_result(
            token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        )
        await handler.on_llm_end(response=result_a, run_id=run_id_a)

        # End B
        result_b = _make_llm_result(
            token_usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}
        )
        await handler.on_llm_end(response=result_b, run_id=run_id_b)

    with patch("app.observability.llm_callback.log"):
        asyncio.run(_two_calls())

    assert len(handler.completed_calls) == 2
    # The correlation IDs must be different
    ids = {m.correlation_id for m in handler.completed_calls}
    assert len(ids) == 2

    # Token values must not be swapped between calls
    totals = {m.total_tokens for m in handler.completed_calls}
    assert 15 in totals
    assert 28 in totals


# ---------------------------------------------------------------------------
# get_last_call_metrics
# ---------------------------------------------------------------------------

def test_get_last_call_metrics_returns_none_when_empty() -> None:
    handler = _make_handler()
    assert handler.get_last_call_metrics() is None


def test_get_last_call_metrics_returns_most_recent() -> None:
    handler = _make_handler()
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()

    async def _two() -> None:
        await handler.on_chat_model_start(
            serialized={}, messages=[[MagicMock()]], run_id=run_id_a,
        )
        result_a = _make_llm_result(token_usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
        await handler.on_llm_end(response=result_a, run_id=run_id_a)

        await handler.on_chat_model_start(
            serialized={}, messages=[[MagicMock()]], run_id=run_id_b,
        )
        result_b = _make_llm_result(token_usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300})
        await handler.on_llm_end(response=result_b, run_id=run_id_b)

    with patch("app.observability.llm_callback.log"):
        asyncio.run(_two())

    last = handler.get_last_call_metrics()
    assert last is not None
    assert last.total_tokens == 300


# ---------------------------------------------------------------------------
# Privacy: assert no prompt/response content in logged events
# ---------------------------------------------------------------------------

def test_privacy_no_prompt_text_in_log_events() -> None:
    """Captured log fields must not include message content or response text."""
    handler = _make_handler()
    run_id = uuid.uuid4()

    captured_calls: list[tuple] = []

    class _CapturingLog:
        def info(self, event: str, **kwargs: Any) -> None:
            captured_calls.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            captured_calls.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            captured_calls.append((event, kwargs))

    with patch("app.observability.llm_callback.log", _CapturingLog()):
        asyncio.run(handler.on_chat_model_start(
            serialized={},
            messages=[[MagicMock(content="SECRET PROMPT TEXT")]],
            run_id=run_id,
        ))
        result = _make_llm_result(token_usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
        asyncio.run(handler.on_llm_end(response=result, run_id=run_id))

    assert captured_calls, "Expected at least one log event"

    private_strings = ["SECRET PROMPT TEXT"]
    for event_name, kwargs in captured_calls:
        for field_value in kwargs.values():
            val_str = str(field_value)
            for private in private_strings:
                assert private not in val_str, (
                    f"Private text '{private}' found in log event '{event_name}' "
                    f"field with value: {val_str!r}"
                )


# ---------------------------------------------------------------------------
# caller=None is valid (backward-compat)
# ---------------------------------------------------------------------------

def test_caller_none_is_valid() -> None:
    handler = _make_handler(caller=None)
    run_id = uuid.uuid4()

    with patch("app.observability.llm_callback.log") as mock_log:
        asyncio.run(handler.on_chat_model_start(
            serialized={}, messages=[[MagicMock()]], run_id=run_id,
        ))
        call_kwargs = mock_log.info.call_args[1]
        assert call_kwargs["caller"] is None
