"""Tests for app/tools/reward_pool.py — per-peer descriptor vocabulary.

The stored vocabulary is an enhancement, never a precondition. Every test here
is about the degradation path: a database that is missing, unreachable, or
returning something unusable must cost the user nothing. The integration tests
cover the schema and the round trip against real Postgres.

Private data discipline: peer values are placeholders.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PEER = "<test-peer-pool-unit>"


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[Any]:
        return self._rows

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


@asynccontextmanager
async def _conn_yielding(rows: list[Any]) -> AsyncGenerator[MagicMock, None]:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=_FakeCursor(rows))
    yield conn


@pytest.fixture(autouse=True)
def _clear_seed_memo() -> Any:
    from app.tools import reward_pool

    reward_pool._seeded_peers.discard(_PEER)
    yield
    reward_pool._seeded_peers.discard(_PEER)


class TestSeeding:
    @pytest.mark.asyncio
    async def test_seed_failure_reports_false_rather_than_raising(self) -> None:
        """Seeding is an optimization; its failure must be recoverable."""
        from app.tools import reward_pool

        @asynccontextmanager
        async def crashing() -> AsyncGenerator[MagicMock, None]:
            raise RuntimeError("DB unavailable")
            yield MagicMock()  # pragma: no cover

        with patch("app.tools.db.get_db_conn", crashing):
            assert await reward_pool.ensure_seeded(_PEER) is False

        assert _PEER not in reward_pool._seeded_peers

    @pytest.mark.asyncio
    async def test_seeding_is_skipped_after_the_first_success(self) -> None:
        """The process memo keeps every later reward from re-issuing ~36 INSERTs."""
        from app.tools import reward_pool

        with patch("app.tools.db.get_db_conn", lambda: _conn_yielding([])) as _:
            assert await reward_pool.ensure_seeded(_PEER) is True

        def explode() -> Any:
            raise AssertionError("second call must not touch the database")

        with patch("app.tools.db.get_db_conn", explode):
            assert await reward_pool.ensure_seeded(_PEER) is True

    @pytest.mark.asyncio
    async def test_seed_rows_cover_every_axis_and_intensity(self) -> None:
        """The payload matches the seed constants and the schema's scoping rule."""
        from app.tools.reward_pool import _seed_rows
        from app.tools.rewards import _SEED_PALETTES, _SEED_STYLES, _SEED_THEMES

        rows = _seed_rows(_PEER)
        by_axis: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            by_axis.setdefault(row[2], []).append(row)

        assert {r[4] for r in by_axis["style"]} == set(_SEED_STYLES)
        assert {r[4] for r in by_axis["palette"]} == set(_SEED_PALETTES)
        assert all(r[3] is None for r in by_axis["style"] + by_axis["palette"])

        for intensity, themes in _SEED_THEMES.items():
            scoped = {r[4] for r in by_axis["theme"] if r[3] == intensity}
            assert scoped == set(themes)

        assert all(r[5] == "seed" for r in rows)
        assert len({r[0] for r in rows}) == len(rows), "ids must be unique"


class TestVocabularyLoading:
    @pytest.mark.asyncio
    async def test_unreachable_database_returns_none(self) -> None:
        """None is the signal to fall back to the seed constants."""
        from app.tools.reward_pool import load_vocabulary

        @asynccontextmanager
        async def crashing() -> AsyncGenerator[MagicMock, None]:
            raise RuntimeError("DB unavailable")
            yield MagicMock()  # pragma: no cover

        with patch("app.tools.db.get_db_conn", crashing):
            assert await load_vocabulary(_PEER, intensity="high") is None

    @pytest.mark.asyncio
    async def test_missing_axis_returns_none_rather_than_a_partial_vocabulary(
        self,
    ) -> None:
        """A partial vocabulary would silently narrow selection."""
        from app.tools.reward_pool import load_vocabulary

        rows = [
            {"axis": "theme", "value": "placeholder theme"},
            {"axis": "style", "value": "placeholder style"},
            # palette missing entirely
        ]
        with patch("app.tools.db.get_db_conn", lambda: _conn_yielding(rows)):
            assert await load_vocabulary(_PEER, intensity="high") is None

    @pytest.mark.asyncio
    async def test_unknown_axis_values_are_ignored(self) -> None:
        """A row with an unexpected axis cannot inject a fourth vocabulary."""
        from app.tools.reward_pool import load_vocabulary

        rows = [
            {"axis": "theme", "value": "placeholder theme"},
            {"axis": "style", "value": "placeholder style"},
            {"axis": "palette", "value": "placeholder palette"},
            {"axis": "unexpected", "value": "placeholder other"},
        ]
        with patch("app.tools.db.get_db_conn", lambda: _conn_yielding(rows)):
            vocabulary = await load_vocabulary(_PEER, intensity="high")

        assert vocabulary is not None
        assert set(vocabulary) == {"theme", "style", "palette"}


class TestSelectionFallback:
    def test_selection_uses_seed_constants_without_a_vocabulary(self) -> None:
        """No stored vocabulary is not an error state."""
        from app.tools.rewards import (
            _SEED_PALETTES,
            _SEED_STYLES,
            _SEED_THEMES,
            _select_theme,
        )

        selection = _select_theme(intensity="high", vocabulary=None)

        assert selection["theme_family"] in _SEED_THEMES["high"]
        assert selection["style"] in _SEED_STYLES
        assert selection["palette"] in _SEED_PALETTES

    def test_stored_vocabulary_replaces_the_seed_constants(self) -> None:
        """A peer whose vocabulary has drifted draws from the drifted one."""
        from app.tools.rewards import _select_theme

        vocabulary = {
            "theme": ["placeholder evolved theme"],
            "style": ["placeholder evolved style"],
            "palette": ["placeholder evolved palette"],
        }
        selection = _select_theme(intensity="high", vocabulary=vocabulary)

        assert selection == {
            "theme_family": "placeholder evolved theme",
            "style": "placeholder evolved style",
            "palette": "placeholder evolved palette",
        }

    def test_empty_axis_in_a_vocabulary_falls_back_to_seeds(self) -> None:
        """Defense in depth against a partial vocabulary reaching selection."""
        from app.tools.rewards import _SEED_STYLES, _select_theme

        vocabulary = {
            "theme": ["placeholder evolved theme"],
            "style": [],
            "palette": ["placeholder evolved palette"],
        }
        selection = _select_theme(intensity="high", vocabulary=vocabulary)

        assert selection["style"] in _SEED_STYLES

    def test_sensitive_selection_ignores_the_stored_vocabulary(self) -> None:
        """Stored descriptors must never reach the sensitive-task path."""
        from app.tools.rewards import _SENSITIVE_THEMES, _select_theme

        vocabulary = {
            "theme": ["placeholder evolved theme"],
            "style": ["placeholder evolved style"],
            "palette": ["placeholder evolved palette"],
        }
        for _ in range(50):
            selection = _select_theme(
                intensity="high", sensitive_task=True, vocabulary=vocabulary
            )
            assert selection["theme_family"] in {t["theme"] for t in _SENSITIVE_THEMES}
            assert selection["style"] != "placeholder evolved style"
            assert selection["palette"] != "placeholder evolved palette"


class TestRewardDeliveryIsNeverBlocked:
    @pytest.mark.asyncio
    async def test_vocabulary_failure_still_delivers_an_image(self) -> None:
        """A vocabulary lookup failure must not cost the user a reward."""
        import uuid

        from app.tools import rewards as rewards_module

        async def crashing_vocabulary(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("DB unavailable")

        image_mock = AsyncMock(
            return_value={
                "path": "/tmp/reward_artifacts/test-image.png",
                "theme_family": "test theme",
                "style": "test style",
                "palette": "test palette",
            }
        )

        with (
            patch("app.tools.reward_pool.load_vocabulary", new=crashing_vocabulary),
            patch.object(rewards_module, "load_reward_prefs", new=AsyncMock(return_value={})),
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(
                rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())
            ),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            result = await rewards_module.maybe_reward(
                peer=_PEER,
                task_title="Placeholder task title",
                notion_page_id="<page-id-pool-1>",
                streak=3,
                energy_required="High",
                time_estimate=45,
            )

        assert result["attachment_path"] == "/tmp/reward_artifacts/test-image.png"
        assert image_mock.await_args.kwargs["vocabulary"] is None

    @pytest.mark.asyncio
    async def test_usage_counter_failure_still_delivers_an_image(self) -> None:
        """Diagnostic counters must never fail an already-generated reward."""
        import uuid

        from app.tools import rewards as rewards_module

        vocabulary = {
            "theme": ["placeholder theme"],
            "style": ["placeholder style"],
            "palette": ["placeholder palette"],
        }

        async def crashing_record(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("DB unavailable")

        with (
            patch(
                "app.tools.reward_pool.load_vocabulary",
                new=AsyncMock(return_value=vocabulary),
            ),
            patch("app.tools.reward_pool.record_use", new=crashing_record),
            patch.object(rewards_module, "load_reward_prefs", new=AsyncMock(return_value={})),
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
            patch.object(
                rewards_module,
                "generate_reward_image",
                new=AsyncMock(
                    return_value={
                        "path": "/tmp/reward_artifacts/test-image.png",
                        "theme_family": "placeholder theme",
                        "style": "placeholder style",
                        "palette": "placeholder palette",
                    }
                ),
            ),
            patch.object(
                rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())
            ),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            result = await rewards_module.maybe_reward(
                peer=_PEER,
                task_title="Placeholder task title",
                notion_page_id="<page-id-pool-2>",
                streak=3,
                energy_required="High",
                time_estimate=45,
            )

        assert result["attachment_path"] == "/tmp/reward_artifacts/test-image.png"
