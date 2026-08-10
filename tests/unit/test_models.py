"""Tests for app/models.py — LLM provider factory and LangSmith guard."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def test_model_tiers_json_exists() -> None:
    """setup/model-tiers.json must exist and contain all required tiers."""
    tiers_path = Path(__file__).parent.parent.parent / "setup" / "model-tiers.json"
    assert tiers_path.is_file(), f"model-tiers.json not found at {tiers_path}"

    import json
    data = json.loads(tiers_path.read_text(encoding="utf-8"))

    required_tiers = {"expensive", "medium", "cheap", "reminder"}
    missing = required_tiers - set(data.keys())
    assert not missing, f"model-tiers.json missing tiers: {sorted(missing)}"


def test_all_tiers_have_known_model_id_prefix() -> None:
    """All model IDs in model-tiers.json must use a known provider prefix.

    The allowlist mirrors app.models._VALID_MODEL_PREFIXES — the proxy is the
    source of truth for which aliases actually resolve; this check only catches
    typos at startup. Importing the constant keeps the two in lockstep.
    """
    import json

    from app.models import _VALID_MODEL_PREFIXES
    tiers_path = Path(__file__).parent.parent.parent / "setup" / "model-tiers.json"
    data = json.loads(tiers_path.read_text(encoding="utf-8"))

    for tier, model_id in data.items():
        if tier in {"expensive", "medium", "cheap", "reminder"}:
            assert any(model_id.startswith(p) for p in _VALID_MODEL_PREFIXES), (
                f"Tier '{tier}' model '{model_id}' must start with one of "
                f"{_VALID_MODEL_PREFIXES}"
            )


def test_langsmith_guard_fires_when_tracing_without_export_flag() -> None:
    """LangSmith guard must refuse startup when LANGSMITH_TRACING=true without export flag."""
    # Reset the lru_cache so the guard runs fresh
    from app import models as models_module
    from app.models import _load_model_tiers  # noqa: F401
    models_module._load_model_tiers.cache_clear()

    with patch.dict(
        os.environ,
        {"LANGSMITH_TRACING": "true"},
        clear=False,
    ):
        # Ensure ALLOW_PRIVATE_TRACE_EXPORT is not set
        env = dict(os.environ)
        env.pop("ALLOW_PRIVATE_TRACE_EXPORT", None)

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="ALLOW_PRIVATE_TRACE_EXPORT"):
                models_module._load_model_tiers.cache_clear()
                models_module._load_model_tiers()

    # Reset cache after test
    models_module._load_model_tiers.cache_clear()


def test_langsmith_guard_passes_when_export_explicitly_allowed() -> None:
    """LangSmith guard must pass when ALLOW_PRIVATE_TRACE_EXPORT=true."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    with patch.dict(
        os.environ,
        {
            "LANGSMITH_TRACING": "true",
            "ALLOW_PRIVATE_TRACE_EXPORT": "true",
        },
        clear=False,
    ):
        # Should not raise
        result = models_module._load_model_tiers()
        assert "expensive" in result

    models_module._load_model_tiers.cache_clear()


def test_langsmith_guard_passes_when_tracing_disabled() -> None:
    """LangSmith guard must not fire when LANGSMITH_TRACING is not set."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.pop("ALLOW_PRIVATE_TRACE_EXPORT", None)

    with patch.dict(os.environ, env, clear=True):
        result = models_module._load_model_tiers()
        assert "expensive" in result
        assert "reminder" in result

    models_module._load_model_tiers.cache_clear()


def test_valid_tier_returns_llm_instance() -> None:
    """llm(tier) must return a runnable wrapping ChatOpenAI for valid tiers.

    The returned object is a RunnableBinding (from with_config) that wraps a
    ChatOpenAI instance with the observability callback attached. We verify
    that the inner bound model is a ChatOpenAI.
    """
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        from langchain_core.runnables import RunnableBinding
        from langchain_openai import ChatOpenAI
        result = models_module.llm("medium")
        # with_config wraps the model in a RunnableBinding
        assert isinstance(result, RunnableBinding)
        assert isinstance(result.bound, ChatOpenAI)

    models_module._load_model_tiers.cache_clear()


def test_llm_constructs_chatopenai_with_expected_kwargs() -> None:
    """llm() must pass tier model id, temperature, max_tokens, base_url, and api_key to ChatOpenAI."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    tiers_path = Path(__file__).parent.parent.parent / "setup" / "model-tiers.json"
    expected_model = json.loads(tiers_path.read_text(encoding="utf-8"))["medium"]

    fake_instance = MagicMock()
    fake_instance.with_config.return_value = fake_instance

    env = {k: v for k, v in os.environ.items() if k not in ("LANGSMITH_TRACING",)}
    env["LLM_PROXY_BASE_URL"] = "https://proxy.test/v1"
    env["LLM_PROXY_API_KEY"] = "test-key"

    with patch.dict(os.environ, env, clear=True):
        with patch("app.models.ChatOpenAI", return_value=fake_instance) as mock_cls:
            models_module.llm("medium", temperature=0.0, caller="test")
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs["model"] == expected_model
            assert call_kwargs["temperature"] == 0.0
            # medium is a reasoning tier: it must NOT carry an output-token cap.
            # A cap (formerly hardcoded 1024) truncates intake's think+JSON and
            # the truncated output silently falls back to a non-reminder task.
            assert "max_tokens" not in call_kwargs
            assert call_kwargs["base_url"] == "https://proxy.test/v1"
            assert call_kwargs["api_key"] == "test-key"

    models_module._load_model_tiers.cache_clear()


def test_max_tokens_per_tier() -> None:
    """Reasoning tiers send NO max_tokens; only the label-only cheap tier is capped.

    The output-token cap was a Claude-era default that truncated gemma4-small's
    think+structured-JSON intake output. Reasoning tiers (medium/expensive/reminder)
    must let the model finish; cheap (intent classifier) only emits a label and
    keeps a small cap.
    """
    from unittest.mock import MagicMock, patch

    from app import models as models_module

    fake_instance = MagicMock()
    fake_instance.with_config.return_value = fake_instance

    env = {k: v for k, v in os.environ.items() if k not in ("LANGSMITH_TRACING",)}
    env["LLM_PROXY_BASE_URL"] = "https://proxy.test/v1"
    env["LLM_PROXY_API_KEY"] = "test-key"

    with patch.dict(os.environ, env, clear=True):
        for tier in ("medium", "expensive", "reminder"):
            models_module._load_model_tiers.cache_clear()
            with patch("app.models.ChatOpenAI", return_value=fake_instance) as mock_cls:
                models_module.llm(tier, caller="test")
                assert "max_tokens" not in mock_cls.call_args.kwargs, (
                    f"{tier} tier must not cap output tokens; "
                    f"got {mock_cls.call_args.kwargs.get('max_tokens')!r}"
                )

        models_module._load_model_tiers.cache_clear()
        with patch("app.models.ChatOpenAI", return_value=fake_instance) as mock_cls:
            models_module.llm("cheap", caller="test")
            assert mock_cls.call_args.kwargs.get("max_tokens") == 1024, (
                "cheap tier (label-only classifier) keeps a small output cap"
            )

    models_module._load_model_tiers.cache_clear()


def test_llm_raises_when_llm_proxy_base_url_missing() -> None:
    """llm() must raise RuntimeError when LLM_PROXY_BASE_URL is unset."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = {
        k: v for k, v in os.environ.items()
        if k not in ("LANGSMITH_TRACING", "LLM_PROXY_BASE_URL")
    }
    env.pop("LLM_PROXY_BASE_URL", None)
    env["LLM_PROXY_API_KEY"] = "test-key"

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match="LLM_PROXY_BASE_URL"):
            models_module.llm("medium")

    models_module._load_model_tiers.cache_clear()


def test_llm_raises_when_llm_proxy_api_key_missing() -> None:
    """llm() must raise RuntimeError when LLM_PROXY_API_KEY is unset."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = {
        k: v for k, v in os.environ.items()
        if k not in ("LANGSMITH_TRACING", "LLM_PROXY_API_KEY")
    }
    env.pop("LLM_PROXY_API_KEY", None)
    env["LLM_PROXY_BASE_URL"] = "https://proxy.test/v1"

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match="LLM_PROXY_API_KEY"):
            models_module.llm("medium")

    models_module._load_model_tiers.cache_clear()


def test_cheap_tier_sets_think_false_extra_body() -> None:
    """llm('cheap') must construct ChatOpenAI with extra_body={'think': False}.

    The proxy forwards `think` to Ollama; cheap is the label-only classifier
    path where reasoning is wasted overhead (significant output-token
    reduction measured). Other tiers must NOT set think=false because their callers
    (chat, rejection, breakdown coaching, selection) rely on reasoning for
    shame-safe phrasing and scoring nuance.
    """
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        cheap = models_module.llm("cheap").bound  # unwrap RunnableBinding
        assert getattr(cheap, "extra_body", None) == {"think": False}, (
            f"cheap tier must send think=false; got extra_body={getattr(cheap, 'extra_body', None)!r}"
        )

        for tier in ("medium", "expensive", "reminder"):
            other = models_module.llm(tier).bound
            extra = getattr(other, "extra_body", None) or {}
            assert "think" not in extra, (
                f"{tier} tier must NOT set think (defaults to thinking=on); "
                f"got extra_body={extra!r}"
            )

    models_module._load_model_tiers.cache_clear()


def test_invalid_tier_raises_value_error() -> None:
    """llm() with unknown tier must raise ValueError."""
    from app import models as models_module
    models_module._load_model_tiers.cache_clear()

    env = dict(os.environ)
    env.pop("LANGSMITH_TRACING", None)
    env.setdefault("LLM_PROXY_API_KEY", "test-key-not-used")
    env.setdefault("LLM_PROXY_BASE_URL", "https://proxy.test/v1")

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="Unknown model tier"):
            models_module.llm("nonexistent_tier")  # type: ignore[arg-type]

    models_module._load_model_tiers.cache_clear()
