"""Tests for app/tools/rewards.py — reward subsystem.

Coverage:
- PR-B5-T1: Sensitive-task suppression (muted emoji, no image)
- PR-B5-T2: Image gen failure falls back to emoji + real-life suggestion
- PR-B5-T3: Manifest written to Postgres; task_title never written to logs
- PR-B5-T4: CI grep — no 'task_title' literal in any committed Python source file
              (private column name, not var-name usage in production code)

Private data discipline: no real task titles in this test file.
All task_title values use placeholder strings (e.g., "Placeholder therapy task").
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_image(
    path: str = "/tmp/reward_artifacts/test-image.png",
    *,
    theme_family: str = "test theme",
    style: str = "test style",
    palette: str = "test palette",
) -> dict[str, str]:
    """Build an ImageGeneration-shaped result for mocking generate_reward_image."""
    return {
        "path": path,
        "theme_family": theme_family,
        "style": style,
        "palette": palette,
    }


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _RewardFeedbackConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.updated_ids: list[str] = []
        self.updated_scores: list[int] = []

    async def execute(self, query: str, params: tuple[Any, ...] | None = None) -> _FakeCursor:
        if "SELECT id" in query:
            assert params is not None
            peer, target_dt, lower_seconds, upper_target_dt, upper_seconds, order_dt = params
            assert target_dt == upper_target_dt == order_dt
            candidates = [
                row for row in self.rows
                if row["peer"] == peer
                and row["feedback_at"] is None
                and target_dt - timedelta(seconds=lower_seconds)
                <= row["delivered_at"]
                <= target_dt + timedelta(seconds=upper_seconds)
            ]
            candidates.sort(key=lambda row: abs((row["delivered_at"] - order_dt).total_seconds()))
            return _FakeCursor(candidates[:1])

        if "UPDATE reward_manifests" in query:
            assert params is not None
            score, emoji, manifest_id = params
            self.updated_ids.append(manifest_id)
            self.updated_scores.append(score)
            for row in self.rows:
                if row["id"] == manifest_id:
                    row["feedback_score"] = score
                    row["feedback_emoji"] = emoji
                    row["feedback_at"] = datetime.now(UTC)
            return _FakeCursor([])

        raise AssertionError(f"Unexpected query: {query}")


# ---------------------------------------------------------------------------
# PR-B5-T1: Sensitive-task suppression
# ---------------------------------------------------------------------------

class TestSensitiveTaskSuppression:
    """Sensitive tasks must receive muted emoji and no image."""

    def test_is_sensitive_task_therapy(self) -> None:
        """Task title containing 'therapy' keyword classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder therapy task") is True

    def test_is_sensitive_task_medical(self) -> None:
        """Task title containing 'doctor' classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder doctor appointment") is True

    def test_is_sensitive_task_legal(self) -> None:
        """Task title containing 'lawyer' classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder lawyer meeting") is True

    def test_is_sensitive_task_financial(self) -> None:
        """Task title containing 'taxes' classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder taxes task") is True

    def test_is_sensitive_task_not_sensitive(self) -> None:
        """Neutral task title does not classify as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder grocery run") is False

    def test_sensitive_task_emoji_is_muted(self) -> None:
        """Sensitive task must receive muted celebration with no emoji fanfare."""
        from app.tools.rewards import get_celebration_emoji
        result = get_celebration_emoji("epic", sensitive_task=True)
        # Must be warm but not fanfare — no emoji characters
        assert result == "Done. That mattered."
        # Confirm no emoji present
        assert "🏆" not in result
        assert "🔥" not in result
        assert "💪" not in result

    def test_nonsensitive_epic_has_fanfare(self) -> None:
        """Non-sensitive epic task must have celebratory emoji.

        Checks across all epic templates — any one of them must contain
        at least one emoji from the full epic pool.
        """
        from app.tools.rewards import _EMOJI_TEMPLATES
        # Collect all emoji present in all epic templates
        all_epic_text = " ".join(_EMOJI_TEMPLATES["epic"])
        # Must contain at least one non-ASCII emoji character
        has_emoji = any(ord(c) > 127 for c in all_epic_text)
        assert has_emoji, "Epic intensity templates must contain celebratory emoji"

        # Also verify the muted path is NOT returned for non-sensitive
        from app.tools.rewards import get_celebration_emoji
        # Run multiple times to sample across pool (random.choice)
        results = {get_celebration_emoji("epic", sensitive_task=False) for _ in range(10)}
        # None of the results should be the sensitive-only muted message
        assert "Done. That mattered." not in results

    @pytest.mark.asyncio
    async def test_maybe_reward_sensitive_skips_image(self) -> None:
        """maybe_reward with a sensitive task title must not attempt image generation."""
        from app.tools import rewards as rewards_module

        # Patch generate_reward_image and write_reward_manifest
        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock()) as mock_gen,
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-1>",
                task_title="Placeholder therapy appointment",  # sensitive keyword
                notion_page_id="<page-id-001>",
                streak=3,
                energy_required="Medium",
                time_estimate=30,
            )

        # Image generation must not have been called
        mock_gen.assert_not_called()
        # Result must not contain MEDIA: line; attachment_path must be None
        assert "MEDIA:" not in result["text"]
        assert result["attachment_path"] is None

    @pytest.mark.asyncio
    async def test_maybe_reward_sensitive_manifest_marks_sensitive(self) -> None:
        """maybe_reward must set sensitive_task=True on the manifest for sensitive tasks."""
        from app.tools import rewards as rewards_module

        recorded_calls: list[dict] = []

        async def fake_write_manifest(**kwargs: Any) -> uuid.UUID:
            recorded_calls.append(kwargs)
            return uuid.uuid4()

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=fake_write_manifest),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-2>",
                task_title="Placeholder doctor visit task",
                notion_page_id="<page-id-002>",
                streak=1,
                energy_required="High",
                time_estimate=60,
            )

        assert len(recorded_calls) == 1
        assert recorded_calls[0]["sensitive_task"] is True


# ---------------------------------------------------------------------------
# PR-B5-T2: Image gen failure falls back to emoji + real-life suggestion
# ---------------------------------------------------------------------------

class TestImageGenFallback:
    """When image generation fails, rewards must fall back gracefully."""

    @pytest.mark.asyncio
    async def test_image_gen_failure_triggers_fallback(self) -> None:
        """Image gen returning None on medium intensity must add fallback suggestion."""
        from app.tools import rewards as rewards_module

        # Ensure OPENAI_API_KEY is set so the function tries to call image gen
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("medium", 40)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-3>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-003>",
                streak=2,
                energy_required="Medium",
                time_estimate=30,
            )

        # Must contain a fallback suggestion (plain text, no MEDIA:); no image path
        assert "MEDIA:" not in result["text"]
        lines = result["text"].strip().split("\n")
        assert len(lines) >= 2, "Expected celebration + fallback on separate lines"
        assert result["attachment_path"] is None

    @pytest.mark.asyncio
    async def test_image_gen_success_no_fallback(self) -> None:
        """When image gen succeeds, no fallback suggestion is appended.

        The image path is surfaced via RewardResult.attachment_path, not
        embedded in the text body. The absence of a newline-separated
        fallback line in result.text signals image gen succeeded.
        attachment_path must equal the path returned by generate_reward_image.
        """
        from app.tools import rewards as rewards_module

        fake_path = "/tmp/reward_artifacts/test-image.png"
        manifest_mock = AsyncMock(return_value=uuid.uuid4())
        with (
            patch.object(
                rewards_module,
                "generate_reward_image",
                new=AsyncMock(return_value=_fake_image(fake_path)),
            ),
            patch.object(rewards_module, "write_reward_manifest", new=manifest_mock),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-4>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-004>",
                streak=5,
                energy_required="High",
                time_estimate=60,
            )

        # No fallback appended to text (image succeeded)
        assert "\n" not in result["text"].strip()
        # Image path surfaced via RewardResult, not embedded in text
        assert result["attachment_path"] == fake_path
        manifest_mock.assert_awaited_once()
        manifest_kwargs = manifest_mock.await_args.kwargs
        assert manifest_kwargs["artifact_path"] == fake_path
        assert manifest_kwargs["reward_kind"] == "emoji+image"
        # Visual descriptors are persisted so a later reaction can be
        # attributed to them (migration 0011).
        assert manifest_kwargs["theme_family"] == "test theme"
        assert manifest_kwargs["style"] == "test style"
        assert manifest_kwargs["palette"] == "test palette"

    @pytest.mark.asyncio
    async def test_lightest_never_attempts_image(self) -> None:
        """Lightest intensity must never attempt image generation."""
        from app.tools import rewards as rewards_module

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock()) as mock_gen,
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("lightest", 5)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-5>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-005>",
                streak=1,
                energy_required="Low",
                time_estimate=5,
            )

        mock_gen.assert_not_called()
        assert "MEDIA:" not in result["text"]
        assert result["attachment_path"] is None

    def test_generate_reward_image_returns_none_without_api_key(self) -> None:
        """generate_reward_image must return None immediately when OPENAI_API_KEY unset."""
        import asyncio

        from app.tools.rewards import generate_reward_image

        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            result = asyncio.run(
                generate_reward_image(
                    intensity="medium",
                    streak_count=2,
                    task_descriptions=["Placeholder description"],
                )
            )
        assert result is None


    def test_generate_reward_image_logs_start_and_end_events(self) -> None:
        """image_gen.start and image_gen.end must be logged with correct payload shape."""
        import asyncio
        import base64
        import tempfile
        from unittest.mock import AsyncMock as _AsyncMock
        from unittest.mock import MagicMock as _MagicMock

        from app.tools.rewards import generate_reward_image

        fake_b64 = base64.b64encode(b"fake-image-bytes").decode()
        fake_image = _MagicMock()
        fake_image.b64_json = fake_b64
        fake_response = _MagicMock()
        fake_response.data = [fake_image]

        mock_client = _MagicMock()
        mock_client.images = _MagicMock()
        mock_client.images.generate = _AsyncMock(return_value=fake_response)
        mock_openai_cls = _MagicMock(return_value=mock_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "REWARD_ARTIFACTS_DIR": tmpdir}):
                with patch("app.tools.rewards.log") as mock_log:
                    with patch("openai.AsyncOpenAI", mock_openai_cls):
                        result = asyncio.run(
                            generate_reward_image(
                                intensity="medium",
                                streak_count=1,
                                task_descriptions=["Placeholder task"],
                            )
                        )

        assert result is not None

        call_events = [c.args[0] for c in mock_log.info.call_args_list if c.args]
        assert "image_gen.start" in call_events
        assert "image_gen.end" in call_events

        start_call = next(c for c in mock_log.info.call_args_list if c.args and c.args[0] == "image_gen.start")
        assert start_call.kwargs.get("intensity") == "medium"
        assert isinstance(start_call.kwargs.get("streak_count"), int)

        end_call = next(c for c in mock_log.info.call_args_list if c.args and c.args[0] == "image_gen.end")
        assert end_call.kwargs.get("intensity") == "medium"
        assert isinstance(end_call.kwargs.get("duration_ms"), float)

        for c in mock_log.info.call_args_list:
            for v in c.kwargs.values():
                assert not isinstance(v, str) or "Placeholder" not in v, \
                    "task content must not appear in log fields"


class TestImageGenerationCallContract:
    """The parameters sent to the OpenAI images API must be valid for gpt-image-1.

    Regression guard. generate_reward_image() wraps its whole API call in a bare
    `except Exception: return None`, so an invalid parameter does not surface as
    an error — every completion silently degrades to the emoji fallback and the
    feature looks like it was never built. Mocking images.generate wholesale
    (as the tests above do) cannot catch that, because a MagicMock accepts any
    keyword argument. These tests assert on the call itself.
    """

    @staticmethod
    def _capture_generate_kwargs() -> dict[str, Any]:
        import asyncio
        import base64
        import tempfile

        from app.tools.rewards import generate_reward_image

        fake_image = MagicMock()
        fake_image.b64_json = base64.b64encode(b"fake-image-bytes").decode()
        fake_response = MagicMock()
        fake_response.data = [fake_image]

        generate_mock = AsyncMock(return_value=fake_response)
        mock_client = MagicMock()
        mock_client.images = MagicMock()
        mock_client.images.generate = generate_mock

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ, {"OPENAI_API_KEY": "test-key", "REWARD_ARTIFACTS_DIR": tmpdir}
            ):
                with patch("openai.AsyncOpenAI", MagicMock(return_value=mock_client)):
                    result = asyncio.run(
                        generate_reward_image(
                            intensity="epic",
                            streak_count=1,
                            task_descriptions=["Placeholder task"],
                        )
                    )

        assert result is not None, "image generation should have succeeded"
        generate_mock.assert_awaited_once()
        return dict(generate_mock.await_args.kwargs)

    def test_response_format_is_not_sent(self) -> None:
        """gpt-image-1 rejects response_format with a 400.

        Per the openai SDK docstring for images.generate: "This parameter isn't
        supported for the GPT image models, which always return base64-encoded
        images." Sending it broke every reward image in production while all
        unit tests still passed.
        """
        kwargs = self._capture_generate_kwargs()
        assert "response_format" not in kwargs, (
            "response_format must not be sent for gpt-image-1 — the API rejects it "
            "and the failure degrades silently to the emoji fallback"
        )

    def test_only_sends_parameters_the_sdk_accepts(self) -> None:
        """Every kwarg must be a real parameter of the installed SDK method.

        Catches typos and parameters dropped in an SDK upgrade, which would
        otherwise fail the same silent way.
        """
        import inspect

        from openai.resources.images import AsyncImages

        accepted = set(inspect.signature(AsyncImages.generate).parameters)
        unknown = set(self._capture_generate_kwargs()) - accepted
        assert not unknown, f"parameters not accepted by the installed SDK: {sorted(unknown)}"

    def test_sends_expected_model_and_size(self) -> None:
        """Model/size/quality must match the docs/reward-system.md technical table."""
        kwargs = self._capture_generate_kwargs()
        assert kwargs["model"] == "gpt-image-1"
        assert kwargs["size"] == "1024x1024"
        # Epic tier uses high quality; all other tiers use auto.
        assert kwargs["quality"] == "high"
        assert kwargs["n"] == 1


class TestFeedbackDecay:
    """Rating influence fades gradually instead of falling off a cliff.

    apply_feedback_weight() used to zero any rating >= 30 days old while
    load_feedback_history() loaded 90 days, so two thirds of every loaded
    history was guaranteed to contribute nothing. At this system's rating
    volume that discarded most of the evidence the user had given.
    """

    THEME = "phoenix rising from golden flames"

    def _nudge(self, timestamp: str) -> float:
        """Weight delta from one positive rating that matches on palette only.

        Deliberately a partial match. A full theme+style+palette match scores
        1.0 before decay, which the +/-0.5 nudge cap clamps flat — the curve
        under test would be invisible at every age. Palette alone scores 0.1,
        so the whole 90-day range stays inside the cap.
        """
        from app.tools.rewards import apply_feedback_weight

        history = [
            {
                "score": 1,
                "theme_family": "some other theme",
                "style": "some other style",
                "palette": "fire gold",
                "timestamp": timestamp,
            }
        ]
        weight = apply_feedback_weight(
            history,
            theme_family=self.THEME,
            style="majestic illustration",
            palette="fire gold",
        )
        return weight - 1.0

    @staticmethod
    def _aged(age_days: float) -> str:
        return (datetime.now(UTC) - timedelta(days=age_days)).isoformat()

    def test_decay_curve_halves_every_half_life(self) -> None:
        """Full strength today, half at 45 days, quarter at the 90-day edge."""
        from app.tools.rewards import _feedback_decay

        assert _feedback_decay(0) == pytest.approx(1.0)
        assert _feedback_decay(45) == pytest.approx(0.5)
        assert _feedback_decay(90) == pytest.approx(0.25)

    def test_decay_clamps_negative_ages(self) -> None:
        """Clock skew must not manufacture influence above the same-day maximum."""
        from app.tools.rewards import _feedback_decay

        assert _feedback_decay(-30) == pytest.approx(1.0)

    def test_influence_decreases_monotonically_with_age(self) -> None:
        """A mid-life rating counts less than a fresh one but more than an old one."""
        fresh = self._nudge(self._aged(0))
        half_life = self._nudge(self._aged(45))
        window_edge = self._nudge(self._aged(90))

        assert fresh > half_life > window_edge > 0

    def test_rating_at_window_edge_still_contributes(self) -> None:
        """A 90-day-old rating is still evidence.

        Regression: the old 30-day cliff zeroed it entirely even though
        load_feedback_history had gone to the trouble of loading it.
        """
        assert self._nudge(self._aged(90)) > 0

    def test_future_dated_rating_counts_as_fresh_not_more(self) -> None:
        """Clock skew clamps to the same-day maximum rather than exceeding it."""
        future = self._nudge(self._aged(-30))
        fresh = self._nudge(self._aged(0))

        assert future == pytest.approx(fresh)

    def test_unparseable_timestamp_degrades_instead_of_raising(self) -> None:
        """A malformed row contributes negligibly rather than breaking selection.

        It is treated as sitting at the far edge of the load window: still
        counted, but worth the least of anything that survives the window.
        """
        malformed = self._nudge("not-a-timestamp")

        assert malformed > 0
        assert malformed == pytest.approx(self._nudge(self._aged(90)))
        assert malformed < self._nudge(self._aged(0))

    def test_load_window_matches_the_decay_constants(self) -> None:
        """The load window and the decay curve cannot drift apart.

        A window narrower than the decay reach truncates ratings that should
        still count; a window wider loads rows that can never matter.
        """
        import inspect

        from app.tools.rewards import _FEEDBACK_WINDOW_DAYS, load_feedback_history

        default = inspect.signature(load_feedback_history).parameters["days"].default
        assert default == _FEEDBACK_WINDOW_DAYS


class TestFeedbackWeightedSelection:
    """Emoji reactions must actually steer future image selection.

    apply_feedback_weight() existed but was never called, and the columns it
    matches on were never persisted, so reactions could not influence anything.
    """

    @staticmethod
    def _history(theme: str, score: int, count: int = 6) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        return [
            {
                "score": score,
                "theme_family": theme,
                "style": "majestic illustration",
                "palette": "fire gold",
                "timestamp": now.isoformat(),
            }
            for _ in range(count)
        ]

    @staticmethod
    def _weights_for(history: list[dict[str, Any]]) -> dict[str, float]:
        """Return the selection weight _select_theme assigns to each theme.

        Asserts on the weights handed to random.choices rather than on sampled
        outcomes. Sampling would be flaky by construction: the nudge is capped
        at +/-0.5, so a favored theme in a 5-theme pool only moves from p=0.20
        to p=0.27, and any threshold between those is within normal variance.
        """
        from app.tools import rewards as rewards_module

        captured: dict[str, float] = {}

        def fake_choices(
            population: list[dict[str, str]],
            weights: list[float],
            k: int = 1,
        ) -> list[dict[str, str]]:
            for candidate, weight in zip(population, weights, strict=True):
                captured[candidate["theme_family"]] = weight
            return [population[0]]

        with patch.object(rewards_module.random, "choices", fake_choices):
            rewards_module._select_theme(intensity="high", feedback_history=history)

        assert captured, "_select_theme must select via weighted random choice"
        return captured

    def test_positively_rated_theme_is_favored(self) -> None:
        """A theme the user reacted positively to gets a higher selection weight."""
        liked = "phoenix rising from golden flames"
        weights = self._weights_for(self._history(liked, score=1))

        others = [w for theme, w in weights.items() if theme != liked]
        assert weights[liked] > max(others), (
            f"liked theme weight {weights[liked]} should exceed all others {others}"
        )

    def test_negatively_rated_theme_is_disfavored_but_still_possible(self) -> None:
        """Negative feedback reduces a theme's weight without zeroing it.

        Novelty is a hard requirement of docs/reward-system.md — habituation is
        the failure mode the image system exists to prevent — so no theme may
        ever be permanently excluded by feedback.
        """
        disliked = "phoenix rising from golden flames"
        weights = self._weights_for(self._history(disliked, score=-1))

        others = [w for theme, w in weights.items() if theme != disliked]
        assert weights[disliked] < min(others), "negative feedback should reduce the weight"
        assert all(w > 0 for w in weights.values()), (
            "feedback must nudge, never permanently exclude a theme"
        )

    def test_feedback_weight_stays_within_documented_bounds(self) -> None:
        """Weights stay in [0.5, 1.5] no matter how lopsided the history.

        docs/reward-system.md pins this range; it is what keeps one strong
        reaction from collapsing selection onto a single theme.
        """
        liked = "phoenix rising from golden flames"
        lopsided = self._history(liked, score=1, count=50)
        weights = self._weights_for(lopsided)

        assert all(0.5 <= w <= 1.5 for w in weights.values()), weights

    def test_no_feedback_selects_from_full_pool(self) -> None:
        """With no history, selection is unbiased across the intensity pool."""
        from app.tools.rewards import _THEME_POOLS, _select_theme

        picks = {
            _select_theme(intensity="low", feedback_history=[])["theme_family"]
            for _ in range(200)
        }
        assert picks == {t["theme"] for t in _THEME_POOLS["low"]}

    def test_sensitive_tasks_ignore_user_style_preferences(self) -> None:
        """The sensitive-task guardrail allowlist wins over user preferences."""
        from app.tools.rewards import _SENSITIVE_THEMES, _select_theme

        selection = _select_theme(
            intensity="epic",
            sensitive_task=True,
            user_prefs={"preferred_styles": ["neon airbrush"], "preferred_palettes": ["hot pink"]},
        )
        assert selection["style"] != "neon airbrush"
        assert selection["palette"] != "hot pink"
        assert selection["theme_family"] in {t["theme"] for t in _SENSITIVE_THEMES}

    def test_user_preferences_drive_style_and_palette(self) -> None:
        """Non-sensitive rewards honor the user_prefs.rewards taste profile."""
        from app.tools.rewards import _select_theme

        selection = _select_theme(
            intensity="medium",
            user_prefs={
                "preferred_styles": ["storybook watercolor"],
                "preferred_palettes": ["cozy pastel glow"],
            },
        )
        assert selection["style"] == "storybook watercolor"
        assert selection["palette"] == "cozy pastel glow"

    def test_fallback_reward_pool_has_min_size(self) -> None:
        """Fallback reward pool must have at least 12 entries."""
        from app.tools.rewards import _FALLBACK_REWARDS
        assert len(_FALLBACK_REWARDS) >= 12


# ---------------------------------------------------------------------------
# Reward feedback reactions
# ---------------------------------------------------------------------------

class TestRewardFeedback:
    """Signal reaction feedback must attribute only to the reacted-to reward."""

    def test_emoji_score_mapping(self) -> None:
        """Known feedback emojis map to positive, negative, or neutral scores."""
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES

        assert _FEEDBACK_EMOJI_SCORES["👍"] == 1
        assert _FEEDBACK_EMOJI_SCORES["👎"] == -1
        assert _FEEDBACK_EMOJI_SCORES.get("🤷", 0) == 0

    @pytest.mark.asyncio
    async def test_record_reward_feedback_matches_within_thirty_seconds(self) -> None:
        """A reaction timestamp within the ±30s window updates that reward."""
        from app.tools import rewards as rewards_module

        target_dt = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
        conn = _RewardFeedbackConn([
            {
                "id": "manifest-1",
                "peer": "<test-peer-10>",
                "delivered_at": target_dt + timedelta(seconds=12),
                "feedback_at": None,
            }
        ])

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[_RewardFeedbackConn, None]:
            yield conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer-10>",
                emoji="👍",
                target_sent_timestamp=int(target_dt.timestamp() * 1000),
            )

        assert result is True
        assert conn.updated_ids == ["manifest-1"]
        assert conn.updated_scores == [1]

    @pytest.mark.asyncio
    async def test_record_reward_feedback_outside_window_returns_false(self) -> None:
        """A reaction outside ±30s must not update a reward."""
        from app.tools import rewards as rewards_module

        target_dt = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
        conn = _RewardFeedbackConn([
            {
                "id": "manifest-1",
                "peer": "<test-peer-11>",
                "delivered_at": target_dt - timedelta(seconds=31),
                "feedback_at": None,
            }
        ])

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[_RewardFeedbackConn, None]:
            yield conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer-11>",
                emoji="👍",
                target_sent_timestamp=int(target_dt.timestamp() * 1000),
            )

        assert result is False
        assert conn.updated_ids == []

    @pytest.mark.asyncio
    async def test_record_reward_feedback_chooses_closest_reward(self) -> None:
        """When two rewards are near the timestamp, only the closest one is updated."""
        from app.tools import rewards as rewards_module

        target_dt = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
        conn = _RewardFeedbackConn([
            {
                "id": "manifest-older",
                "peer": "<test-peer-12>",
                "delivered_at": target_dt - timedelta(seconds=25),
                "feedback_at": None,
            },
            {
                "id": "manifest-closest",
                "peer": "<test-peer-12>",
                "delivered_at": target_dt + timedelta(seconds=2),
                "feedback_at": None,
            },
        ])

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[_RewardFeedbackConn, None]:
            yield conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer-12>",
                emoji="👎",
                target_sent_timestamp=int(target_dt.timestamp() * 1000),
            )

        assert result is True
        assert conn.updated_ids == ["manifest-closest"]
        assert conn.updated_scores == [-1]

    @pytest.mark.asyncio
    async def test_load_feedback_history_returns_recent_feedback(self) -> None:
        """Feedback history is normalized into prompt-friendly dictionaries."""
        from app.tools import rewards as rewards_module

        feedback_at = datetime(2026, 5, 27, 12, 5, 0, tzinfo=UTC)
        rows = [
            {
                "feedback_score": 1,
                "feedback_emoji": "👍",
                "feedback_at": feedback_at,
                "intensity": "high",
                "reward_kind": "emoji+image",
                "theme_family": "phoenix rising from golden flames",
                "style": "majestic illustration",
                "palette": "fire gold",
            }
        ]
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value=_FakeCursor(rows))

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.load_feedback_history("<test-peer-13>")

        assert result == [
            {
                "score": 1,
                "emoji": "👍",
                "timestamp": feedback_at.isoformat(),
                "intensity": "high",
                "reward_kind": "emoji+image",
                # Carried through so apply_feedback_weight() can match on them.
                "theme_family": "phoenix rising from golden flames",
                "style": "majestic illustration",
                "palette": "fire gold",
            }
        ]

    @pytest.mark.asyncio
    async def test_load_reward_prefs_returns_the_rewards_subtree(self) -> None:
        """load_reward_prefs unwraps prefs_json -> 'rewards'."""
        from app.tools import rewards as rewards_module

        rows = [
            {
                "prefs_json": {
                    "timezone": "America/Chicago",
                    "rewards": {"preferred_styles": ["placeholder style"]},
                }
            }
        ]
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value=_FakeCursor(rows))

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.load_reward_prefs("<test-peer-16>")

        assert result == {"preferred_styles": ["placeholder style"]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([], id="no-row-for-peer"),
            pytest.param([{"prefs_json": {}}], id="no-rewards-subtree"),
            pytest.param([{"prefs_json": {"rewards": ["a", "b"]}}], id="rewards-is-array"),
            pytest.param([{"prefs_json": {"rewards": "text"}}], id="rewards-is-scalar"),
            pytest.param([{"prefs_json": ["not", "an", "object"]}], id="prefs-json-is-array"),
        ],
    )
    async def test_load_reward_prefs_degrades_to_empty(self, rows: list[dict[str, Any]]) -> None:
        """Absent or wrongly-shaped preferences return {} rather than raising.

        jsonb accepts scalars and arrays, so the column type does not
        guarantee the shape the reward path expects.
        """
        from app.tools import rewards as rewards_module

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value=_FakeCursor(rows))

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            assert await rewards_module.load_reward_prefs("<test-peer-17>") == {}

    @pytest.mark.asyncio
    async def test_load_reward_prefs_fails_open_on_db_error(self) -> None:
        """A preferences lookup failure returns {} instead of propagating."""
        from app.tools import rewards as rewards_module

        @asynccontextmanager
        async def crashing_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            raise RuntimeError("DB unavailable")
            yield MagicMock()  # pragma: no cover

        with patch("app.tools.db.get_db_conn", crashing_get_db_conn):
            assert await rewards_module.load_reward_prefs("<test-peer-18>") == {}

    @pytest.mark.asyncio
    async def test_maybe_reward_loads_stored_prefs_when_none_passed(self) -> None:
        """The graph never passes preferences, so maybe_reward must fetch them.

        Regression: complete_node calls maybe_reward without user_prefs and
        nothing read the user_prefs table, so a stored taste profile could
        never reach image generation.
        """
        from app.tools import rewards as rewards_module

        stored = {"preferred_styles": ["placeholder style"]}
        prefs_mock = AsyncMock(return_value=stored)
        image_mock = AsyncMock(return_value=_fake_image())

        with (
            patch.object(rewards_module, "load_reward_prefs", new=prefs_mock),
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-19>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-019>",
                streak=3,
                energy_required="High",
                time_estimate=45,
            )

        prefs_mock.assert_awaited_once_with("<test-peer-19>")
        assert image_mock.await_args.kwargs["user_prefs"] == stored

    @pytest.mark.asyncio
    async def test_explicit_user_prefs_argument_wins_over_stored(self) -> None:
        """An explicit argument short-circuits the lookup entirely."""
        from app.tools import rewards as rewards_module

        prefs_mock = AsyncMock(return_value={"preferred_styles": ["stored style"]})
        image_mock = AsyncMock(return_value=_fake_image())

        with (
            patch.object(rewards_module, "load_reward_prefs", new=prefs_mock),
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-20>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-020>",
                streak=3,
                energy_required="High",
                time_estimate=45,
                user_prefs={"rewards": {"preferred_styles": ["explicit style"]}},
            )

        prefs_mock.assert_not_awaited()
        assert image_mock.await_args.kwargs["user_prefs"] == {
            "preferred_styles": ["explicit style"]
        }

    @pytest.mark.asyncio
    async def test_explicit_empty_user_prefs_wins_over_stored(self) -> None:
        """An explicit empty dict short-circuits the lookup — {} is a valid override."""
        from app.tools import rewards as rewards_module

        prefs_mock = AsyncMock(return_value={"preferred_styles": ["stored style"]})
        image_mock = AsyncMock(return_value=_fake_image())

        with (
            patch.object(rewards_module, "load_reward_prefs", new=prefs_mock),
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-20b>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-020b>",
                streak=3,
                energy_required="High",
                time_estimate=45,
                user_prefs={},
            )

        prefs_mock.assert_not_awaited()
        assert image_mock.await_args.kwargs["user_prefs"] is None

    @pytest.mark.asyncio
    async def test_maybe_reward_continues_if_prefs_lookup_raises(self) -> None:
        """A preferences failure must not block reward delivery."""
        from app.tools import rewards as rewards_module

        async def crashing_prefs(_peer: str) -> dict[str, Any]:
            raise RuntimeError("DB unavailable")

        image_mock = AsyncMock(return_value=_fake_image())

        with (
            patch.object(rewards_module, "load_reward_prefs", new=crashing_prefs),
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=[])),
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-21>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-021>",
                streak=3,
                energy_required="High",
                time_estimate=45,
            )

        assert result["attachment_path"] == "/tmp/reward_artifacts/test-image.png"

    @pytest.mark.asyncio
    async def test_maybe_reward_passes_feedback_history_to_image_generation(self) -> None:
        """maybe_reward loads peer feedback and forwards it into image generation."""
        from app.tools import rewards as rewards_module

        history = [
            {"score": 1, "timestamp": "2026-05-27T12:00:00+00:00"},
            {"score": 1, "timestamp": "2026-05-26T12:00:00+00:00"},
            {"score": 1, "timestamp": "2026-05-25T12:00:00+00:00"},
        ]
        image_mock = AsyncMock(return_value=_fake_image())

        with (
            patch.object(rewards_module, "load_feedback_history", new=AsyncMock(return_value=history)) as load_mock,
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-14>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-014>",
                streak=3,
                energy_required="High",
                time_estimate=45,
            )

        load_mock.assert_awaited_once_with("<test-peer-14>")
        assert image_mock.await_args.kwargs["feedback_history"] == history

    @pytest.mark.asyncio
    async def test_maybe_reward_continues_if_feedback_history_raises(self) -> None:
        """A feedback history failure must not block reward delivery."""
        from app.tools import rewards as rewards_module

        async def crashing_history(_peer: str) -> list[dict[str, Any]]:
            raise RuntimeError("DB unavailable")

        image_mock = AsyncMock(return_value=_fake_image())

        with (
            patch.object(rewards_module, "load_feedback_history", new=crashing_history),
            patch.object(rewards_module, "generate_reward_image", new=image_mock),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-15>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-015>",
                streak=3,
                energy_required="High",
                time_estimate=45,
            )

        assert result["attachment_path"] == "/tmp/reward_artifacts/test-image.png"
        assert image_mock.await_args.kwargs["feedback_history"] == []

    def test_build_image_prompt_includes_positive_feedback_guidance(self) -> None:
        """Three or more positive ratings add prompt guidance for future images."""
        from app.tools.rewards import _build_image_prompt

        prompt = _build_image_prompt(
            intensity="high",
            streak_count=3,
            task_descriptions=["Placeholder task title"],
            feedback_history=[
                {"score": 1},
                {"score": 1},
                {"score": 1},
            ],
        )

        assert "User has positively responded to recent rewards" in prompt
        assert "lean energetic and celebratory" in prompt


# ---------------------------------------------------------------------------
# PR-B5-T3: Manifest written to Postgres, task_title never to logs
# ---------------------------------------------------------------------------

class TestManifestWriting:
    """Manifest must be written to Postgres; task_title must never appear in log output."""

    @pytest.mark.asyncio
    async def test_manifest_inserts_to_postgres(self) -> None:
        """write_reward_manifest must execute an INSERT with all required columns."""
        from app.tools import rewards as rewards_module

        executed_queries: list[str] = []
        executed_params: list[tuple] = []

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=lambda q, p=None: executed_queries.append(q) or executed_params.append(p))

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.write_reward_manifest(
                peer="<test-peer-6>",
                notion_page_id="<page-id-006>",
                task_title="Placeholder private task",
                reward_kind="emoji+image",
                intensity="high",
                streak_count=3,
                delivered_at=datetime.now(UTC),
                sensitive_task=False,
            )

        assert result is not None
        assert len(executed_queries) == 1
        query = executed_queries[0]
        assert "reward_manifests" in query
        assert "INSERT" in query.upper()

        # Verify task_title is passed as a parameter (not embedded in query string)
        params = executed_params[0]
        assert "Placeholder private task" in params

    @pytest.mark.asyncio
    async def test_task_title_never_in_log_output(self, caplog: pytest.LogCaptureFixture) -> None:
        """write_reward_manifest must not emit task_title to any log record."""
        from app.tools import rewards as rewards_module

        private_title = "Placeholder-private-do-not-log-5x9z"

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock()

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with caplog.at_level(logging.DEBUG):
            with patch("app.tools.db.get_db_conn", fake_get_db_conn):
                await rewards_module.write_reward_manifest(
                    peer="<test-peer-7>",
                    notion_page_id="<page-id-007>",
                    task_title=private_title,
                    reward_kind="emoji",
                    intensity="low",
                    streak_count=1,
                    delivered_at=datetime.now(UTC),
                    sensitive_task=False,
                )

        # task_title must not appear in any log record
        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert private_title not in all_log_text, (
            "task_title leaked into log output — private data violation"
        )

    @pytest.mark.asyncio
    async def test_maybe_reward_task_title_never_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """maybe_reward must not emit task_title in any log record at any level."""
        from app.tools import rewards as rewards_module

        private_title = "Placeholder-private-maybe-reward-7a3b"

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            caplog.at_level(logging.DEBUG),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-8>",
                task_title=private_title,
                notion_page_id="<page-id-008>",
                streak=1,
                energy_required="Low",
                time_estimate=10,
            )

        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert private_title not in all_log_text, (
            "task_title leaked from maybe_reward into log output — private data violation"
        )

    @pytest.mark.asyncio
    async def test_manifest_failure_does_not_crash_maybe_reward(self) -> None:
        """A Postgres failure in write_reward_manifest must not prevent reward delivery."""
        from app.tools import rewards as rewards_module

        async def crashing_manifest(**_kwargs: Any) -> None:
            raise RuntimeError("DB connection refused")

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=crashing_manifest),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-9>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-009>",
                streak=1,
                energy_required="Low",
                time_estimate=10,
            )

        # Must still return a RewardResult with celebration text
        assert isinstance(result["text"], str)
        assert len(result["text"]) > 0


# ---------------------------------------------------------------------------
# PR-B5-T4: CI grep — 'task_title' as private column name must not leak
#            as a literal string in structlog field keys in committed source
# ---------------------------------------------------------------------------

class TestPrivateDataDiscipline:
    """CI-style checks for private data leakage in source files."""

    def test_no_task_title_in_structlog_field_keys(self) -> None:
        """No Python source file may pass 'task_title' as a structlog field key.

        task_title is a private Postgres column. It may appear as a variable name
        or parameter name in application code, but must NEVER appear as a string
        literal key in a log.info/log.warning/log.error/log.debug call.

        The pattern checked is: log.<level>(..., task_title=... OR "task_title"=...)
        """
        import re
        from pathlib import Path

        # Pattern: any structlog call with task_title as a keyword argument
        # or as a string literal key
        log_call_with_task_title = re.compile(
            r'\blog\.(info|warning|error|debug|exception)\s*\([^)]*\btask_title\s*='
        )

        repo_root = Path(__file__).parent.parent.parent
        source_dirs = [repo_root / "app", repo_root / "tests"]

        violations: list[str] = []
        for source_dir in source_dirs:
            for py_file in source_dir.rglob("*.py"):
                content = py_file.read_text(encoding="utf-8")
                if log_call_with_task_title.search(content):
                    violations.append(str(py_file.relative_to(repo_root)))

        assert not violations, (
            f"task_title found as a structlog field key in: {violations}. "
            "task_title is private — never log it."
        )

    def test_reward_manifests_sql_marks_task_title_private(self) -> None:
        """The reward_manifests migration must mark task_title as a private column."""
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent.parent / "migrations" / "0002_reward_manifests.sql"
        )
        assert migration_path.is_file(), f"Migration not found: {migration_path}"

        content = migration_path.read_text(encoding="utf-8")
        # Must contain the private marker comment for task_title
        assert "PRIVATE" in content, "Migration must mark task_title as PRIVATE"
        assert "task_title" in content

    def test_no_real_task_title_in_fixture_files(self) -> None:
        """Test fixtures must not contain real task_title values.

        All fixture task titles must use placeholder strings, not real content.
        """
        import json
        from pathlib import Path

        fixtures_dir = Path(__file__).parent.parent / "fixtures"
        if not fixtures_dir.is_dir():
            pytest.skip("No fixtures directory found")

        # Known-safe placeholder prefix
        safe_prefixes = ("Placeholder", "<", "Test", "test", "sample", "Sample")

        for fixture_file in fixtures_dir.glob("*.json"):
            content = json.loads(fixture_file.read_text(encoding="utf-8"))
            # Look for any task_title fields in the fixture
            def check_values(obj: Any, path: str = "", _fname: str = fixture_file.name) -> list[str]:
                violations = []
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == "task_title" and isinstance(v, str):
                            if not any(v.startswith(p) for p in safe_prefixes) and v != "":
                                violations.append(f"{_fname}:{path}.{k}={v!r}")
                        violations.extend(check_values(v, f"{path}.{k}"))
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        violations.extend(check_values(item, f"{path}[{i}]"))
                return violations

            found = check_values(content)
            assert not found, (
                f"Possible real task_title in fixture: {found}. Use placeholder values."
            )
