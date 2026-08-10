"""Integration tests for migration 0012: reward_manifests motif columns.

Verifies against a real Postgres that:

  - write_reward_manifest persists `motif` and `image_failure_reason`.
  - Both survive the round trip, including on rows where image generation
    failed — that is the row an operator reads when asking why a completion
    delivered text instead of a picture.
  - Omitting them stores NULL, so pre-0012 rows and emoji-only rewards read
    back the same way.

The unit tests mock the connection, so they can only prove the call site passes
the values. This proves the columns exist and the INSERT column list matches the
migration — the failure mode a mocked connection cannot see.

Tests require a live DATABASE_URL. Skipped otherwise.

Private data discipline: all identifiers and content use placeholders; no real
page IDs, recipients, or task titles.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL", ""))
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="DATABASE_URL not set; skipping integration tests",
)

_PEER = "<test-peer-manifest-motif>"


@pytest.fixture()
async def clean_manifests() -> Any:
    """Apply migrations and clear reward_manifests for this test's peer."""
    import psycopg

    conn_str = os.environ["DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(conn_str, autocommit=False) as conn:
        from app.tools.db import _MIGRATIONS_DIR

        for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(mig.read_text())  # type: ignore[arg-type]
        await conn.commit()

        await conn.execute("DELETE FROM reward_manifests WHERE peer = %s", (_PEER,))
        await conn.commit()
        yield conn

        await conn.execute("DELETE FROM reward_manifests WHERE peer = %s", (_PEER,))
        await conn.commit()


async def _deliver(
    *,
    motif: str | None,
    image_failure_reason: str | None,
    reward_kind: str,
    artifact_path: str | None,
) -> uuid.UUID | None:
    """Write one manifest row through the production write path."""
    from app.tools.rewards import write_reward_manifest

    return await write_reward_manifest(
        peer=_PEER,
        notion_page_id=f"<page-{uuid.uuid4()}>",
        task_title="Placeholder task title",
        reward_kind=reward_kind,
        intensity="high",
        streak_count=3,
        delivered_at=datetime.now(UTC),
        artifact_path=artifact_path,
        sensitive_task=False,
        motif=motif,
        image_failure_reason=image_failure_reason,
    )


async def _read_back(conn: Any, manifest_id: uuid.UUID) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT motif, image_failure_reason, streak_count FROM reward_manifests WHERE id = %s",
        (str(manifest_id),),
    )
    row = await cur.fetchone()
    assert row is not None, "manifest row must exist"
    return {"motif": row[0], "image_failure_reason": row[1], "streak_count": row[2]}


@pytest.mark.asyncio
async def test_motif_round_trips_on_a_delivered_image(clean_manifests: Any) -> None:
    """The motif is how image relevance stays auditable without the PNG.

    Generated images live in a Docker volume with no host bind mount, so the
    manifest row is the only place an operator can check whether the picture
    suited the task.
    """
    manifest_id = await _deliver(
        motif="errand",
        image_failure_reason=None,
        reward_kind="emoji+image",
        artifact_path="/data/reward_artifacts/placeholder.png",
    )
    assert manifest_id is not None, "manifest insert must succeed against the real schema"

    stored = await _read_back(clean_manifests, manifest_id)
    assert stored["motif"] == "errand"
    assert stored["image_failure_reason"] is None
    assert stored["streak_count"] == 3


@pytest.mark.asyncio
async def test_failure_reason_round_trips_on_a_fallback(clean_manifests: Any) -> None:
    """A text-only delivery records why, so it stays explainable after logs expire."""
    manifest_id = await _deliver(
        motif="repair",
        image_failure_reason="empty_response",
        reward_kind="image_fallback",
        artifact_path=None,
    )
    assert manifest_id is not None

    stored = await _read_back(clean_manifests, manifest_id)
    assert stored["image_failure_reason"] == "empty_response"
    # The motif describes the task, not the image, so it survives the failure.
    assert stored["motif"] == "repair"


@pytest.mark.asyncio
async def test_classify_task_motif_reachable_from_maybe_reward(clean_manifests: Any) -> None:
    """classify_task_motif is reachable end-to-end from maybe_reward.

    Verifies the full wiring: motif classification feeds generate_reward_image
    and the same motif is persisted on the manifest row. Uses a real Postgres
    manifest write; mocks only the LLM and image provider so no external calls
    are made.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.tools.rewards import maybe_reward

    # Fake image attempt that looks like a successful generation.
    fake_image = {
        "path": "/data/reward_artifacts/placeholder.png",
        "theme_family": "fox dancing in wildflowers",
        "style": "watercolor",
        "palette": "spring green",
    }
    fake_attempt = {"image": fake_image, "failure_reason": None}

    # Stub LLM: ainvoke returns "errand" so classify_task_motif produces a label.
    fake_response = MagicMock()
    fake_response.content = "errand"
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(return_value=fake_response)
    fake_llm = MagicMock(return_value=fake_model)

    image_mock = AsyncMock(return_value=fake_attempt)

    with (
        patch("app.tools.rewards.asyncio.wait_for", new=AsyncMock(return_value=fake_response)),
        patch("app.models.is_local_tier", return_value=True),
        patch("app.models.llm", fake_llm),
        patch("app.tools.rewards.generate_reward_image", image_mock),
        patch("app.tools.rewards.load_reward_prefs", AsyncMock(return_value={})),
        patch("app.tools.rewards.load_feedback_history", AsyncMock(return_value=[])),
        patch("app.tools.rewards.load_vocabulary", AsyncMock(return_value=None), create=True),
        patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
    ):
        await maybe_reward(
            peer=_PEER,
            task_title="Placeholder store run",
            notion_page_id=f"<page-{uuid.uuid4()}>",
            streak=2,
            energy_required="High",
        )

    # The motif the classifier returned must reach generate_reward_image.
    image_mock.assert_called_once()
    assert image_mock.await_args.kwargs["motif"] == "errand"

    # The same motif must be persisted on the manifest row.
    cur = await clean_manifests.execute(
        "SELECT motif FROM reward_manifests WHERE peer = %s ORDER BY delivered_at DESC LIMIT 1",
        (_PEER,),
    )
    row = await cur.fetchone()
    assert row is not None, "manifest row must have been written"
    assert row[0] == "errand"


@pytest.mark.asyncio
async def test_omitted_columns_store_null(clean_manifests: Any) -> None:
    """Emoji-only rewards and pre-0012 rows read back identically."""
    from app.tools.rewards import write_reward_manifest

    manifest_id = await write_reward_manifest(
        peer=_PEER,
        notion_page_id=f"<page-{uuid.uuid4()}>",
        task_title="Placeholder task title",
        reward_kind="emoji",
        intensity="low",
        streak_count=1,
        delivered_at=datetime.now(UTC),
        sensitive_task=True,
    )
    assert manifest_id is not None

    stored = await _read_back(clean_manifests, manifest_id)
    assert stored["motif"] is None
    assert stored["image_failure_reason"] is None
