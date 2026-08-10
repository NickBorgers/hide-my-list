"""Judge LLM scoring for qualitative behavior contracts.

Used by the eval runner to score model-under-test responses against
rubrics that are too qualitative for regex (e.g., "response acknowledges
the system can send reminders without explaining internals").

Design notes:
- The judge is ALWAYS a stronger model than any model under test. Default
  is `claude-sonnet-5` per the test-rig spec (`docs/python-rewrite/test-rig.md`).
  Never use the model-under-test as its own judge.
- Responses are deterministic-by-cache: `sha256(rubric + response)` keys
  a JSON cache under `tests/evals/.cache/`. Re-running the same rubric
  against the same response does not re-bill.
- The judge returns a JSON object `{score: float in [0,1], reasoning: str}`.
  Strict pydantic parsing — a malformed judge response is an error, not a
  fallback to 0.5.
- The judge runs through the same LiteLLM proxy the production app uses,
  so what we validate matches what users see.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)

# The judge must be a stronger model than anything under test, and must be
# an alias the LiteLLM proxy actually serves — a judge the proxy rejects
# fails every `judge` and `shame_safe` contract as an error, taking the
# whole qualitative half of the suite down with it. Override via
# EVAL_JUDGE_MODEL when the proxy's alias set changes.
_DEFAULT_JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-sonnet-5")
_CACHE_DIR = Path(__file__).parent / ".cache"
_JUDGE_PROMPT_TEMPLATE = """\
You are an evaluation judge. Score how well the response below satisfies the rubric.

Return ONLY a JSON object on a single line with this exact shape:
{{"score": <float between 0.0 and 1.0>, "reasoning": "<one-sentence explanation>"}}

A score of 1.0 means the response perfectly satisfies the rubric.
A score of 0.0 means the response completely fails to satisfy the rubric.
Intermediate scores are allowed.

Rubric:
{rubric}

Response to score:
---
{response}
---

Return the JSON object now. No prose before or after.
"""


@dataclass
class JudgeResult:
    """The result of one judge invocation."""

    score: float
    reasoning: str
    from_cache: bool


class _JudgeJSON(BaseModel):
    """Schema for the judge's JSON output. Strict parsing."""

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=500)


def _cache_key(rubric: str, response: str, model: str) -> str:
    """Stable cache key. Model included so a different judge produces a different key."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(rubric.encode("utf-8"))
    h.update(b"\x00")
    h.update(response.encode("utf-8"))
    return h.hexdigest()


def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{key}.json"


def _load_cached(key: str) -> JudgeResult | None:
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return JudgeResult(
            score=float(data["score"]),
            reasoning=str(data["reasoning"]),
            from_cache=True,
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        # Treat corrupted cache as a miss; re-run.
        return None


def _save_cached(key: str, result: JudgeResult) -> None:
    payload = {"score": result.score, "reasoning": result.reasoning}
    _cache_path(key).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _call_judge_llm(
    *,
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 60.0,
) -> str:
    """Single-shot LLM call via langchain-openai.

    Points at the same LiteLLM proxy (`LLM_PROXY_BASE_URL`) as the production
    app. Keeping judge outbound HTTP inside LangChain preserves the
    constrained-tool-surface invariant from `docs/python-rewrite/test-rig.md`.
    """
    chat = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        # Thinking tokens count against max_tokens on the proxy path. A tight
        # cap starves the visible output: the judge returns empty or
        # mid-sentence-truncated JSON, which strict parsing then reports as a
        # contract error — indistinguishable in the report from a real failure.
        max_tokens=2000,
        timeout=timeout_seconds,
    )
    response = chat.invoke([HumanMessage(content=prompt)])
    # AIMessage.content is a string (or list of blocks for tool-calling models).
    content = response.content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", "")).strip()
            if isinstance(block, str):
                return block.strip()
        raise RuntimeError(f"Judge returned no text block: {content!r}")
    return str(content).strip()


def score(
    *,
    rubric: str,
    response: str,
    model: str = _DEFAULT_JUDGE_MODEL,
    base_url: str | None = None,
    api_key: str | None = None,
) -> JudgeResult:
    """Score `response` against `rubric` using the judge LLM.

    Cached by `sha256(model || rubric || response)` to avoid re-billing
    deterministic reruns.

    Args:
        rubric: The qualitative criterion the response must satisfy.
        response: The model-under-test's output to score.
        model: Judge model alias (must resolve via LiteLLM proxy).
            Default `claude-sonnet-5`.
        base_url: LiteLLM proxy URL. Defaults to env `LLM_PROXY_BASE_URL`.
            If neither is set, raises RuntimeError.
        api_key: LiteLLM proxy API key. Defaults to env `LLM_PROXY_API_KEY`.
            If neither is set, raises RuntimeError.

    Returns:
        JudgeResult with `score in [0.0, 1.0]`, a one-sentence reasoning,
        and `from_cache=True` if the cached value was used.
    """
    if not rubric:
        raise ValueError("rubric must be non-empty")
    if not response:
        raise ValueError("response must be non-empty")

    key = _cache_key(rubric, response, model)
    cached = _load_cached(key)
    if cached is not None:
        log.debug("judge.cache_hit", extra={"key": key[:12]})
        return cached

    effective_base_url = base_url or os.environ.get("LLM_PROXY_BASE_URL")
    effective_api_key = api_key or os.environ.get("LLM_PROXY_API_KEY")
    if not effective_base_url:
        raise RuntimeError(
            "Judge requires LLM_PROXY_BASE_URL (LiteLLM proxy endpoint) — "
            "pass base_url= or set the env var."
        )
    if not effective_api_key:
        raise RuntimeError(
            "Judge requires LLM_PROXY_API_KEY — "
            "pass api_key= or set the env var."
        )

    prompt = _JUDGE_PROMPT_TEMPLATE.format(rubric=rubric, response=response)
    raw = _call_judge_llm(
        prompt=prompt,
        model=model,
        base_url=effective_base_url,
        api_key=effective_api_key,
    )

    # Strip code fences if the judge wrapped its JSON.
    cleaned = raw.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].strip()

    try:
        parsed_dict = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Judge response is not valid JSON. Raw: {raw!r}"
        ) from exc

    try:
        parsed = _JudgeJSON(**parsed_dict)
    except ValidationError as exc:
        raise RuntimeError(
            f"Judge JSON fails schema. Raw: {raw!r}. Errors: {exc}"
        ) from exc

    result = JudgeResult(
        score=parsed.score,
        reasoning=parsed.reasoning,
        from_cache=False,
    )
    _save_cached(key, result)
    return result


__all__ = ["JudgeResult", "score"]
