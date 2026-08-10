"""Regression: a blank task title must not suppress the reward image.

See README.md. The pre-fix code filtered blank task_descriptions, found the
list empty, and returned None — degrading the reward to the text fallback —
even though the image prompt never embeds task text at all.

Standalone run:
    pytest tests/regressions/bug_0632_reward_image_blank_title -v
"""
from __future__ import annotations

import base64
import os
import tempfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools.rewards import _build_image_prompt, generate_reward_image


def _mock_openai_client() -> tuple[MagicMock, AsyncMock]:
    """Return (client, generate_mock) whose generate() yields one b64 image."""
    fake_image = MagicMock()
    fake_image.b64_json = base64.b64encode(b"fake-image-bytes").decode()
    fake_response = MagicMock()
    fake_response.data = [fake_image]

    generate_mock = AsyncMock(return_value=fake_response)
    client = MagicMock()
    client.images = MagicMock()
    client.images.generate = generate_mock
    return client, generate_mock


async def _generate(descriptions: list[str]) -> tuple[Any, AsyncMock]:
    """Run generate_reward_image with a mocked OpenAI client."""
    client, generate_mock = _mock_openai_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(
            os.environ, {"OPENAI_API_KEY": "test-key", "REWARD_ARTIFACTS_DIR": tmpdir}
        ):
            with patch("openai.AsyncOpenAI", MagicMock(return_value=client)):
                result = await generate_reward_image(
                    intensity="high",
                    streak_count=len(descriptions),
                    task_descriptions=descriptions,
                )
    return result, generate_mock


@pytest.mark.asyncio
async def test_blank_description_still_calls_images_generate() -> None:
    """The load-bearing assertion: a blank title still produces an image."""
    result, generate_mock = await _generate([""])

    generate_mock.assert_awaited_once()
    assert result["image"] is not None, (
        "A blank task title must not suppress image generation — the image "
        "prompt never embeds task text, so there is nothing to guard against."
    )
    assert result["failure_reason"] is None
    assert result["image"]["path"], "a successful generation must return an artifact path"


@pytest.mark.asyncio
async def test_all_blank_descriptions_still_generate() -> None:
    """Whitespace-only titles count as blank to the old filter — same outcome."""
    result, generate_mock = await _generate(["   ", "\n", ""])

    generate_mock.assert_awaited_once()
    assert result["image"] is not None


def test_prompt_never_contains_task_text() -> None:
    """Pins why tolerating blank descriptions is safe.

    If a future change starts embedding task descriptions in the image prompt,
    this fails — and the tolerance above must be reconsidered, because a blank
    description would then actually degrade the prompt.
    """
    sentinel = "zzsentineltaskzz"
    prompt = _build_image_prompt(
        intensity="high",
        streak_count=1,
        task_descriptions=[sentinel],
    )

    assert sentinel not in prompt, (
        "task_descriptions leaked into the image prompt. This violates the "
        "private-data discipline in docs/reward-system.md and invalidates the "
        "blank-description tolerance in generate_reward_image()."
    )


@pytest.mark.parametrize(
    "active_task",
    [
        pytest.param(
            {
                "page_id": "<page_id>",
                "title": "",
                "status": "In Progress",
                "selected_at": datetime.now(UTC).isoformat(),
            },
            id="blank-title",
        ),
        pytest.param(
            {
                "page_id": "<page_id>",
                "status": "In Progress",
                "selected_at": datetime.now(UTC).isoformat(),
            },
            id="missing-title",
        ),
    ],
)
@pytest.mark.asyncio
async def test_complete_node_does_not_fabricate_a_title(
    active_task: dict[str, Any],
) -> None:
    """complete_node must forward an unnamed task as unnamed, not as a placeholder.

    task_title is written to the private reward_manifests column. The old
    `.get("title", "task")` substituted the literal string "task" whenever the
    key was absent, storing a fabricated value indistinguishable from a real
    title — and, being non-blank, one that would also have masked this bug.
    """
    from app.graph.nodes.complete import complete_node

    maybe_reward_mock = AsyncMock(
        return_value={"text": "Nice work!", "attachment_path": None}
    )

    state = {"peer": "+10000000000", "active_task": active_task, "streak": 1}

    with patch("app.tools.rewards.maybe_reward", maybe_reward_mock):
        with patch("app.tools.notion.update_status", new_callable=AsyncMock):
            await complete_node(state)  # type: ignore[arg-type]

    maybe_reward_mock.assert_awaited_once()
    assert maybe_reward_mock.await_args.kwargs["task_title"] == "", (
        "complete_node fabricated a task title for a task that has none; the "
        "value is stored in the private manifest column as if it were real."
    )


@pytest.mark.asyncio
async def test_intake_blank_title_does_not_create_a_page() -> None:
    """A save the model could not name must ask, not write a nameless page."""
    from app.graph.nodes import intake as intake_module

    create_task_mock = AsyncMock(return_value={"id": "<page_id>"})

    response = MagicMock()
    response.content = '{"action": "save", "title": "   "}'
    model_mock = MagicMock()
    model_mock.ainvoke = AsyncMock(return_value=response)

    state = {"peer": "+10000000000", "incoming": "   "}

    with patch("app.tools.notion.create_task", create_task_mock):
        with patch("app.models.llm", MagicMock(return_value=model_mock)):
            result = await intake_module.intake_node(state)  # type: ignore[arg-type]

    create_task_mock.assert_not_awaited()
    drafts = result["pending_outbound"]
    assert drafts and drafts[0]["notion_page_id"] is None
    assert drafts[0]["body"], "the user must get a reply asking for a name"
