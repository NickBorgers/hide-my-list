"""Integration tests for the LLM callback wired through app/models.py.

Tests that:
1. llm(tier, caller=...) returns a model with the callback attached.
2. The callback is an LLMObservabilityCallback instance.
3. Calling .ainvoke() on the returned model triggers on_chat_model_start
   + on_llm_end (verified via callback hook simulation).
4. The callback's completed_calls is populated with the correct tier/caller.
5. Existing callers with no caller= kwarg still work (caller=None).

No real LLM calls are made.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from app.observability.llm_callback import LLMObservabilityCallback

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_model_cache() -> None:
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()


def _make_fake_response() -> MagicMock:
    """Minimal LLMResult-like mock with token metadata."""
    response = MagicMock()
    response.content = "Test response"
    response.usage_metadata = {"input_tokens": 10, "output_tokens": 5}
    return response


# ---------------------------------------------------------------------------
# Test: callback is attached to the returned model
# ---------------------------------------------------------------------------

def test_llm_factory_attaches_callback() -> None:
    """llm(tier, caller=...) must have an LLMObservabilityCallback in its callbacks."""
    _clear_model_cache()

    import os
    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        from app.models import llm
        model = llm("medium", caller="test")

    # The model returned by with_config is a RunnableBinding; callbacks live in .config
    cfg = getattr(model, "config", {}) or {}
    callbacks = cfg.get("callbacks", [])
    handler_instances = [cb for cb in callbacks if isinstance(cb, LLMObservabilityCallback)]

    assert handler_instances, (
        "Expected at least one LLMObservabilityCallback in model callbacks. "
        f"Got: {callbacks!r}"
    )
    handler = handler_instances[0]
    assert handler._tier == "medium"
    assert handler._caller == "test"

    _clear_model_cache()


def test_llm_factory_caller_none_is_valid() -> None:
    """llm(tier) without caller= kwarg must not raise; caller defaults to None."""
    _clear_model_cache()

    import os
    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        from app.models import llm
        model = llm("cheap")

    cfg = getattr(model, "config", {}) or {}
    callbacks = cfg.get("callbacks", [])
    handler_instances = [cb for cb in callbacks if isinstance(cb, LLMObservabilityCallback)]
    assert handler_instances
    assert handler_instances[0]._caller is None

    _clear_model_cache()


def test_llm_factory_different_tiers_produce_different_handlers() -> None:
    """Each llm() call creates a fresh LLMObservabilityCallback instance."""
    _clear_model_cache()

    import os
    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        from app.models import llm
        model_a = llm("medium", caller="intake")
        model_b = llm("cheap", caller="chat")

    def _get_handler(m: Any) -> LLMObservabilityCallback | None:
        cfg = getattr(m, "config", {}) or {}
        for cb in cfg.get("callbacks", []):
            if isinstance(cb, LLMObservabilityCallback):
                return cb
        return None

    handler_a = _get_handler(model_a)
    handler_b = _get_handler(model_b)

    assert handler_a is not None
    assert handler_b is not None
    assert handler_a is not handler_b  # separate instances
    assert handler_a._tier == "medium"
    assert handler_b._tier == "cheap"

    _clear_model_cache()


# ---------------------------------------------------------------------------
# Test: ainvoke produces start + end events via the callback
# ---------------------------------------------------------------------------

def test_ainvoke_triggers_callback_events() -> None:
    """Calling model.ainvoke() triggers on_chat_model_start + on_llm_end.

    No real API call is made. We verify that the callback's completed_calls is
    populated after the simulated call.
    """
    _clear_model_cache()

    import os
    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        from app.models import llm

        # Get the handler BEFORE mocking ainvoke, so we can inspect it.
        model = llm("medium", caller="test_integration")

        cfg = getattr(model, "config", {}) or {}
        handler = next(
            (cb for cb in cfg.get("callbacks", []) if isinstance(cb, LLMObservabilityCallback)),
            None,
        )
        assert handler is not None, "Handler must be attached"

        # Simulate a call by directly invoking the callback hooks (as LangChain would).
        # This is the cleanest approach without patching the entire LangChain runtime.
        run_id = uuid.uuid4()

        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        from langchain_core.outputs import ChatGeneration

        async def _simulate_call() -> None:
            await handler.on_chat_model_start(
                serialized={},
                messages=[[SystemMessage(content="system"), HumanMessage(content="human")]],
                run_id=run_id,
            )
            # Build a realistic LLMResult
            ai_message = AIMessage(content="response")
            ai_message.usage_metadata = {  # type: ignore[attr-defined]
                "input_tokens": 12,
                "output_tokens": 8,
            }
            gen = ChatGeneration(message=ai_message)
            from langchain_core.outputs import LLMResult
            llm_result = LLMResult(generations=[[gen]])
            await handler.on_llm_end(response=llm_result, run_id=run_id)

        asyncio.run(_simulate_call())

    assert len(handler.completed_calls) == 1
    metrics = handler.completed_calls[0]
    assert metrics.tier == "medium"
    assert metrics.caller == "test_integration"
    assert metrics.duration_ms >= 0.0
    assert metrics.input_tokens == 12
    assert metrics.output_tokens == 8
    assert metrics.total_tokens == 20

    _clear_model_cache()


# ---------------------------------------------------------------------------
# Test: start event fields — assert shape, not just that it was called
# ---------------------------------------------------------------------------

def test_start_event_kwargs_shape() -> None:
    """Assert mock.call_args.kwargs shape for llm.call.start, not just mock.called."""
    from unittest.mock import patch as _patch

    _clear_model_cache()

    import os
    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with _patch.dict(os.environ, env, clear=True):
        from app.models import llm
        model = llm("expensive", caller="selection")

        cfg = getattr(model, "config", {}) or {}
        handler = next(
            (cb for cb in cfg.get("callbacks", []) if isinstance(cb, LLMObservabilityCallback)),
            None,
        )
        assert handler is not None

        run_id = uuid.uuid4()
        with _patch("app.observability.llm_callback.log") as mock_log:
            asyncio.run(handler.on_chat_model_start(
                serialized={},
                messages=[[MagicMock(), MagicMock()]],
                run_id=run_id,
            ))

        mock_log.info.assert_called_once()
        call = mock_log.info.call_args
        assert call[0][0] == "llm.call.start"
        assert call[1]["tier"] == "expensive"
        assert call[1]["caller"] == "selection"
        assert call[1]["messages_count"] == 2
        assert isinstance(call[1]["correlation_id"], str)
        assert len(call[1]["correlation_id"]) == 32  # uuid4().hex

    _clear_model_cache()
