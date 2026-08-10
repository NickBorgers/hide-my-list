"""LLM observability callback handler for hide-my-list.

Hooks into LangChain's async callback system to capture token usage and
wall-clock latency for every LLM call. Emits structured log events via
structlog so the signals ship to Gravwell via the existing log pipeline.

Privacy invariant: this module NEVER logs message content, prompt text,
response text, task titles, or any user-identifiable content. Only structural
metadata (token counts, durations, model names, tier, caller, correlation_id)
is captured. The redact processor in app/main.py is the safety net; this
module must never place private data into log calls in the first place.

Event schema (emitted to structlog at INFO/WARNING level):

  llm.call.start
    correlation_id: str  — UUID hex, per-call unique
    tier:           str  — e.g. "medium"
    model:          str  — resolved model ID from setup/model-tiers.json
    caller:         str | None  — node name, e.g. "intake"
    messages_count: int  — number of messages in the prompt

  llm.call.end
    correlation_id: str
    tier:           str
    model:          str
    caller:         str | None
    duration_ms:    float
    input_tokens:   int | None
    output_tokens:  int | None
    total_tokens:   int | None

  llm.call.error
    correlation_id: str
    tier:           str
    model:          str
    caller:         str | None
    duration_ms:    float
    error_type:     str  — type(exc).__name__

At-least-once note: on_llm_end and on_llm_error are invoked at-least-once by
LangChain's callback machinery. The handler does not guarantee exactly-once
delivery of log events under retry or streaming conditions.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

log = structlog.get_logger(__name__)


@dataclass
class CallMetrics:
    """Immutable snapshot of a completed LLM call's performance data.

    All fields reflect what was observable at call-end time. Token fields
    are None when the provider did not return usage metadata.
    """
    correlation_id: str
    tier: str
    model: str
    caller: str | None
    duration_ms: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class LLMObservabilityCallback(AsyncCallbackHandler):
    """Async LangChain callback that records token usage and latency per call.

    Construct one instance per llm() call (caller + tier baked in).
    After the model invocation, inspect handler.completed_calls for results.

    Thread/concurrency note: LangChain may call on_llm_start with different
    run_ids concurrently when multiple chains run in parallel. This handler
    uses a per-run_id dict keyed on the UUID string to avoid collisions.
    Multiple concurrent calls produce independent, non-colliding records.

    Usage:
        handler = LLMObservabilityCallback(tier="medium", model=model_id, caller="intake")
        model = ChatOpenAI(...).with_config(callbacks=[handler])
        await model.ainvoke(messages)
        metrics = handler.completed_calls  # list[CallMetrics]
        # Access handler after with_config via: model.config["callbacks"][0]
    """

    def __init__(self, *, tier: str, model: str, caller: str | None) -> None:
        super().__init__()
        self._tier = tier
        self._model = model
        self._caller = caller
        # Mutable state: keyed by str(run_id) to support concurrent calls.
        self._start_times: dict[str, float] = {}
        self._correlation_ids: dict[str, str] = {}
        # Completed call records accumulate here for the perf harness.
        self.completed_calls: list[CallMetrics] = []

    # ------------------------------------------------------------------
    # LangChain async callback hooks
    # ------------------------------------------------------------------

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Record start time and emit llm.call.start event.

        messages is a list of lists (one per generation): count the first batch.
        """
        run_key = str(run_id)
        correlation_id = uuid.uuid4().hex
        self._start_times[run_key] = time.monotonic()
        self._correlation_ids[run_key] = correlation_id

        messages_count = len(messages[0]) if messages else 0

        # NEVER log messages content — only the count.
        log.info(
            "llm.call.start",
            correlation_id=correlation_id,
            tier=self._tier,
            model=self._model,
            caller=self._caller,
            messages_count=messages_count,
        )

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Fallback hook for non-chat LLM starts (completion-style APIs).

        on_chat_model_start takes priority for chat models; this fires for
        any provider that does not subclass BaseChatModel.
        """
        run_key = str(run_id)
        if run_key in self._start_times:
            # Already captured via on_chat_model_start — skip duplicate.
            return

        correlation_id = uuid.uuid4().hex
        self._start_times[run_key] = time.monotonic()
        self._correlation_ids[run_key] = correlation_id

        log.info(
            "llm.call.start",
            correlation_id=correlation_id,
            tier=self._tier,
            model=self._model,
            caller=self._caller,
            messages_count=len(prompts),
        )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Compute duration, extract token usage, emit llm.call.end event."""
        run_key = str(run_id)
        duration_ms = _elapsed_ms(self._start_times.pop(run_key, None))
        correlation_id = self._correlation_ids.pop(run_key, "unknown")

        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None

        # Provider-dependent shapes — handle both gracefully.
        # Shape A: llm_output["token_usage"] dict (OpenAI / older Anthropic).
        if response.llm_output and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage") or response.llm_output.get("usage")
            if isinstance(usage, dict):
                input_tokens = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
                output_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
                total_tokens = _safe_int(usage.get("total_tokens"))

        # Shape B: usage_metadata on the first AIMessage (LangChain v0.3+ Anthropic).
        if input_tokens is None and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    msg = getattr(gen, "message", None) or getattr(gen, "text", None)
                    metadata = getattr(msg, "usage_metadata", None)
                    if isinstance(metadata, dict):
                        input_tokens = _safe_int(
                            metadata.get("input_tokens") or metadata.get("prompt_tokens")
                        )
                        output_tokens = _safe_int(
                            metadata.get("output_tokens") or metadata.get("completion_tokens")
                        )
                        total_tokens = _safe_int(
                            metadata.get("total_tokens")
                            or (
                                (input_tokens or 0) + (output_tokens or 0)
                                if input_tokens is not None and output_tokens is not None
                                else None
                            )
                        )
                        break
                if input_tokens is not None:
                    break

        # Derive total from parts if provider omitted it.
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        metrics = CallMetrics(
            correlation_id=correlation_id,
            tier=self._tier,
            model=self._model,
            caller=self._caller,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        self.completed_calls.append(metrics)

        log.info(
            "llm.call.end",
            correlation_id=correlation_id,
            tier=self._tier,
            model=self._model,
            caller=self._caller,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Emit llm.call.error so partial/failed calls appear in Gravwell."""
        run_key = str(run_id)
        duration_ms = _elapsed_ms(self._start_times.pop(run_key, None))
        correlation_id = self._correlation_ids.pop(run_key, "unknown")

        log.warning(
            "llm.call.error",
            correlation_id=correlation_id,
            tier=self._tier,
            model=self._model,
            caller=self._caller,
            duration_ms=duration_ms,
            error_type=type(error).__name__,
        )

    def get_last_call_metrics(self) -> CallMetrics | None:
        """Return the most recent completed call's metrics, or None.

        Convenience accessor for the perf harness. Callers should prefer
        iterating handler.completed_calls for multi-call scenarios.
        """
        return self.completed_calls[-1] if self.completed_calls else None


# ------------------------------------------------------------------
# Internal helpers (private — not part of the public surface)
# ------------------------------------------------------------------

def _elapsed_ms(start: float | None) -> float:
    """Return elapsed wall-clock milliseconds since start, or 0.0."""
    if start is None:
        return 0.0
    return (time.monotonic() - start) * 1000.0


def _safe_int(value: Any) -> int | None:
    """Coerce a numeric-ish value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
