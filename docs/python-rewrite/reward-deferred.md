# Reward Subsystem — Deferred Features

This document lists reward subsystem features deferred from v1 (Phase B) to v1.1,
with references to the spec sections that define their intended behavior.

## Deferred: Audio Rewards (Home Audio Integration)

**Spec section:** `docs/reward-system.md` — "Music Playback (Home Audio Integration)"

What is deferred:
- Playback of user-selected victory songs or ambient soundscapes via home audio system
- Integration with home audio controller (e.g., Apple Home, Sonos, Home Assistant)
- `audio_enabled` configuration toggle in the reward delivery settings schema

Why deferred:
- Requires home automation environment variable (`HOME_AUDIO_ENDPOINT`) not available
  in Phase B scope.
- Infrastructure for routing audio commands to user's home speaker setup is not
  included in the Python rewrite infra stack.

## Deferred: Outing Suggestions (Interest-Aligned Treats)

**Spec section:** `docs/reward-system.md` — "Outing Suggestions"

What is deferred:
- Generation of location-specific outing recommendations (coffee shop, park, restaurant)
  correlated to user's task completion mood and local weather.
- `OPENWEATHER_API_KEY` integration for weather-aware outing filtering.
- `outing_enabled` configuration toggle.

Why deferred:
- Requires weather API key (`OPENWEATHER_API_KEY`) and geolocation data not available
  in Phase B scope.
- Outing suggestion ranking and interest-alignment inference adds scope beyond the
  Phase B intent port milestone.

## Deferred: Weekly Recap Video Compilation

**Spec section:** `docs/reward-system.md` — "AI-Generated Celebration Images"
(subsection on `scripts/generate-weekly-recap.sh` compilation output)

What is deferred:
- MP4 card-flip video compilation of the week's reward images (~40 seconds, 12 images).
- `generate_weekly_recap()` currently emits a text summary listing image count.
- Full video compilation requires `ffmpeg` or equivalent video pipeline not in Phase B scope.

v1.1 plan: replace text summary in `generate_weekly_recap()` with ffmpeg-based
compilation when the video pipeline is available.

## What is in scope for v1

The following are fully implemented in Phase B:

- Emoji rewards (intensity-mapped: lightest/low/medium/high/epic)
- AI-generated celebration images via OpenAI `gpt-image-1` (`generate_reward_image()`)
- Sensitive-task guardrails (metaphorical/abstract imagery, muted emoji)
- Feedback weighting with 90-day load window, 45-day decay half-life, per-axis nudge caps (theme ±0.25, style/palette ±0.50), and `_SELECTION_EPSILON` probability floor
- Fallback rewards (pool of 12 real-life suggestions) when image gen fails
- `reward_manifests` Postgres table for delivery records (private `task_title` column)
- `maybe_reward()` as the COMPLETE node's single entry point
