"""Regression: the celebration image must be about the task that earned it.

Before PR #652 the image prompt was a pure function of intensity, a random
theme draw, streak count, and user prefs. Nothing about the completed task
reached it, so the picture was unrelated to the accomplishment by construction.

Private data discipline: placeholder task titles only. The point of the fix is
that the title never reaches the image provider — only a fixed-vocabulary motif
label does — so these tests assert on the motif, never on title text.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools import rewards as rewards_module
from app.tools.rewards import (
    _MOTIF_THEME_AFFINITY,
    _MOTIFS,
    _SEED_THEMES,
    _build_image_prompt,
    classify_task_motif,
)


def _themes_for(intensity: str, motif: str) -> set[str]:
    """Seed themes at `intensity` that the motif favors."""
    return _MOTIF_THEME_AFFINITY[motif] & set(_SEED_THEMES[intensity])


@pytest.mark.asyncio
async def test_classified_motif_reaches_the_prompt() -> None:
    """The load-bearing assertion: task classification changes the prompt."""
    mock_llm = MagicMock()
    mock_llm.return_value.ainvoke = AsyncMock(return_value=MagicMock(content="errand"))

    with patch("app.models.llm", mock_llm):
        motif = await classify_task_motif("Placeholder store run")

    assert motif == "errand"

    prompt = _build_image_prompt(
        intensity="high",
        streak_count=1,
        task_descriptions=["Placeholder store run"],
        motif=motif,
    )
    assert _MOTIFS["errand"] in prompt, (
        "the classified motif must reach the image prompt — otherwise the "
        "generated image has no relationship to the completed task"
    )


def test_motif_steers_which_scene_is_drawn() -> None:
    """A motif line on a randomly-picked scene would still be the wrong picture.

    Selection must favor theme descriptors that suit the motif, so the whole
    composition — not just one appended sentence — reflects what the user
    finished.
    """
    captured: list[dict[str, float]] = []

    def fake_choices(population: list[str], weights: list[float], k: int = 1) -> list[str]:
        captured.append(dict(zip(population, weights, strict=True)))
        return [population[0]]

    with patch.object(rewards_module.random, "choices", fake_choices):
        rewards_module._select_theme(intensity="high", motif="errand")

    # Theme is the first of the three independent axis draws.
    theme_weights = captured[0]
    suited = _themes_for("high", "errand")
    unsuited_best = max(w for theme, w in theme_weights.items() if theme not in suited)
    assert all(theme_weights[theme] > unsuited_best for theme in suited)
    assert all(w > 0 for w in theme_weights.values()), "no scene may be excluded outright"


@pytest.mark.asyncio
async def test_task_text_still_never_reaches_the_image_prompt() -> None:
    """Relevance must not have been bought with the task title.

    Text inference runs on the local proxy; image generation calls OpenAI. The
    motif label is the only task-derived value that may cross that boundary.
    """
    sentinel = "zzsentineltaskzz"
    mock_llm = MagicMock()
    mock_llm.return_value.ainvoke = AsyncMock(return_value=MagicMock(content="errand"))

    with patch("app.models.llm", mock_llm):
        motif = await classify_task_motif(sentinel)

    prompt = _build_image_prompt(
        intensity="high",
        streak_count=1,
        task_descriptions=[sentinel],
        motif=motif,
    )
    assert sentinel not in prompt


@pytest.mark.asyncio
async def test_maybe_reward_classifies_before_generating() -> None:
    """End to end through the production entry point."""
    import uuid

    recorded: dict[str, Any] = {}

    async def fake_generate(**kwargs: Any) -> dict[str, Any]:
        recorded.update(kwargs)
        return {
            "image": {
                "path": "/tmp/reward_artifacts/placeholder.png",
                "theme_family": "eagle soaring above mountain range at dawn",
                "style": "realistic",
                "palette": "sunrise red",
            },
            "failure_reason": None,
        }

    mock_llm = MagicMock()
    mock_llm.return_value.ainvoke = AsyncMock(return_value=MagicMock(content="errand"))

    with (
        patch("app.models.llm", mock_llm),
        # Classification only runs when an image is actually possible.
        patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
        patch.object(rewards_module, "load_reward_prefs", new=AsyncMock(return_value={})),
        patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
        patch.object(rewards_module, "generate_reward_image", new=fake_generate),
        patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
        patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
    ):
        await rewards_module.maybe_reward(
            peer="<test-peer-relevance>",
            task_title="Placeholder store run",
            notion_page_id="<page-id-relevance>",
            streak=1,
            energy_required="High",
        )

    assert recorded["motif"] == "errand"
