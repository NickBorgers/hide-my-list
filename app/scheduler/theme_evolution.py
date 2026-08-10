"""Grow and prune each peer's reward descriptor vocabulary.

The vocabulary in `reward_theme_pool` starts as a copy of the seed constants.
This job is what makes it drift: descriptors the user consistently reacts badly
to are retired, and new ones are proposed from the qualities of the ones they
react well to. Over a season the vocabulary a peer draws from stops being the
one that shipped in the repo.

Design constraints:

Scheduled, not inline. Image generation already spends 10-20s waiting on the
image API; adding an LLM round trip to that path would slow every reward and
let an evolution failure degrade a delivery. The decisions here depend on
aggregates that move over weeks.

Evidence-gated and rate-limited. Early on there is very little data, so this is
closer to guided vocabulary expansion than to preference learning — which is
fine, because novelty is the product goal and learned taste is the bonus. The
gate and the per-run caps keep the vocabulary from outrunning the evidence.

No private data reaches the model. The prompt is built from descriptor strings
and integer counts only — never task titles, peers, emoji, timestamps, or
artifact paths. This mirrors the discipline _build_image_prompt already
enforces.

Nothing here can widen the sensitive-task path: that path reads a code
constant and never touches reward_theme_pool.

Retirement removes descriptors from selection, and that is intended. The
_SELECTION_EPSILON floor in app/tools/rewards.py guarantees no active
descriptor can be driven to zero probability by feedback; it says nothing about
which descriptors stay active, which is this job's decision. Keeping a
consistently disliked descriptor reachable forever would spend real rewards on
images that do not land in exchange for combinations nobody wants. Novelty is
protected instead by the bounds above: additions outpace retirements, the
per-axis floors are hard, and retirement is a soft delete that keeps rating
attribution and can be reversed. See "Vocabulary Evolution" in
docs/reward-system.md.
"""
from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Ratings needed since the last run before proposing anything. Below this the
# job logs and returns: a vocabulary grown on four reactions is noise.
_MIN_NEW_RATINGS = 12

# Per-run caps. Growth stays slow relative to the rate evidence accumulates,
# and additions outpace retirements two to one so an axis cannot shrink over
# time. Novelty is protected by the size of the active set, not by keeping
# every descriptor in it forever.
_MAX_NEW_PER_AXIS = 2
_MAX_RETIRED_PER_AXIS = 1

# An axis may never shrink below this, whatever the evidence says. This is the
# hard bound on retirement: below the floor the job stops retiring entirely.
_MIN_ACTIVE_PER_AXIS: dict[str, int] = {"theme": 5, "style": 4, "palette": 4}

# Retirement thresholds. All three must hold. Deliberately strict — a value
# only leaves the active set once it has demonstrably been producing images the
# user does not want, since the cost of keeping it is a wasted reward and the
# cost of dropping it is one combination out of hundreds.
_RETIRE_MIN_NEGATIVES = 3
_RETIRE_MAX_SUCCESS_RATE = 0.25
_RETIRE_MIN_AGE_DAYS = 14

# How many descriptors per axis are shown to the model as context.
_PROMPT_EXAMPLES = 6

_AXES = ("theme", "style", "palette")
_AXIS_TO_MANIFEST_COLUMN = {
    "theme": "theme_family",
    "style": "style",
    "palette": "palette",
}

_SYSTEM_PROMPT = """\
You expand a vocabulary of art descriptors used to generate abstract \
celebration artwork. You never see, and never need, the subject matter being \
celebrated.

You are given descriptors that have been received well and badly. Propose new \
ones that share the qualities of the well-received entries and avoid the \
qualities of the poorly-received ones.

Rules for every value you return:
- 2 to 8 words, lowercase, describing visual art qualities only
- no people, no named individuals, no brands
- no text, letters, words, logos, captions, or signatures in the artwork
- no medical, legal, financial, therapeutic, or clinical imagery
- no punctuation other than commas, apostrophes, and hyphens
- must not duplicate any value already listed

Return only a JSON object of this exact shape, with no commentary:
{"styles": [], "palettes": [], "themes": []}\
"""


def _format_counts(entries: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{entry['value']} ({entry['positives']} up / {entry['negatives']} down)"
        for entry in entries
    ) or "none yet"


def _build_evolution_prompt(
    *,
    stats: dict[str, list[dict[str, Any]]],
    active: dict[str, list[str]],
    intensity: str,
) -> str:
    """Build the proposal prompt from aggregate descriptors and counts only.

    Everything in here is a descriptor string or an integer. No task titles, no
    peer, no emoji, no timestamps, no paths.
    """
    def top(axis: str, *, positive: bool) -> list[dict[str, Any]]:
        entries = [
            entry
            for entry in stats.get(axis, [])
            if (entry["positives"] > entry["negatives"])
            == positive
            and entry["positives"] != entry["negatives"]
        ]
        entries.sort(key=lambda e: e["positives"] - e["negatives"], reverse=positive)
        return entries[:_PROMPT_EXAMPLES]

    lines = [
        f"Well-received styles: {_format_counts(top('style', positive=True))}",
        f"Poorly-received styles: {_format_counts(top('style', positive=False))}",
        f"Well-received palettes: {_format_counts(top('palette', positive=True))}",
        f"Poorly-received palettes: {_format_counts(top('palette', positive=False))}",
        f'Existing "{intensity}" tier themes: '
        + ("; ".join(active.get("theme", [])) or "none yet"),
        f"Existing styles: {'; '.join(active.get('style', [])) or 'none yet'}",
        f"Existing palettes: {'; '.join(active.get('palette', [])) or 'none yet'}",
        "",
        f"Propose up to {_MAX_NEW_PER_AXIS} new art styles, "
        f"{_MAX_NEW_PER_AXIS} new color palettes, and "
        f'{_MAX_NEW_PER_AXIS} new theme subjects for the "{intensity}" tier.',
    ]
    return "\n".join(lines)


def _parse_proposals(raw: str) -> dict[str, list[str]]:
    """Parse the model's JSON reply, tolerating fenced or padded output."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}

    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {}

    if not isinstance(parsed, dict):
        return {}

    out: dict[str, list[str]] = {}
    for axis, key in (("style", "styles"), ("palette", "palettes"), ("theme", "themes")):
        values = parsed.get(key)
        if isinstance(values, list):
            out[axis] = [v for v in values if isinstance(v, str)]
    return out


async def _load_descriptor_stats(
    peer: str, *, intensity: str
) -> dict[str, list[dict[str, Any]]]:
    """Decayed-free rating counts per descriptor, per axis, from the manifests.

    Only values present in the peer's active reward_theme_pool (origin 'seed'
    or 'evolved') are included. This prevents user-authored preference text
    that landed in reward_manifests from reaching the model prompt.

    Raw counts rather than the decayed weights selection uses: this job cares
    about whether a descriptor has ever earned its place, on a slower clock
    than a single reward draw.
    """
    from app.tools.db import get_db_conn
    from app.tools.rewards import _FEEDBACK_WINDOW_DAYS

    stats: dict[str, list[dict[str, Any]]] = {axis: [] for axis in _AXES}

    async with get_db_conn() as conn:
        for axis in _AXES:
            column = _AXIS_TO_MANIFEST_COLUMN[axis]
            if axis == "theme":
                cur = await conn.execute(
                    f"""
                    SELECT rm.{column} AS value,
                           count(*) FILTER (WHERE rm.feedback_score > 0) AS positives,
                           count(*) FILTER (WHERE rm.feedback_score < 0) AS negatives
                    FROM reward_manifests rm
                    JOIN reward_theme_pool rtp
                      ON rtp.peer = rm.peer
                      AND rtp.axis = %s
                      AND rtp.value = rm.{column}
                      AND rtp.retired_at IS NULL
                      AND rtp.origin IN ('seed', 'evolved')
                      AND rtp.intensity = %s
                    WHERE rm.peer = %s
                      AND rm.feedback_at IS NOT NULL
                      AND rm.feedback_at >= now() - (%s * interval '1 day')
                      AND rm.{column} IS NOT NULL
                    GROUP BY rm.{column}
                    """,  # noqa: S608 - column name comes from a fixed internal map
                    (axis, intensity, peer, _FEEDBACK_WINDOW_DAYS),
                )
            else:
                cur = await conn.execute(
                    f"""
                    SELECT rm.{column} AS value,
                           count(*) FILTER (WHERE rm.feedback_score > 0) AS positives,
                           count(*) FILTER (WHERE rm.feedback_score < 0) AS negatives
                    FROM reward_manifests rm
                    JOIN reward_theme_pool rtp
                      ON rtp.peer = rm.peer
                      AND rtp.axis = %s
                      AND rtp.value = rm.{column}
                      AND rtp.retired_at IS NULL
                      AND rtp.origin IN ('seed', 'evolved')
                    WHERE rm.peer = %s
                      AND rm.feedback_at IS NOT NULL
                      AND rm.feedback_at >= now() - (%s * interval '1 day')
                      AND rm.{column} IS NOT NULL
                    GROUP BY rm.{column}
                    """,  # noqa: S608 - column name comes from a fixed internal map
                    (axis, peer, _FEEDBACK_WINDOW_DAYS),
                )
            for row in await cur.fetchall():
                stats[axis].append(
                    {
                        "value": row["value"],
                        "positives": int(row["positives"]),
                        "negatives": int(row["negatives"]),
                    }
                )
    return stats


async def _count_new_ratings(peer: str) -> int:
    """Ratings recorded since the newest descriptor this peer has."""
    from app.tools.db import get_db_conn

    async with get_db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT count(*) AS n
            FROM reward_manifests
            WHERE peer = %s
              AND feedback_at IS NOT NULL
              AND feedback_at > COALESCE(
                    (SELECT max(created_at) FROM reward_theme_pool
                     WHERE peer = %s AND origin = 'evolved'),
                    'epoch'::timestamptz
                  )
            """,
            (peer, peer),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def _active_values(peer: str, *, intensity: str) -> dict[str, list[str]]:
    from app.tools.db import get_db_conn

    active: dict[str, list[str]] = {axis: [] for axis in _AXES}
    async with get_db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT axis, value FROM reward_theme_pool
            WHERE peer = %s AND retired_at IS NULL
              AND (axis <> 'theme' OR intensity = %s)
            ORDER BY axis, value
            """,
            (peer, intensity),
        )
        for row in await cur.fetchall():
            if row["axis"] in active:
                active[row["axis"]].append(row["value"])
    return active


async def _known_values(peer: str, *, intensity: str) -> dict[str, set[str]]:
    """Every value the peer has on each axis, retired ones included.

    The proposal filter has to dedupe against this rather than against the
    active set. A retired value is absent from `_active_values`, so it passes
    an active-only duplicate check, and then hits the unique index and inserts
    nothing — an accepted-but-not-stored row. Counting that as growth is what
    would let retirement outrun insertion.
    """
    from app.tools.db import get_db_conn

    known: dict[str, set[str]] = {axis: set() for axis in _AXES}
    async with get_db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT axis, value FROM reward_theme_pool
            WHERE peer = %s
              AND (axis <> 'theme' OR intensity = %s)
            """,
            (peer, intensity),
        )
        for row in await cur.fetchall():
            if row["axis"] in known:
                known[row["axis"]].add(row["value"].lower())
    return known


async def _retire_stale_descriptors(
    conn: Any,
    peer: str,
    *,
    stats: dict[str, list[dict[str, Any]]],
    active: dict[str, list[str]],
    intensity: str,
    budget: dict[str, int],
) -> int:
    """Retire consistently-disliked descriptors, never below the axis floor.

    `budget` caps retirements per axis at the number of descriptors actually
    inserted on that axis in this same run. An axis that grew by nothing
    retires nothing, which is what makes "retirement never outpaces growth" a
    property of the code rather than of the per-run constants.

    Runs on the caller's connection so insertion and retirement land in one
    transaction: a failure part-way through rolls back both.
    """
    retired_total = 0
    for axis in _AXES:
        allowed = min(_MAX_RETIRED_PER_AXIS, budget.get(axis, 0))
        if allowed <= 0:
            continue

        floor = _MIN_ACTIVE_PER_AXIS[axis]
        remaining = len(active.get(axis, []))
        if remaining <= floor:
            continue

        candidates = []
        for entry in stats.get(axis, []):
            if entry["value"] not in active.get(axis, []):
                continue
            observed = entry["positives"] + entry["negatives"]
            if entry["negatives"] < _RETIRE_MIN_NEGATIVES or observed == 0:
                continue
            if entry["positives"] / observed > _RETIRE_MAX_SUCCESS_RATE:
                continue
            candidates.append(entry)

        candidates.sort(key=lambda e: e["positives"] - e["negatives"])

        for entry in candidates[:allowed]:
            if remaining <= floor:
                break
            await conn.execute(
                """
                UPDATE reward_theme_pool
                SET retired_at = now()
                WHERE peer = %s AND axis = %s AND value = %s
                  AND retired_at IS NULL
                  AND COALESCE(intensity, '') = COALESCE(%s, '')
                  AND created_at <= now() - (%s * interval '1 day')
                """,
                (
                    peer,
                    axis,
                    entry["value"],
                    intensity if axis == "theme" else None,
                    _RETIRE_MIN_AGE_DAYS,
                ),
            )
            remaining -= 1
            retired_total += 1

    return retired_total


async def _insert_proposals(
    conn: Any,
    peer: str,
    *,
    proposals: dict[str, list[str]],
    known: dict[str, set[str]],
    intensity: str,
) -> dict[str, int]:
    """Sanitize and store accepted proposals; count-only logging for rejects.

    Returns the number of rows inserted per axis. Deduping against `known`
    (every value the peer has, retired included) rather than against the
    active set is what makes those counts exact: no accepted row can lose to
    the unique index, so "accepted" and "stored" cannot diverge. The counts
    then become the retirement budget, so an axis can only lose a descriptor
    on a run where it gained one.

    Runs on the caller's connection; see _retire_stale_descriptors.
    """
    import uuid

    from app.tools.rewards import _sanitize_descriptor

    accepted: list[tuple[uuid.UUID, str, str, str | None, str, str]] = []
    added: dict[str, int] = dict.fromkeys(_AXES, 0)
    rejected = 0

    for axis in _AXES:
        seen = set(known.get(axis, set()))
        kept = 0
        for raw in proposals.get(axis, []):
            if kept >= _MAX_NEW_PER_AXIS:
                break
            cleaned = _sanitize_descriptor(raw)
            if cleaned is None or cleaned.lower() in seen:
                rejected += 1
                continue
            seen.add(cleaned.lower())
            kept += 1
            accepted.append(
                (
                    uuid.uuid4(),
                    peer,
                    axis,
                    intensity if axis == "theme" else None,
                    cleaned,
                    "evolved",
                )
            )
        added[axis] = kept

    if rejected:
        # Count only — a rejected value may be adversarial or derived from
        # user-authored preference text.
        log.info("theme_evolution.rejected", count=rejected)

    for row in accepted:
        await conn.execute(
            """
            INSERT INTO reward_theme_pool
                (id, peer, axis, intensity, value, origin)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (peer, axis, COALESCE(intensity, ''), value) DO NOTHING
            """,
            row,
        )

    return added


async def evolve_peer_vocabulary(peer: str, *, intensity: str = "high") -> dict[str, int]:
    """Run one evolution pass for a single peer.

    Returns a summary of what changed. Never raises: this is a background
    improvement, and a failure must leave the existing vocabulary intact.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.models import llm
    from app.tools.db import get_db_conn

    summary = {"added": 0, "retired": 0}

    try:
        new_ratings = await _count_new_ratings(peer)
        if new_ratings < _MIN_NEW_RATINGS:
            log.info("theme_evolution.gated", new_ratings=new_ratings)
            return summary

        stats = await _load_descriptor_stats(peer, intensity=intensity)
        active = await _active_values(peer, intensity=intensity)
        if not any(active.values()):
            log.info("theme_evolution.no_vocabulary")
            return summary

        # Build proposals BEFORE any mutations so a model failure or empty
        # parse leaves the existing vocabulary completely unchanged.
        prompt = _build_evolution_prompt(stats=stats, active=active, intensity=intensity)
        model = llm("cheap", caller="theme_evolution")
        response = await model.ainvoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=prompt)]
        )
        proposals = _parse_proposals(str(response.content))
        if not proposals:
            log.info("theme_evolution.no_proposals")
            return summary

        known = await _known_values(peer, intensity=intensity)

        # Insert first, then retire within the budget the insertions earned,
        # both on one connection. Order and atomicity are the contract:
        # retiring first would let a run of all-rejected proposals shrink an
        # axis, and two connections would leave retirements committed when
        # insertion fails. Either way the vocabulary could shrink on a run
        # that added nothing, which docs/reward-system.md rules out.
        async with get_db_conn() as conn:
            added = await _insert_proposals(
                conn, peer, proposals=proposals, known=known, intensity=intensity
            )
            summary["retired"] = await _retire_stale_descriptors(
                conn,
                peer,
                stats=stats,
                active=active,
                intensity=intensity,
                budget=added,
            )
        summary["added"] = sum(added.values())
        log.info("theme_evolution.done", **summary)
        return summary

    except Exception:
        log.warning("theme_evolution.failed", exc_info=True)
        return summary


def _authorized_peers() -> list[str]:
    """Peers this deployment serves.

    Reuses the listener's loader so the two cannot disagree about who is
    authorized. It raises when AUTHORIZED_PEERS is unset; here that is caught
    and treated as "no peers to evolve" rather than a scheduler error, since
    the listener has already refused to start in that case.
    """
    from app.ingress.signal_listener import _load_authorized_peers

    try:
        return sorted(_load_authorized_peers())
    except RuntimeError:
        log.warning("theme_evolution.no_authorized_peers")
        return []


async def run_theme_evolution() -> dict[str, int]:
    """Evolve every authorized peer's vocabulary, one intensity per run.

    Rotating the intensity keeps theme growth slow without needing a separate
    schedule per tier; style and palette are intensity-free and so are
    considered on every run.
    """
    from datetime import UTC, datetime

    totals = {"added": 0, "retired": 0, "peers": 0}
    intensities = ("low", "medium", "high", "epic")
    # Deterministic rotation from the ISO week, so successive runs cover
    # different theme tiers without needing stored cursor state.
    intensity = intensities[datetime.now(UTC).isocalendar().week % len(intensities)]

    for peer in _authorized_peers():
        result = await evolve_peer_vocabulary(peer, intensity=intensity)
        totals["added"] += result["added"]
        totals["retired"] += result["retired"]
        totals["peers"] += 1

    log.info("theme_evolution.run_complete", intensity=intensity, **totals)
    return totals
