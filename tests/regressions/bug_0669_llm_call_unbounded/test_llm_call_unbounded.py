"""Regression: LLM calls must carry an explicit timeout and retry cap.

See README.md. Without both, the OpenAI SDK defaults applied and a wedged model
host cost 1800s per call (3 attempts x the gateway's 600s), stalling every
conversation queued behind it on a backend that serves one request at a time.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# The reverse proxy in front of the LiteLLM proxy returns 504 at this point. The
# app must give up first so the failure is ours to classify.
GATEWAY_TIMEOUT_SECONDS = 600.0

TIERS = ("cheap", "medium", "expensive", "reminder")


def _proxy_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "LANGSMITH_TRACING"}
    env["LLM_PROXY_BASE_URL"] = "https://proxy.test/v1"
    env["LLM_PROXY_API_KEY"] = "test-key"
    env.pop("LLM_REQUEST_TIMEOUT_SECONDS", None)
    env.pop("LLM_MAX_RETRIES", None)
    env.update(overrides)
    return env


def _construct(tier: str, env: dict[str, str]) -> dict[str, object]:
    """Return the kwargs llm(tier) passes to ChatOpenAI under `env`."""
    from app import models as models_module

    fake_instance = MagicMock()
    fake_instance.with_config.return_value = fake_instance

    models_module._load_model_tiers.cache_clear()
    with patch.dict(os.environ, env, clear=True):
        with patch("app.models.ChatOpenAI", return_value=fake_instance) as mock_cls:
            models_module.llm(tier, caller="regression")
            kwargs: dict[str, object] = dict(mock_cls.call_args.kwargs)
    models_module._load_model_tiers.cache_clear()
    return kwargs


@pytest.mark.parametrize("tier", TIERS)
def test_every_tier_is_bounded(tier: str) -> None:
    """No tier may fall back to the SDK's own timeout and retry defaults."""
    from app import models as models_module

    kwargs = _construct(tier, _proxy_env())

    assert kwargs.get("timeout") == models_module._DEFAULT_REQUEST_TIMEOUT_SECONDS, (
        f"{tier} tier must pass an explicit timeout; an unbounded call is how one "
        f"wedged model host stalled the app for 90 minutes"
    )
    assert kwargs.get("max_retries") == models_module._DEFAULT_MAX_RETRIES, (
        f"{tier} tier must cap retries; the SDK default of 2 tripled the ceiling"
    )


def test_worst_case_stays_under_the_gateway_timeout() -> None:
    """Attempts x timeout must land below 600s so the app fails on its own clock.

    At the SDK defaults this product was 1800s and the app only ever saw an
    opaque 504 from the gateway, 30 minutes after the request went out.
    """
    from app import models as models_module

    attempts = models_module._DEFAULT_MAX_RETRIES + 1
    worst_case = attempts * models_module._DEFAULT_REQUEST_TIMEOUT_SECONDS

    assert worst_case < GATEWAY_TIMEOUT_SECONDS, (
        f"worst case {worst_case}s ({attempts} attempts x "
        f"{models_module._DEFAULT_REQUEST_TIMEOUT_SECONDS}s) must stay under the "
        f"{GATEWAY_TIMEOUT_SECONDS}s gateway timeout"
    )


def test_overrides_are_honoured() -> None:
    """Operators can retune the ceiling without a code change."""
    kwargs = _construct(
        "medium",
        _proxy_env(LLM_REQUEST_TIMEOUT_SECONDS="45.5", LLM_MAX_RETRIES="0"),
    )

    assert kwargs.get("timeout") == 45.5
    assert kwargs.get("max_retries") == 0


@pytest.mark.parametrize("bad_value", ["not-a-number", "0", "-1", "", "inf", "Infinity", "nan", "1e309"])
def test_unusable_timeout_override_falls_back_to_default(bad_value: str) -> None:
    """A malformed, non-positive, or non-finite override must not reintroduce an unbounded call."""
    from app import models as models_module

    kwargs = _construct("medium", _proxy_env(LLM_REQUEST_TIMEOUT_SECONDS=bad_value))

    assert kwargs.get("timeout") == models_module._DEFAULT_REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("bad_value", ["not-a-number", "-1", ""])
def test_unusable_retry_override_falls_back_to_default(bad_value: str) -> None:
    """A malformed or negative retry override must not restore the SDK default."""
    from app import models as models_module

    kwargs = _construct("medium", _proxy_env(LLM_MAX_RETRIES=bad_value))

    assert kwargs.get("max_retries") == models_module._DEFAULT_MAX_RETRIES
