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
import re
import time
import unicodedata
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

# Theme, style, and palette are drawn independently rather than as welded
# triples. Two reasons:
#
#   Combinations. Five triples per intensity is five reachable images, and
#   habituation is the failure mode the image system exists to prevent. The
#   same strings, drawn independently, reach 5 x 8 x 8 = 320.
#
#   Learning. Welded triples make every attribute exactly as sparse as the
#   rarest one, so no attribute can ever accumulate enough ratings to mean
#   anything. Split apart, one rating updates three parameters, and style and
#   palette pool their observations across all four intensities.
#
# Style and palette are deliberately intensity-agnostic: scoping them per
# intensity would quarter their observation rate, which is the whole reason
# they are the axes that can learn. The theme string and the prompt's mood
# line carry the intensity semantics instead.
_SEED_THEMES: dict[str, list[str]] = {
    "low": [
        "cheerful bird with sparkle",
        "paper airplane soaring through clouds",
        "happy cat in sunbeam",
        "small garden with blooming flowers",
        "cozy reading nook with warm light",
    ],
    "medium": [
        "fox dancing in wildflowers",
        "confetti explosion in bright colors",
        "otter sliding down rainbow waterfall",
        "butterfly emerging from cocoon in golden light",
        "mountain summit with celebration flags",
    ],
    "high": [
        "phoenix rising from golden flames",
        "astronaut planting flag on colorful planet",
        "whale breaching in starfield",
        "ancient temple lit by aurora borealis",
        "eagle soaring above mountain range at dawn",
    ],
    "epic": [
        "galaxy forming crown of light",
        "reality folding into cathedral of light",
        "cosmic phoenix ascending through dimensional portal",
        "universe crystallizing into perfect order",
        "ancient forest where trees become stars",
    ],
}

# Consolidated from the 18 distinct styles the welded triples carried. Fewer,
# broader values on purpose: distinguishing a liked style from a disliked one
# needs roughly 40 ratings for that style, so vocabulary size is a budget, not
# a feature. These eight span the range the original 18 covered.
_SEED_STYLES: list[str] = [
    "watercolor",
    "paper collage",
    "storybook illustration",
    "impressionist painting",
    "bold graphic illustration",
    "cartoon",
    "digital concept art",
    "oil painting",
]

# Consolidated from the 20 distinct palettes the welded triples carried, kept
# to eight for the same reason. Spans warm, cool, deep, and bright.
_SEED_PALETTES: list[str] = [
    "warm pastel",
    "soft blue",
    "nature green",
    "amber gold",
    "jewel tones",
    "cosmic purple",
    "midnight blue",
    "iridescent prism",
]

_SENSITIVE_THEMES: list[dict[str, str]] = [
    {"theme": "abstract geometric pattern expanding outward", "style": "minimalist", "palette": "calm blue-grey"},
    {"theme": "smooth river stones arranged in peaceful pattern", "style": "zen illustration", "palette": "earth tones"},
    {"theme": "gentle light through frosted glass", "style": "abstract", "palette": "soft white"},
    {"theme": "growing seedling in quiet soil", "style": "simple illustration", "palette": "natural green"},
    {"theme": "single candle flame in dark, steady and bright", "style": "minimal", "palette": "warm amber"},
]


# Descriptors reach the image prompt verbatim, and their sources are not
# trustworthy: user-authored preference text today, LLM-proposed values later.
# The character allowlist is the control that actually works — it removes every
# character usable to break out of the "Theme: {x}." framing in
# _build_image_prompt (newlines, colons, braces, brackets, quotes, backticks,
# parentheses). The term lists below are defense in depth and will always be
# incomplete; do not rely on them alone.
_DESCRIPTOR_ALLOWED = re.compile(r"^[a-z0-9 ,'-]+$")
_DESCRIPTOR_MAX_CHARS = 60
_DESCRIPTOR_MAX_WORDS = 8

_BANNED_DESCRIPTOR_TERMS: frozenset[str] = frozenset([
    # Instruction verbs — an attempt to address the image model directly.
    "ignore", "instead", "disregard", "override", "prompt", "system",
    # Text rendering — the prompt already forbids lettering; a descriptor
    # asking for it is either an attack or a guaranteed bad image.
    "text", "word", "letter", "caption", "logo", "watermark", "signature",
    # Identity — celebration art depicts no one in particular.
    "person", "child", "nude", "celebrity",
])


def _sanitize_descriptor(value: Any) -> str | None:
    """Normalize an untrusted theme/style/palette descriptor, or reject it.

    Returns the cleaned descriptor, or None if it fails any check. Callers drop
    rejects silently and log a count only — the value itself may be
    user-authored text and must never reach a log.
    """
    if not isinstance(value, str):
        return None

    normalized = unicodedata.normalize("NFKC", value)

    # Reject line breaks and control characters outright rather than folding
    # them into spaces. Collapsing first would silently rewrite an injection
    # attempt into an accepted descriptor, which is both a weaker defense and
    # a surprising one — the stored value would not be what anyone wrote.
    if any(ch.isspace() and ch != " " for ch in normalized):
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in normalized):
        return None

    cleaned = " ".join(part for part in normalized.split(" ") if part).lower()
    if not cleaned or len(cleaned) > _DESCRIPTOR_MAX_CHARS:
        return None

    words = cleaned.split(" ")
    if len(words) > _DESCRIPTOR_MAX_WORDS:
        return None

    if not _DESCRIPTOR_ALLOWED.match(cleaned):
        return None

    if any(term in cleaned for term in _BANNED_DESCRIPTOR_TERMS):
        return None

    # Sensitive subject matter is handled by a separate locked pool; a
    # preference must not smuggle it into the ordinary path.
    if any(keyword in cleaned for keyword in _SENSITIVE_KEYWORDS):
        return None

    return cleaned


def _sanitized_list(values: Any) -> list[str]:
    """Sanitize a preference list, dropping rejects with a count-only log."""
    if not isinstance(values, list):
        return []

    kept: list[str] = []
    for value in values:
        cleaned = _sanitize_descriptor(value)
        if cleaned is not None and cleaned not in kept:
            kept.append(cleaned)

    rejected = len(values) - len(kept)
    if rejected > 0:
        # Count only. The rejected values are user-authored text.
        log.info("reward_descriptor.rejected", count=rejected)
    return kept


# Selection tuning. See docs/reward-system.md: Weighted Selection.
#
# Per-axis nudge caps, deliberately inverted from the old match weights
# (theme 0.6, style 0.3, palette 0.1). Theme is the highest-cardinality axis
# and the one that rarely repeats, so it is where novelty is spent and where
# feedback should push least. Style and palette repeat constantly, so they are
# where evidence actually accumulates.
_AXIS_NUDGE_CAP: dict[str, float] = {
    "theme_family": 0.25,
    "style": 0.50,
    "palette": 0.50,
}

# Beta(1,1) prior: an unrated value sits at p=0.5, neither favored nor punished.
_FEEDBACK_PRIOR = 1.0
# Confidence half-saturation: a value needs ~3 effective ratings before its
# estimate carries half its potential weight. Keeps one reaction from swinging
# selection.
_FEEDBACK_CONFIDENCE_SCALE = 3.0
# Share of every draw reserved for a uniform pick. This is the novelty floor:
# no active value can fall below _SELECTION_EPSILON / len(vocabulary),
# regardless of how lopsided the feedback gets.
_SELECTION_EPSILON = 0.15
# Multiplier applied to values the user explicitly asked for. Preferences bias
# the draw; they do not replace the vocabulary, because a single stated style
# would otherwise appear on every image and eliminate style novelty outright.
_PREFERENCE_BONUS = 1.5


def _attribute_weight(
    feedback_history: list[dict[str, Any]],
    *,
    axis: str,
    value: str,
) -> float:
    """Weight for one descriptor on one axis, from decayed rating counts.

    Positive and negative ratings accumulate separately with time decay, then
    combine into a Beta-smoothed success rate scaled by how much evidence
    exists. With no ratings the result is exactly 1.0, so an unrated value is
    drawn at the same rate as any other — cold start is a uniform draw.
    """
    positives = 0.0
    negatives = 0.0

    for entry in feedback_history:
        if entry.get(axis, "") != value:
            continue
        score = entry.get("score", 0)
        if not score:
            # Unknown emoji: recorded as acknowledgment, carries no direction.
            continue

        try:
            entry_ts = datetime.fromisoformat(
                str(entry.get("timestamp", "")).replace("Z", "+00:00")
            )
            age_days = (datetime.now(UTC) - entry_ts).total_seconds() / 86400.0
        except (ValueError, TypeError, AttributeError):
            age_days = float(_FEEDBACK_WINDOW_DAYS)

        decay = _feedback_decay(age_days)
        if score > 0:
            positives += decay
        else:
            negatives += decay

    observed = positives + negatives
    if observed == 0:
        return 1.0

    success_rate = (positives + _FEEDBACK_PRIOR) / (observed + 2 * _FEEDBACK_PRIOR)
    confidence = observed / (observed + _FEEDBACK_CONFIDENCE_SCALE)
    cap = _AXIS_NUDGE_CAP[axis]

    return 1.0 + cap * (2 * success_rate - 1) * confidence


def _draw_attribute(
    vocabulary: list[str],
    *,
    axis: str,
    feedback_history: list[dict[str, Any]],
    preferred: frozenset[str] = frozenset(),
) -> str:
    """Draw one descriptor, biased by feedback but never locked to one value.

    Weights are mixed with a uniform distribution at _SELECTION_EPSILON, which
    is what enforces the novelty floor: every value keeps at least
    _SELECTION_EPSILON / len(vocabulary) probability no matter how negative its
    history. docs/reward-system.md treats habituation as the failure mode the
    image system exists to prevent, so no descriptor may ever be excluded.
    """
    weights = []
    for value in vocabulary:
        weight = _attribute_weight(feedback_history, axis=axis, value=value)
        if value in preferred:
            weight *= _PREFERENCE_BONUS
        weights.append(weight)

    total = sum(weights)
    uniform = 1.0 / len(vocabulary)
    if total <= 0:
        probabilities = [uniform] * len(vocabulary)
    else:
        probabilities = [
            (1 - _SELECTION_EPSILON) * (w / total) + _SELECTION_EPSILON * uniform
            for w in weights
        ]

    return random.choices(vocabulary, weights=probabilities, k=1)[0]


def _select_theme(
    *,
    intensity: str,
    sensitive_task: bool = False,
    user_prefs: dict[str, Any] | None = None,
    feedback_history: list[dict[str, Any]] | None = None,
    vocabulary: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Pick a theme/style/palette, biased by prior emoji-reaction feedback.

    Each axis is drawn independently from its own vocabulary, so the reachable
    combinations are the product of the three rather than a fixed list of
    triples. Stated preferences extend a vocabulary and get a bonus; they do
    not replace it, because a single stated style would otherwise appear on
    every image and remove style novelty entirely.

    `vocabulary` is the peer's stored descriptor set when one is available.
    Omitted or None falls back to the seed constants, so selection keeps
    working when the database does not.

    Returns a dict with theme_family / style / palette keys.
    """
    history = feedback_history or []

    if sensitive_task:
        # The guardrail allowlist wins outright: fixed triples, no preferences,
        # no feedback weighting, and — by returning here — no stored vocabulary
        # of any kind. Sensitive rewards cannot be steered by stored content,
        # however that content got there.
        chosen = random.choice(_SENSITIVE_THEMES)
        return {
            "theme_family": chosen["theme"],
            "style": chosen["style"],
            "palette": chosen["palette"],
        }

    prefs = user_prefs or {}
    preferred_styles = _sanitized_list(prefs.get("preferred_styles"))
    preferred_palettes = _sanitized_list(prefs.get("preferred_palettes"))
    favorite_subjects = _sanitized_list(prefs.get("favorite_subjects"))

    stored = vocabulary or {}
    themes = stored.get("theme") or _SEED_THEMES.get(intensity, _SEED_THEMES["low"])
    styles = stored.get("style") or _SEED_STYLES
    palettes = stored.get("palette") or _SEED_PALETTES
    return {
        "theme_family": _draw_attribute(
            _extend(themes, favorite_subjects),
            axis="theme_family",
            feedback_history=history,
            preferred=frozenset(favorite_subjects),
        ),
        "style": _draw_attribute(
            _extend(styles, preferred_styles),
            axis="style",
            feedback_history=history,
            preferred=frozenset(preferred_styles),
        ),
        "palette": _draw_attribute(
            _extend(palettes, preferred_palettes),
            axis="palette",
            feedback_history=history,
            preferred=frozenset(preferred_palettes),
        ),
    }


def _extend(base: list[str], extra: list[str]) -> list[str]:
    """Base vocabulary plus any stated values it does not already contain."""
    return base + [value for value in extra if value not in base]


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
    vocabulary: dict[str, list[str]] | None = None,
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
        vocabulary: Optional stored descriptor vocabulary; falls back to seeds

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
            vocabulary=vocabulary,
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
    """Combined feedback weight for a full theme/style/palette combination.

    The product of the three per-axis weights. Selection itself draws each axis
    separately via _draw_attribute(); this is the whole-combination view, used
    where a single number for a candidate image is wanted.

    Args:
        feedback_history: List of dicts with keys: score (int), theme_family (str),
            style (str), palette (str), timestamp (str ISO 8601).
        theme_family: Theme family to compute weight for.
        style: Style to compute weight for.
        palette: Palette to compute weight for.

    Returns:
        Weight float. 1.0 is neutral, >1.0 positive bias, <1.0 negative bias.
        Bounded by the per-axis caps in _AXIS_NUDGE_CAP and therefore always
        strictly positive: feedback biases selection and can never eliminate a
        combination. The floor that actually guarantees novelty is the
        _SELECTION_EPSILON mixture in _draw_attribute(), which bounds
        probability rather than weight.
    """
    if not feedback_history:
        return 1.0

    return (
        _attribute_weight(feedback_history, axis="theme_family", value=theme_family)
        * _attribute_weight(feedback_history, axis="style", value=style)
        * _attribute_weight(feedback_history, axis="palette", value=palette)
    )


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


async def load_reward_prefs(peer: str) -> dict[str, Any]:
    """Load the peer's reward-image taste profile from Postgres.

    Reads `user_prefs.prefs_json -> 'rewards'` (docs/user-preferences.md:
    Reward Preferences). Returns {} when the peer has no row, has no rewards
    subtree, or when the subtree is not a JSON object.

    Read from Postgres here rather than threaded through LangGraph State on
    purpose: State is the checkpoint unit, so a preferences copy living there
    would be persisted per thread and drift from the table on every edit.
    maybe_reward() already owns one peer-keyed fail-open read
    (load_feedback_history); this is the same shape and adds no new failure
    mode.

    Returns {} on DB failure so reward delivery proceeds with neutral
    generation rather than being blocked by a preferences lookup.

    Privacy: peer is a DB filter key only. Preference contents are never
    logged — they are user-authored free text.
    """
    from app.tools.db import get_db_conn

    try:
        async with get_db_conn() as conn:
            cur = await conn.execute(
                "SELECT prefs_json FROM user_prefs WHERE peer = %s",
                (peer,),
            )
            row = await cur.fetchone()

        if row is None:
            return {}

        prefs_json = row["prefs_json"]
        if not isinstance(prefs_json, dict):
            # Column is NOT NULL DEFAULT '{}', but a scalar or array JSON value
            # would still satisfy jsonb. Treat anything non-object as absent.
            return {}

        rewards = prefs_json.get("rewards")
        if not isinstance(rewards, dict):
            return {}

        return rewards

    except Exception:
        # Count-free, content-free: the failure is what matters, not the value.
        log.warning("load_reward_prefs.failed")
        return {}


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
        user_prefs: Optional user preferences. When omitted, the peer's stored
            taste profile is loaded from Postgres via load_reward_prefs().

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
        # An explicit user_prefs argument wins; otherwise fall back to the
        # stored taste profile. Callers in the graph do not carry preferences,
        # so in production this is the path that runs.
        prefs: dict[str, Any] | None
        if user_prefs:
            prefs = user_prefs.get("rewards")
        else:
            try:
                prefs = await load_reward_prefs(peer)
            except Exception:
                # load_reward_prefs already fails open; this guards the call
                # itself so a preferences lookup can never block a reward.
                log.warning("maybe_reward.reward_prefs_failed")
                prefs = None
        try:
            feedback_history = await load_feedback_history(peer)
        except Exception:
            log.warning("maybe_reward.feedback_history_failed")
            feedback_history = []

        # The stored vocabulary is an enhancement, not a precondition: any
        # failure degrades to the seed constants rather than to no image.
        vocabulary: dict[str, list[str]] | None = None
        try:
            from app.tools.reward_pool import load_vocabulary

            vocabulary = await load_vocabulary(peer, intensity=intensity_label)
        except Exception:
            log.warning("maybe_reward.vocabulary_failed")

        image = await generate_reward_image(
            intensity=intensity_label,
            streak_count=1,  # Only current task available; intensity score still uses full streak
            task_descriptions=[task_title],  # Private — classified, not embedded in prompt
            work_type=work_type,
            energy_level=energy_required.lower(),
            sensitive_task=sensitive,
            user_prefs=prefs,
            feedback_history=feedback_history,
            vocabulary=vocabulary,
        )

        if image is not None and vocabulary is not None:
            # Diagnostic counters only, and already-delivered work: never let
            # this fail a reward that has been generated.
            try:
                from app.tools.reward_pool import record_use

                await record_use(
                    peer,
                    selection={
                        "theme_family": image["theme_family"],
                        "style": image["style"],
                        "palette": image["palette"],
                    },
                    intensity=intensity_label,
                )
            except Exception:
                log.debug("maybe_reward.record_use_failed")
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
