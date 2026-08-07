"""Reward subsystem for hide-my-list.

v1 scope per docs/reward-system.md:
- Emoji rewards (intensity-mapped)
- AI-generated celebration images via OpenAI gpt-image-1
- Sensitive-task guardrails (docs/reward-system.md:302-348)
- Feedback weighting (docs/reward-system.md:361-445)
- Fallback rewards when image gen fails
- Weekly recap (image compilation)

Deferred to v1.1:
- Audio rewards (home audio integration)
- Outing suggestions

See docs/python-rewrite/reward-deferred.md for deferred feature details.

Private data discipline (Codex F018):
- task_title is NEVER written to any log output
- reward_manifests Postgres table stores task_title (private column)
- Generated images stored under reward_artifacts Docker volume mount
- Test fixtures must NOT contain real task_title values
"""
from __future__ import annotations

import os
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from typing_extensions import TypedDict

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# RewardResult — returned by maybe_reward()
# ---------------------------------------------------------------------------

class RewardResult(TypedDict):
    """Result of a reward delivery attempt.

    text: Celebration message string (emoji text, possibly with fallback line).
    attachment_path: Absolute path to generated PNG, or None when no image.
        This value is private — it traces back to the user's task via the
        manifest table. Never log it; log attachment_count only.
    """
    text: str
    attachment_path: str | None


class ImageGeneration(TypedDict):
    """A successfully generated reward image plus the visual choices behind it.

    path: Absolute path to the generated PNG. Private — never log it.
    theme_family / style / palette: the selected visual descriptors. These are
        generic art descriptors (not user data) and are persisted on the
        manifest row so a later emoji reaction can be attributed to them —
        that attribution is what makes apply_feedback_weight() able to learn.
    """
    path: str
    theme_family: str
    style: str
    palette: str


# ---------------------------------------------------------------------------
# Sensitive task classification
# docs/reward-system.md:302-348
# ---------------------------------------------------------------------------

_SENSITIVE_KEYWORDS: frozenset[str] = frozenset([
    # Therapy / mental health
    "therapy", "therapist", "counseling", "counselor", "psychiatrist", "psychiatry",
    "mental health", "psychology", "psychologist", "anxiety", "depression",
    # Medical
    "doctor", "physician", "medical", "hospital", "clinic", "diagnosis",
    "medication", "prescription", "surgery", "appointment",
    # Legal
    "lawyer", "attorney", "legal", "court", "lawsuit", "contract",
    # Financial
    "taxes", "tax return", "irs", "bankruptcy", "debt", "financial advisor",
    # Personal admin
    "divorce", "custody", "funeral", "estate",
])


def is_sensitive_task(task_title: str) -> bool:
    """Classify whether a task title is sensitive (private/shame-heavy).

    Sensitive tasks receive suppressed or muted rewards:
    - task_mode forced to metaphorical
    - no literal task artifacts in imagery
    - humor forced to subtle

    Args:
        task_title: Task title string (private — not logged by this function).

    Returns:
        True if the task is classified as sensitive.
    """
    title_lower = task_title.lower()
    return any(keyword in title_lower for keyword in _SENSITIVE_KEYWORDS)


# ---------------------------------------------------------------------------
# Intensity scoring
# docs/reward-system.md: Score Calculation section
# ---------------------------------------------------------------------------

def compute_intensity(
    *,
    time_estimate: int,
    energy_required: str,
    streak: int,
    is_parent_complete: bool = False,
    is_all_cleared: bool = False,
    rewards_in_last_hour: int = 0,
    trigger: str = "completion",
    initiation_base_weight: float = 1.0,
    initiation_ceiling: int = 100,
) -> tuple[str, int]:
    """Compute reward intensity level and score.

    Implements the unified scoring algorithm from docs/reward-system.md.
    Returns (intensity_label, score).

    Args:
        time_estimate: Task time estimate in minutes.
        energy_required: "High", "Medium", or "Low".
        streak: Current consecutive completion streak.
        is_parent_complete: True if all sub-tasks of a parent task are done.
        is_all_cleared: True if all pending tasks cleared.
        rewards_in_last_hour: Count of rewards delivered in the past hour (for diminishing returns).
        trigger: "completion" or initiation trigger name.
        initiation_base_weight: Per-trigger multiplier (1.0 for completion).
        initiation_ceiling: Per-trigger score cap (100 for completion).

    Returns:
        Tuple of (intensity_label, score) where intensity_label is one of:
        "lightest", "low", "medium", "high", "epic".
    """
    energy_map = {"High": 3, "Medium": 2, "Low": 1}
    energy_value = energy_map.get(energy_required, 2)

    # Base score
    base_score = (time_estimate / 15) * 10 + (energy_value * 10)

    # Streak bonus
    streak_bonus = streak * 5

    # Milestone bonuses
    milestone_bonus = 0
    if is_parent_complete:
        milestone_bonus += 25
    if is_all_cleared:
        milestone_bonus += 50

    raw_score = base_score + streak_bonus + milestone_bonus

    # Diminishing returns
    diminishing = max(0, (rewards_in_last_hour - 2) * 10)

    if trigger == "completion":
        score = int(min(100, max(0, raw_score - diminishing)))
    else:
        # Initiation triggers
        weighted_score = (base_score * initiation_base_weight) + streak_bonus
        score = int(min(initiation_ceiling, max(0, weighted_score - diminishing)))

    # Map to intensity levels
    if score <= 10:
        return "lightest", score
    if score <= 25:
        return "low", score
    if score <= 50:
        return "medium", score
    if score <= 75:
        return "high", score
    return "epic", score


# ---------------------------------------------------------------------------
# Emoji celebration
# docs/reward-system.md: Emoji Celebrations section
# ---------------------------------------------------------------------------

_EMOJI_TEMPLATES: dict[str, list[str]] = {
    "lightest": ["Nice."],
    "low": ["Nice work! ✨", "Done! 💫", "Got it! ✅", "Speed demon! ⚡"],
    "medium": ["Deep work done! 🧠✨", "Hat trick! 🎩✨🎉", "Crushing it! 🎉✨💪", "Three down! 🔥💪"],
    "high": ["UNSTOPPABLE! 🔥🎉✨💪🚀", "On fire! 🔥🔥🔥✨💪", "Beast mode! 💪🔥🎉", "Conquered! ⚔️✨🏆"],
    "epic": [
        "LEGENDARY! 🏆👑🔥🎉✨💪🚀⭐",
        "MAJOR WIN! 🏆👑🎉✨🔥",
        "INBOX ZERO! 🏆👑✨🎉🔥💪🚀",
        "LEGENDARY DAY! 👑⭐🏆🎊",
        "PROJECT COMPLETE! 🚀⭐💪🎊",
    ],
}


def get_celebration_emoji(intensity: str, sensitive_task: bool = False) -> str:
    """Return an emoji celebration string for the given intensity.

    Args:
        intensity: One of "lightest", "low", "medium", "high", "epic".
        sensitive_task: If True, returns a muted response (no emoji).

    Returns:
        Celebration string.
    """
    if sensitive_task:
        # Muted reward for sensitive tasks — calm and warm, no fanfare.
        # "That took courage." can over-label routine private tasks as emotionally loaded.
        # "That mattered." is neutral, affirming, and applies to any private task category.
        return "Done. That mattered."

    templates = _EMOJI_TEMPLATES.get(intensity, _EMOJI_TEMPLATES["low"])
    return random.choice(templates)


# ---------------------------------------------------------------------------
# Fallback reward pool
# docs/reward-system.md: Graceful Degradation section
# ---------------------------------------------------------------------------

_FALLBACK_REWARDS: list[str] = [
    "Treat yourself to your favorite snack.",
    "30 minutes of your favorite game — you've earned it.",
    "Fancy coffee or hot chocolate time.",
    "Take a walk outside — fresh air after good work.",
    "Stretch or do a few yoga poses.",
    "Mini dance party in your living room.",
    "Call a friend and celebrate.",
    "Watch an episode of something you love.",
    "Order your favorite takeout.",
    "A cupcake or small treat.",
    "Ice cream — classic reward.",
    "Square of good chocolate.",
]


def get_fallback_reward() -> str:
    """Return a fun non-digital real-life reward suggestion.

    Used when image generation is unavailable.
    """
    return random.choice(_FALLBACK_REWARDS)


# ---------------------------------------------------------------------------
# Image generation
# docs/reward-system.md: AI-Generated Celebration Images section
# ---------------------------------------------------------------------------

_THEME_POOLS: dict[str, list[dict[str, str]]] = {
    "low": [
        {"theme": "cheerful bird with sparkle", "style": "watercolor", "palette": "warm pastel"},
        {"theme": "paper airplane soaring through clouds", "style": "paper collage", "palette": "soft blue"},
        {"theme": "happy cat in sunbeam", "style": "storybook illustration", "palette": "cozy warm"},
        {"theme": "small garden with blooming flowers", "style": "watercolor", "palette": "nature green"},
        {"theme": "cozy reading nook with warm light", "style": "impressionist", "palette": "amber glow"},
    ],
    "medium": [
        {"theme": "fox dancing in wildflowers", "style": "vibrant illustration", "palette": "jewel tones"},
        {"theme": "confetti explosion in bright colors", "style": "graphic art", "palette": "rainbow"},
        {"theme": "otter sliding down rainbow waterfall", "style": "cartoon", "palette": "neon pastel"},
        {"theme": "butterfly emerging from cocoon in golden light", "style": "watercolor", "palette": "golden"},
        {"theme": "mountain summit with celebration flags", "style": "adventure illustration", "palette": "crisp blue"},
    ],
    "high": [
        {"theme": "phoenix rising from golden flames", "style": "majestic illustration", "palette": "fire gold"},
        {"theme": "astronaut planting flag on colorful planet", "style": "space art", "palette": "cosmic purple"},
        {"theme": "whale breaching in starfield", "style": "surreal art", "palette": "midnight blue"},
        {"theme": "ancient temple lit by aurora borealis", "style": "epic landscape", "palette": "aurora jewel"},
        {"theme": "eagle soaring above mountain range at dawn", "style": "realistic", "palette": "sunrise red"},
    ],
    "epic": [
        {"theme": "galaxy forming crown of light", "style": "cosmic art", "palette": "nebula purple"},
        {"theme": "reality folding into cathedral of light", "style": "transcendent digital art", "palette": "iridescent"},
        {"theme": "cosmic phoenix ascending through dimensional portal", "style": "epic sci-fi", "palette": "gold and violet"},
        {"theme": "universe crystallizing into perfect order", "style": "abstract cosmic", "palette": "prismatic"},
        {"theme": "ancient forest where trees become stars", "style": "mythic illustration", "palette": "silver and emerald"},
    ],
}

_SENSITIVE_THEMES: list[dict[str, str]] = [
    {"theme": "abstract geometric pattern expanding outward", "style": "minimalist", "palette": "calm blue-grey"},
    {"theme": "smooth river stones arranged in peaceful pattern", "style": "zen illustration", "palette": "earth tones"},
    {"theme": "gentle light through frosted glass", "style": "abstract", "palette": "soft white"},
    {"theme": "growing seedling in quiet soil", "style": "simple illustration", "palette": "natural green"},
    {"theme": "single candle flame in dark, steady and bright", "style": "minimal", "palette": "warm amber"},
]


def _select_theme(
    *,
    intensity: str,
    sensitive_task: bool = False,
    user_prefs: dict[str, Any] | None = None,
    feedback_history: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Pick a theme/style/palette, biased by prior emoji-reaction feedback.

    Candidates are the intensity's theme pool crossed with the user's preferred
    styles and palettes (when set). Each candidate is weighted by
    apply_feedback_weight(), whose influence halves every 45 days and whose
    total nudge is capped at +/-0.5 — so feedback nudges selection without ever
    locking it to one theme. Novelty
    is a hard requirement of docs/reward-system.md, so every candidate keeps a
    non-zero chance of being selected.

    Returns a dict with theme_family / style / palette keys.
    """
    prefs = user_prefs or {}

    if sensitive_task:
        pool = _SENSITIVE_THEMES
        # Sensitive tasks ignore user style/palette prefs — the guardrail
        # allowlist wins (docs/reward-system.md: Sensitive Task Guardrail).
        styles: list[str] = []
        palettes: list[str] = []
    else:
        pool = _THEME_POOLS.get(intensity, _THEME_POOLS["low"])
        styles = list(prefs.get("preferred_styles") or [])
        palettes = list(prefs.get("preferred_palettes") or [])

    candidates: list[dict[str, str]] = []
    for theme in pool:
        for style in styles or [theme["style"]]:
            for palette in palettes or [theme["palette"]]:
                candidates.append(
                    {
                        "theme_family": theme["theme"],
                        "style": style,
                        "palette": palette,
                    }
                )

    weights = [
        apply_feedback_weight(
            feedback_history or [],
            theme_family=candidate["theme_family"],
            style=candidate["style"],
            palette=candidate["palette"],
        )
        for candidate in candidates
    ]

    return random.choices(candidates, weights=weights, k=1)[0]


def _build_image_prompt(
    *,
    intensity: str,
    streak_count: int,
    task_descriptions: list[str],
    user_prefs: dict[str, Any] | None = None,
    sensitive_task: bool = False,
    feedback_history: list[dict[str, Any]] | None = None,
    selection: dict[str, str] | None = None,
) -> str:
    """Build a personalized OpenAI image generation prompt.

    Task descriptions are used to classify task motifs but are NOT copied
    verbatim into the prompt (private data discipline).

    Args:
        intensity: "low", "medium", "high", or "epic"
        streak_count: Current streak count
        task_descriptions: List of completed task descriptions (private — not embedded in prompt)
        user_prefs: Optional reward preferences dict
        sensitive_task: If True, uses abstract/symbolic themes only
        feedback_history: Optional recent feedback for theme weighting

    Returns:
        Image generation prompt string (does not contain task_title verbatim).
    """
    prefs = user_prefs or {}

    # Theme/style/palette are chosen by _select_theme() so the caller can
    # persist the same values it prompted with onto the manifest row.
    if selection is None:
        selection = _select_theme(
            intensity=intensity,
            sensitive_task=sensitive_task,
            user_prefs=user_prefs,
            feedback_history=feedback_history,
        )
    theme_family = selection["theme_family"]
    style = selection["style"]
    palette = selection["palette"]

    # Avoid list
    avoid_str = ""
    if prefs.get("avoid"):
        avoid_str = f" Avoid: {', '.join(prefs['avoid'])}."

    # Humor level
    humor = prefs.get("humor_level", "subtle")
    if sensitive_task:
        humor = "subtle"

    # Build streak marker description
    if streak_count == 1:
        streak_str = "one small glowing progress marker"
    else:
        streak_str = f"exactly {streak_count} small glowing progress markers"

    feedback_guidance = _feedback_prompt_guidance(feedback_history or [])
    feedback_str = f" Reward feedback context: {feedback_guidance}" if feedback_guidance else ""

    prompt = (
        f"A {style} artwork in {palette} color palette. "
        f"Theme: {theme_family}. "
        f"Mood: celebratory, uplifting, {humor} energy.{feedback_str} "
        f"Include {streak_str} subtly integrated into the composition. "
        f"Professional quality, no text, no words, no letters.{avoid_str} "
        f"High resolution, clean composition."
    )

    return prompt


def _feedback_prompt_guidance(feedback_history: list[dict[str, Any]]) -> str:
    """Return short prompt guidance from recent reward feedback."""
    if len(feedback_history) < 3:
        return ""

    positive_count = sum(1 for item in feedback_history if item.get("score", 0) > 0)
    negative_count = sum(1 for item in feedback_history if item.get("score", 0) < 0)

    if positive_count > negative_count:
        return (
            "User has positively responded to recent rewards; "
            "lean energetic and celebratory."
        )
    if negative_count > positive_count:
        return (
            "User has given mixed/negative feedback recently; "
            "be a bit more subdued."
        )
    return ""


async def generate_reward_image(
    *,
    intensity: str,
    streak_count: int,
    task_descriptions: list[str],
    work_type: str = "",
    energy_level: str = "",
    sensitive_task: bool = False,
    user_prefs: dict[str, Any] | None = None,
    feedback_history: list[dict[str, Any]] | None = None,
) -> ImageGeneration | None:
    """Generate an AI celebration image via OpenAI gpt-image-1.

    Implements docs/reward-system.md: AI-Generated Celebration Images.

    Private data discipline:
    - task_descriptions are used for motif classification only, never embedded verbatim
    - Generated images stored under reward_artifacts volume (env REWARD_ARTIFACTS_DIR)
    - The returned PNG path is private — never log it

    Args:
        intensity: "low", "medium", "high", or "epic"
        streak_count: Current streak count (must equal len(task_descriptions))
        task_descriptions: Completed task descriptions (private — classified, not copied)
        work_type: Optional work type hint
        energy_level: Optional energy level hint
        sensitive_task: If True, uses abstract imagery only
        user_prefs: Optional user reward preferences
        feedback_history: Optional feedback list for theme weighting

    Returns:
        An ImageGeneration (PNG path plus the theme/style/palette used), or
        None on failure. Callers treat None as "fall back to emoji/text".
    """
    if not os.environ.get("OPENAI_API_KEY"):
        log.debug("generate_reward_image.no_api_key")
        return None

    if intensity == "lightest":
        # Lightest tier doesn't get image rewards
        return None

    # Validate inputs
    if streak_count < 1:
        log.warning("generate_reward_image.invalid_streak_count", streak_count=streak_count)
        streak_count = 1
    if len(task_descriptions) != streak_count:
        log.warning(
            "generate_reward_image.description_count_mismatch",
            streak_count=streak_count,
            description_count=len(task_descriptions),
        )
        # Truncate or pad to match streak
        task_descriptions = task_descriptions[:streak_count]

    # Blank descriptions are tolerated, not fatal. _build_image_prompt() never
    # embeds task text (private data discipline) — the prompt is built from
    # intensity, theme/style/palette, streak count, and user prefs alone — so a
    # task that reaches us without a usable title still earns its image. Bailing
    # here degraded every such completion to the text fallback while the guard
    # protected nothing. See tests/regressions/bug_0632_reward_image_blank_title.
    task_descriptions = [d for d in task_descriptions if d.strip()]
    if not task_descriptions:
        log.info("generate_reward_image.no_usable_descriptions")

    _img_gen_start = time.monotonic()
    log.info("image_gen.start", intensity=intensity, streak_count=streak_count)

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

        selection = _select_theme(
            intensity=intensity,
            sensitive_task=sensitive_task,
            user_prefs=user_prefs,
            feedback_history=feedback_history,
        )

        prompt = _build_image_prompt(
            intensity=intensity,
            streak_count=streak_count,
            task_descriptions=task_descriptions,
            user_prefs=user_prefs,
            sensitive_task=sensitive_task,
            feedback_history=feedback_history,
            selection=selection,
        )

        quality: Literal["high", "auto"] = "high" if intensity == "epic" else "auto"

        # NOTE: do not pass response_format here. gpt-image-1 rejects it (400)
        # and always returns base64. Because failures below degrade silently to
        # the emoji fallback, an unsupported parameter looks like "images just
        # never arrive" rather than an error. See test_image_generate_call_params.
        response = await client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality=quality,
            n=1,
        )

        if not response.data:
            log.warning("generate_reward_image.empty_response")
            return None

        image_data = response.data[0].b64_json
        if not image_data:
            log.warning("generate_reward_image.empty_response")
            return None

        # Save to artifact path
        artifacts_dir = Path(
            os.environ.get("REWARD_ARTIFACTS_DIR", "/tmp/reward_artifacts")
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        filename = f"{timestamp}_{intensity}.png"
        output_path = artifacts_dir / filename

        import base64
        output_path.write_bytes(base64.b64decode(image_data))

        _img_gen_duration_ms = (time.monotonic() - _img_gen_start) * 1000.0
        log.info(
            "image_gen.end",
            intensity=intensity,
            duration_ms=_img_gen_duration_ms,
            # task_descriptions / prompt intentionally not logged — private data
        )
        return ImageGeneration(
            path=str(output_path),
            theme_family=selection["theme_family"],
            style=selection["style"],
            palette=selection["palette"],
        )

    except Exception:
        _img_gen_duration_ms = (time.monotonic() - _img_gen_start) * 1000.0
        log.exception(
            "generate_reward_image.failed",
            intensity=intensity,
            duration_ms=_img_gen_duration_ms,
        )
        return None


# ---------------------------------------------------------------------------
# Weekly recap
# docs/reward-system.md: Weekly Recap section
# ---------------------------------------------------------------------------

async def generate_weekly_recap(
    *,
    peer: str,
    days_back: int = 7,
    artifacts_dir: str | None = None,
) -> str | None:
    """Compile reward images from the past week into a summary.

    v1 implementation: finds PNG files from the past N days in the artifacts dir
    and returns a text summary (video compilation deferred to v1.1).

    Args:
        peer: E.164 peer identifier.
        days_back: Number of days to look back (default 7).
        artifacts_dir: Override path to reward artifacts directory.

    Returns:
        Path to generated recap file, or None if no images available.
    """
    dir_path = Path(artifacts_dir or os.environ.get("REWARD_ARTIFACTS_DIR", "/tmp/reward_artifacts"))

    if not dir_path.is_dir():
        log.info("generate_weekly_recap.no_artifacts_dir", peer=peer)
        return None

    cutoff = datetime.now(UTC).timestamp() - (days_back * 86400)
    images = [
        p for p in dir_path.glob("*.png")
        if p.stat().st_mtime >= cutoff
    ]

    if not images:
        log.info("generate_weekly_recap.no_images", peer=peer, days_back=days_back)
        return None

    # v1: generate a text recap (video compilation in v1.1)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
    recap_path = dir_path / f"weekly-recap-{timestamp}.txt"
    recap_path.write_text(
        f"Weekly recap: {len(images)} completions in the past {days_back} days. Keep it up!\n",
        encoding="utf-8",
    )

    log.info("generate_weekly_recap.done", peer=peer, image_count=len(images))
    return str(recap_path)


# ---------------------------------------------------------------------------
# Feedback weighting
# docs/reward-system.md:361-445
# ---------------------------------------------------------------------------

# How far back load_feedback_history() reads, and how fast a rating inside that
# window loses influence. These two must stay consistent: a window wider than
# the decay reach loads rows that can never matter, and a decay reach wider than
# the window silently truncates ratings that should still count. Exponential
# decay has no hard edge, so the window is the only cutoff.
_FEEDBACK_WINDOW_DAYS = 90
_FEEDBACK_HALF_LIFE_DAYS = 45.0


def _feedback_decay(age_days: float) -> float:
    """Return the influence multiplier for a rating `age_days` old.

    Exponential with a 45-day half-life: a rating is worth 1.0 the day it is
    given, 0.5 at 45 days, and 0.25 at the 90-day edge of the load window.
    Negative ages (clock skew between the DB and this process) clamp to 0 so a
    future-dated row can never outweigh a fresh one.
    """
    return float(0.5 ** (max(age_days, 0.0) / _FEEDBACK_HALF_LIFE_DAYS))


def apply_feedback_weight(
    feedback_history: list[dict[str, Any]],
    theme_family: str,
    style: str,
    palette: str,
) -> float:
    """Compute a selection weight for a theme/style/palette based on feedback history.

    Implements bounded feedback weighting: reaction influence halves every 45
    days, aggregate weights are capped, result is a nudge not a dictate.

    Args:
        feedback_history: List of dicts with keys: score (int), theme_family (str),
            style (str), palette (str), timestamp (str ISO 8601).
        theme_family: Theme family to compute weight for.
        style: Style to compute weight for.
        palette: Palette to compute weight for.

    Returns:
        Weight float between 0.5 and 1.5. 1.0 is neutral.
        >1.0 means positive bias, <1.0 means negative bias.
        The total nudge is capped at +/-0.5, matching the bounded-feedback
        contract in docs/reward-system.md: feedback biases selection but can
        never eliminate a theme.
    """
    if not feedback_history:
        return 1.0

    now = datetime.now(UTC)
    total_nudge = 0.0

    for entry in feedback_history:
        score = entry.get("score", 0)
        entry_theme = entry.get("theme_family", "")
        entry_style = entry.get("style", "")
        entry_palette = entry.get("palette", "")
        entry_ts_str = entry.get("timestamp", "")

        # Check relevance
        match_weight = 0.0
        if entry_theme == theme_family:
            match_weight += 0.6
        if entry_style == style:
            match_weight += 0.3
        if entry_palette == palette:
            match_weight += 0.1

        if match_weight == 0:
            continue

        # Time decay: influence halves every _FEEDBACK_HALF_LIFE_DAYS.
        try:
            entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
            age_days = (now - entry_ts).total_seconds() / 86400.0
        except (ValueError, TypeError, AttributeError):
            # Unparseable timestamp: treat as maximally old rather than dropping
            # it, so a malformed row degrades to negligible influence.
            age_days = float(_FEEDBACK_WINDOW_DAYS)

        decay = _feedback_decay(age_days)

        # Contribution: score ∈ {-1, 0, 1} × match × decay
        contribution = score * match_weight * decay
        total_nudge += contribution

    # Cap total nudge at ±0.5 (so weight stays in [0.5, 1.5])
    total_nudge = max(-0.5, min(0.5, total_nudge))
    return 1.0 + total_nudge


# ---------------------------------------------------------------------------
# Signal-reaction feedback: emoji-to-score mapping + record_reward_feedback()
# docs/reward-system.md: Feedback Loop section
# ---------------------------------------------------------------------------

_FEEDBACK_EMOJI_SCORES: dict[str, int] = {
    "👍": +1, "❤️": +1, "🎉": +1, "🔥": +1, "😍": +1, "💯": +1,
    "👎": -1, "😞": -1, "😕": -1, "💔": -1,
    # Unknown emojis map to 0 — neutral acknowledgment; no positive/negative signal.
}


async def record_reward_feedback(
    *,
    peer: str,
    emoji: str,
    target_sent_timestamp: int,
    match_window_seconds: int = 30,
) -> bool:
    """Record user feedback on a recent reward via Signal reaction.

    Looks up the closest reward_manifests row for this peer where delivered_at
    is within `match_window_seconds` of the reaction's target timestamp.
    Updates feedback_score, feedback_emoji, and feedback_at.

    Returns True if a matching reward was found and updated, False if no
    match (e.g., reaction on a non-reward message, or outside the window).

    Idempotency: the `feedback_at IS NULL` filter prevents double-counting.
    If the user reacts twice to the same reward, only the first reaction
    counts. A later reaction can still match a different (older) reward
    within the window.

    Unknown emojis receive score 0 — still recorded as an acknowledgment
    but carry no positive/negative training signal.

    Privacy: peer is used only as a DB filter key. Emoji recipient, task
    title, and message body are never logged.
    """
    from app.tools.db import get_db_conn

    # signal-cli timestamps are milliseconds-since-epoch; convert to datetime.
    target_dt = datetime.fromtimestamp(target_sent_timestamp / 1000.0, tz=UTC)
    score = _FEEDBACK_EMOJI_SCORES.get(emoji, 0)

    try:
        async with get_db_conn() as conn:
            # Find the closest unrated reward for this peer within the tight window.
            # Uses reward_manifests_peer_delivered_idx for efficiency.
            cur = await conn.execute(
                """
                SELECT id
                FROM reward_manifests
                WHERE peer = %s
                  AND delivered_at BETWEEN (%s - (%s * interval '1 second'))
                                      AND (%s + (%s * interval '1 second'))
                  AND feedback_at IS NULL
                ORDER BY ABS(EXTRACT(EPOCH FROM (delivered_at - %s))) ASC
                LIMIT 1
                """,
                (
                    peer,
                    target_dt,
                    match_window_seconds,
                    target_dt,
                    match_window_seconds,
                    target_dt,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                log.debug("record_reward_feedback.no_match")
                return False

            manifest_id = row["id"]
            await conn.execute(
                """
                UPDATE reward_manifests
                SET feedback_score = %s,
                    feedback_emoji = %s,
                    feedback_at    = now()
                WHERE id = %s
                """,
                (score, emoji, manifest_id),
            )

        # Log only the score (integer), never the emoji text or any task data.
        log.info("record_reward_feedback.ok", feedback_score=score)
        return True

    except Exception:
        log.exception("record_reward_feedback.failed")
        return False


async def load_feedback_history(
    peer: str, days: int = _FEEDBACK_WINDOW_DAYS
) -> list[dict[str, Any]]:
    """Load recent reward feedback for prompt personalization.

    The default window is _FEEDBACK_WINDOW_DAYS so it cannot drift away from the
    decay curve in apply_feedback_weight().

    Returns an empty list on DB failure so reward delivery can proceed with
    neutral generation.
    """
    from app.tools.db import get_db_conn

    try:
        async with get_db_conn() as conn:
            cur = await conn.execute(
                """
                SELECT feedback_score, feedback_emoji, feedback_at, intensity, reward_kind,
                       theme_family, style, palette
                FROM reward_manifests
                WHERE peer = %s
                  AND feedback_at IS NOT NULL
                  AND feedback_at >= now() - (%s * interval '1 day')
                ORDER BY feedback_at DESC
                """,
                (peer, days),
            )
            rows = await cur.fetchall()

        history: list[dict[str, Any]] = []
        for row in rows:
            feedback_at = row["feedback_at"]
            timestamp = (
                feedback_at.isoformat()
                if isinstance(feedback_at, datetime)
                else str(feedback_at)
            )
            history.append(
                {
                    "score": row["feedback_score"],
                    "emoji": row["feedback_emoji"],
                    "timestamp": timestamp,
                    "intensity": row["intensity"],
                    "reward_kind": row["reward_kind"],
                    # Visual descriptors apply_feedback_weight() matches on.
                    # NULL for emoji-only rewards and for rows written before
                    # migration 0011 — coerced to "" so they simply never match.
                    "theme_family": row["theme_family"] or "",
                    "style": row["style"] or "",
                    "palette": row["palette"] or "",
                }
            )
        return history

    except Exception:
        log.warning("load_feedback_history.failed")
        return []


# ---------------------------------------------------------------------------
# Manifest writing
# docs/reward-system.md: Feedback Loop section
# ---------------------------------------------------------------------------

async def write_reward_manifest(
    *,
    peer: str,
    notion_page_id: str,
    task_title: str,
    reward_kind: str,
    intensity: str,
    streak_count: int,
    delivered_at: datetime,
    artifact_path: str | None = None,
    sensitive_task: bool = False,
    theme_family: str | None = None,
    style: str | None = None,
    palette: str | None = None,
) -> uuid.UUID | None:
    """Write a reward delivery record to the reward_manifests Postgres table.

    Private data discipline:
    - task_title is stored in Postgres (private column) but NEVER written to logs
    - artifact_path is a local filesystem path, never committed to repo

    Returns the UUID of the inserted manifest row, or None on failure.
    """
    from app.tools.db import get_db_conn

    manifest_id = uuid.uuid4()
    try:
        async with get_db_conn() as conn:
            await conn.execute(
                """
                INSERT INTO reward_manifests
                  (id, peer, notion_page_id, task_title, reward_kind, intensity,
                   streak_count, delivered_at, artifact_path, sensitive_task,
                   theme_family, style, palette)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(manifest_id),
                    peer,
                    notion_page_id,
                    task_title,  # Private — stored in DB, never logged
                    reward_kind,
                    intensity,
                    streak_count,
                    delivered_at,
                    artifact_path,
                    sensitive_task,
                    theme_family,
                    style,
                    palette,
                ),
            )
        # Log without task_title — private data discipline
        log.info(
            "write_reward_manifest.ok",
            manifest_id=str(manifest_id),
            peer=peer,
            notion_page_id=notion_page_id,
            intensity=intensity,
            # task_title intentionally omitted
        )
        return manifest_id
    except Exception:
        log.exception(
            "write_reward_manifest.failed",
            peer=peer,
            notion_page_id=notion_page_id,
        )
        return None


# ---------------------------------------------------------------------------
# Main entry point: maybe_reward()
# Called by COMPLETE node
# ---------------------------------------------------------------------------

async def maybe_reward(
    *,
    peer: str,
    task_title: str,
    notion_page_id: str,
    streak: int,
    work_type: str = "",
    energy_required: str = "",
    time_estimate: int = 30,
    is_parent_complete: bool = False,
    is_all_cleared: bool = False,
    rewards_in_last_hour: int = 0,
    user_prefs: dict[str, Any] | None = None,
) -> RewardResult:
    """Generate and deliver a complete reward for a task completion.

    Implements docs/reward-system.md: Completion Flow Enhancement.
    Private data discipline: task_title is passed through but never logged.

    Args:
        peer: E.164 recipient phone number.
        task_title: Completed task title (PRIVATE — never log).
        notion_page_id: Notion page ID of the completed task.
        streak: Post-completion streak count.
        work_type: Task work type.
        energy_required: Task energy level.
        time_estimate: Task time estimate in minutes.
        is_parent_complete: True if all sub-tasks of a parent are done.
        is_all_cleared: True if all tasks cleared.
        rewards_in_last_hour: Recent reward count for diminishing returns.
        user_prefs: Optional user reward preferences.

    Returns:
        RewardResult with text (celebration message) and attachment_path (PNG
        path or None). attachment_path is private — never log it.
    """
    # Classify sensitive task
    sensitive = is_sensitive_task(task_title)

    # Compute intensity
    intensity_label, score = compute_intensity(
        time_estimate=time_estimate,
        energy_required=energy_required,
        streak=streak,
        is_parent_complete=is_parent_complete,
        is_all_cleared=is_all_cleared,
        rewards_in_last_hour=rewards_in_last_hour,
        trigger="completion",
    )

    # Get emoji celebration
    celebration_text = get_celebration_emoji(intensity_label, sensitive_task=sensitive)

    # Attempt image generation
    image: ImageGeneration | None = None
    if intensity_label != "lightest" and not sensitive:
        prefs = (user_prefs or {}).get("rewards") if user_prefs else None
        try:
            feedback_history = await load_feedback_history(peer)
        except Exception:
            log.warning("maybe_reward.feedback_history_failed")
            feedback_history = []
        image = await generate_reward_image(
            intensity=intensity_label,
            streak_count=1,  # Only current task available; intensity score still uses full streak
            task_descriptions=[task_title],  # Private — classified, not embedded in prompt
            work_type=work_type,
            energy_level=energy_required.lower(),
            sensitive_task=sensitive,
            user_prefs=prefs,
            feedback_history=feedback_history,
        )
    elif sensitive:
        # Sensitive: muted emoji only (no image)
        image = None

    # Fallback if image gen failed
    reward_kind = "emoji"
    if image:
        reward_kind = "emoji+image"
    elif intensity_label in ("medium", "high", "epic") and not sensitive:
        # Image was expected but failed — add fallback
        fallback = get_fallback_reward()
        celebration_text = f"{celebration_text}\n{fallback}"
        reward_kind = "image_fallback"

    # Write manifest (non-blocking — failure doesn't break reward delivery)
    delivered_at = datetime.now(UTC)
    try:
        await write_reward_manifest(
            peer=peer,
            notion_page_id=notion_page_id,
            task_title=task_title,  # Private — stored in Postgres only
            reward_kind=reward_kind,
            intensity=intensity_label,
            streak_count=streak,
            delivered_at=delivered_at,
            artifact_path=image["path"] if image else None,
            sensitive_task=sensitive,
            # Persisted so a later emoji reaction on this message can be
            # attributed to these visual choices — this is what closes the
            # feedback loop for apply_feedback_weight().
            theme_family=image["theme_family"] if image else None,
            style=image["style"] if image else None,
            palette=image["palette"] if image else None,
        )
    except Exception:
        log.exception(
            "maybe_reward.manifest_failed",
            peer=peer,
            notion_page_id=notion_page_id,
            # task_title intentionally omitted
        )

    log.info(
        "maybe_reward.delivered",
        peer=peer,
        notion_page_id=notion_page_id,
        intensity=intensity_label,
        streak=streak,
        reward_kind=reward_kind,
        sensitive=sensitive,
        # task_title intentionally omitted — private data
        # image path intentionally omitted — private data
    )

    return RewardResult(
        text=celebration_text,
        attachment_path=image["path"] if image else None,
    )
