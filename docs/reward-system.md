---
layout: default
title: Reward System
---

# Reward System

## Overview

Reward system = core hide-my-list component. Dopamine-inducing positive reinforcement on task completion/progress. Multiple reward channels — system-generated + interpersonal — create motivation loop.

## Reward Philosophy

```mermaid
mindmap
  root((Dopamine<br/>Delivery))
    System Generated
      Visual Celebration
        Emoji explosions
        AI-generated images
        AI-generated videos
      Audio Celebration
        Favorite songs
        Victory sounds
        Home audio integration
    Interpersonal
      Significant Other
        Completion texts
        Progress updates
        Shared victories
      Self-Reward
        Outing suggestions
        Interest-aligned treats
        Break recommendations
```

Principle: **completing tasks should feel genuinely rewarding**. Achieved via:

1. **Immediate gratification** - Instant visual/audio feedback
2. **Social reinforcement** - Loved ones acknowledge achievements
3. **Anticipatory pleasure** - Suggestions for enjoyable activities

### Shame-Safe Reward Principles

> **Shame Prevention:** Reward system must never create implicit comparison between "good" sessions (many completions) and "bad" sessions (few or none). Rewards celebrate what happened, never highlight what didn't.

- **Celebrate effort, not just results** — "You showed up and tried today. That counts."
- **Never reference streak breaks negatively** — streak ends: don't mention. Just start fresh.
- **Partial progress is real progress** — sub-task completion deserves acknowledgment
- **Safe exits get warmth, not silence** — "See you next time" beats no response
- **No guilt-inducing comparisons** — never "You did 3 tasks yesterday but only 1 today"

---

## Reward Architecture

```mermaid
flowchart TB
    subgraph Trigger["Reward Triggers"]
        Initiation[Task Started]
        FirstStep[First Step Done]
        Resume[Resumed After Break]
        Complete[Task Completed]
        Streak[Streak Achieved]
        Milestone[Milestone Reached]
    end

    subgraph Engine["Reward Engine"]
        Select[Reward Selector]
        Scale[Intensity Scaler]
        Deliver[Multi-channel Delivery]
    end

    subgraph SystemRewards["System-Generated Rewards"]
        Emoji[Emoji Celebration]
        Image[AI-Generated Image]
        Video[AI Video<br/>ffmpeg]
        Music[Music Playback<br/>Home Audio]
    end

    subgraph InterpersonalRewards["Interpersonal Rewards"]
        TextSO[Text Significant Other]
        Outing[Suggest Outing]
    end

    Initiation --> Select
    FirstStep --> Select
    Resume --> Select
    Complete --> Select
    Streak --> Select
    Milestone --> Select

    Select --> Scale
    Scale --> Deliver

    Deliver --> Emoji
    Deliver --> Image
    Deliver --> Video
    Deliver --> Music
    Deliver --> TextSO
    Deliver --> Outing
```

> **Note:** All initiation-phase triggers (Task Started, First Step Done, Resumed After Break)
> fire as reward events within the **Active** conversation state — they are not separate
> conversation states. See the conversation state diagram in `docs/ai-prompts/shared.md`.

---

## System-Generated Rewards

### Emoji Celebrations

Emoji-loaded congratulations messages scaling with achievement significance.

```mermaid
flowchart LR
    subgraph Intensity["Celebration Intensity"]
        Low["Single Task<br/>Completed"]
        Medium["Difficult Task<br/>or 3-task Streak"]
        High["Major Milestone<br/>or 5+ Streak"]
        Epic["Parent Task Complete<br/>or Day Clear"]
    end

    subgraph Output["Emoji Output"]
        LowEmoji["Nice work! ✨"]
        MedEmoji["Crushing it! 🎉✨💪"]
        HighEmoji["UNSTOPPABLE! 🔥🎉✨💪🚀"]
        EpicEmoji["LEGENDARY! 🏆👑🔥🎉✨💪🚀⭐"]
    end

    Low --> LowEmoji
    Medium --> MedEmoji
    High --> HighEmoji
    Epic --> EpicEmoji
```

#### Initiation Reward Templates

> **Design principle:** Starting harder than finishing for ADHD brains. Initiation rewards acknowledge this truth. Feel like genuine encouragement from someone who understands, not participation trophies. Keep brief — user about to start working.

```mermaid
flowchart TD
    subgraph Triggers["Initiation Trigger Points"]
        Accept[User accepts task] --> StartReward[Initiation Reward]
        FirstStep[First sub-step completed] --> ProgressReward[First-Step Reward]
        Return[User returns to paused task] --> ResumeReward[Resume Reward]
    end

    subgraph Scoring["Unified Scoring (see Reward Scaling Algorithm)"]
        Calc["Score calculated using<br/>base_score + streak_bonus − diminishing"]
        Cap["Capped by initiation_ceiling<br/>to keep lighter than completion"]
    end

    subgraph Intensity["Maps to Unified Intensity Levels"]
        Lightest["Score 0-10 → Lightest<br/>Brief acknowledgment"]
        Low["Score 11-25 → Low<br/>Momentum confirmation"]
        Medium["Score 26-50 → Medium<br/>(max for initiation triggers)"]
    end

    StartReward --> Calc
    ProgressReward --> Calc
    ResumeReward --> Calc
    Calc --> Cap
    Cap --> Intensity
```

Initiation rewards use **same scoring algorithm** as completion rewards
(see [Reward Scaling Algorithm](#reward-scaling-algorithm)), with two
initiation-specific adjustments:

1. **`initiation_base_weight`** — multiplier (default `0.4`) on base score, keeping initiation rewards inherently lighter.
2. **`initiation_ceiling`** — intensity cap (default `Medium / 50`) preventing initiation rewards from reaching `High` or `Epic`, preserving those tiers for completion.

| Trigger | Base-Weight | Ceiling | Example Messages |
|---------|-------------|---------|------------------|
| Task accepted (starting) | 0.3 | Lightest (10) | "You're in. That's the hardest part.", "Starting — nice.", "Let's go." |
| First sub-step done | 0.4 | Low (25) | "First step done — you're in motion now.", "One down. Momentum's real." |
| Resumed after break | 0.5 | Medium (50) | "Back at it — picking up where you left off is a skill.", "Welcome back. Ready to keep going?" |
| Started 3+ tasks today | 0.4 | Low (25) | "Third start today — your initiation muscle is getting stronger." |

**Important design constraints:**
- Initiation rewards must be **briefer and lighter** than completion rewards
- Never celebrate starting so much it diminishes completion celebration
- Tone is **acknowledgment of difficulty**, not generic cheerleading
- "You started" validates that starting is genuinely hard — don't trivialize it
- First-time users always get initiation reward; returning users: vary frequency to avoid habituation

#### Completion Celebration Message Templates

| Trigger | Intensity | Example Messages |
|---------|-----------|------------------|
| Single task | Low | "Nice work! ✨", "Done! 💫", "Got it! ✅" |
| Quick task (< 15 min) | Low | "Speed demon! ⚡", "Quick win! 🎯" |
| Focus task complete | Medium | "Deep work done! 🧠✨", "Focus mode: crushed! 💪🎯" |
| 3-task streak | Medium | "Hat trick! 🎩✨🎉", "Three down! 🔥💪" |
| 5-task streak | High | "On fire! 🔥🔥🔥✨💪", "Unstoppable! 🚀🎉💪" |
| Difficult task | High | "Beast mode! 💪🔥🎉", "Conquered! ⚔️✨🏆" |
| Parent task (all subs done) | Epic | "MAJOR WIN! 🏆👑🎉✨🔥", "PROJECT COMPLETE! 🚀⭐💪🎊" |
| All tasks cleared | Epic | "INBOX ZERO! 🏆👑✨🎉🔥💪🚀", "LEGENDARY DAY! 👑⭐🏆🎊" |

---

### AI-Generated Celebration Images

Every completion gets **unique, AI-generated celebration image** via OpenAI's `gpt-image-1` model. Novelty ADHD brains crave — no two celebrations identical, prevents habituation, maintains dopamine response.

#### Why AI-Generated Images

- **Novelty**: ADHD brains habituate to repeated stimuli. Every AI image unique, no predictability.
- **Dopamine**: Novel visual stimuli trigger stronger dopamine than familiar ones.
- **Personalization**: Prompts incorporate user context, streaks, preferences.
- **Scalability**: No static image library to curate/maintain.

```mermaid
flowchart TD
    subgraph Trigger["Completion Trigger"]
        Complete[Task Completed]
        Intensity[Calculate Intensity]
    end

    subgraph Generation["Image Generation"]
        Analyze[Classify Task Motif + Sensitivity]
        Prefs[Load Reward Preferences + Feedback]
        Theme[Select Weighted Theme]
        Prompt[Build Personalized Prompt]
        Style[Apply Style + Palette Modifiers]
        Generate[OpenAI gpt-image-1 API]
    end

    subgraph Delivery["Image Delivery"]
        Reply[Celebration text only]
        Media[Single MEDIA attachment]
    end

    Complete --> Intensity
    Intensity --> Analyze
    Analyze --> Prefs
    Prefs --> Theme
    Theme --> Prompt
    Prompt --> Style
    Style --> Generate
    Generate --> Delivery
```

#### Image Generation

`app/tools/rewards.py` handles all image generation. The equivalent call:

```python
await rewards.generate_reward_image(
    intensity="medium",
    streak_count=2,
    task_descriptions=["Review proposal", "Clean desk"],
    motif="cleanup",  # optional; from classify_task_motif()
    work_type="focus",  # optional
    energy_level="low",  # optional
)
```

The function validates its reward inputs before generation:

- `streak_count` is the positive, post-completion current streak length. It drives the progress-marker count in the prompt and is clamped there.
- Task descriptions are private input — never embedded in the prompt and never logged. What connects the image to the task is `motif`, a label the caller obtains from `classify_task_motif()`.
- Blank or missing task descriptions are tolerated and do not abort image generation; without a usable title there is no motif, and the prompt falls back to generic progress imagery driven by intensity and theme selection alone.

Classification is the caller's job, not this function's: `generate_reward_image()` makes no LLM calls, so one classification is shared between the image prompt and the manifest row.

Callers pass the current completed task's description. There is no count relationship between descriptions and `streak_count` — descriptions never reach the prompt, so holding only the current task while reporting a longer streak is correct.

Output: `app/tools/rewards.py` generates a PNG, stores it in the reward artifacts volume, writes a manifest row in Postgres, and surfaces the path in `RewardResult.attachment_path`. The COMPLETE node populates `OutboundDraft.attachment_path`, and the send node delivers the image as a Signal attachment via `signal_client.send_message(attachment_paths=[...])`. Only PNG files under the `REWARD_ARTIFACTS_DIR` root are accepted (PNG-only content-type policy).

#### Prompt Personalization Pipeline

Reward prompts are built from four layers:

1. **Independent descriptor axes** — theme-family, style, and palette are drawn separately. Theme comes from the pool for the intensity (low/medium/high/epic), which is what carries celebration scale. Style and palette come from single vocabularies shared across all intensities
2. **Task motif** — the completed task is classified into one label from a fixed motif vocabulary; the label favors theme descriptors that suit it and contributes one scene line to the prompt
3. **Preference modifiers** — optional `user_prefs.rewards` values (Postgres) extend the vocabularies and are favored within them, biasing style, palette, and subject matter
4. **Feedback weighting** — prior positive/negative reactions bias future selection on each axis independently

**Why the axes are independent:** welding theme, style, and palette into fixed triples makes both the reachable image count and the learning rate collapse to the number of triples. Five triples per intensity is five reachable images, and habituation is the failure mode the image system exists to prevent. Drawn independently, the same strings reach 5 x 8 x 8 = 320 combinations per intensity. Welded triples also make every attribute exactly as sparse as the rarest one, so no attribute can accumulate enough ratings to mean anything; split apart, a single reaction is evidence about three descriptors rather than one.

**Why style and palette ignore intensity:** scoping them per intensity would quarter the rate at which observations accumulate, on the two axes that are capable of learning at all. Sharing one vocabulary across intensities pools that evidence. The theme string and the prompt's mood line carry the intensity semantics instead, so an unexpected style pairing stays tonally anchored.

**Where the vocabularies live.** The seed vocabularies are constants in `app/tools/rewards.py`. On a peer's first image reward they are copied into the `reward_theme_pool` table (migration 0012), keyed by peer, and selection reads from there afterward — so a peer's vocabulary can grow and retire independently of what shipped in the repo. Theme rows are scoped to an intensity; style and palette rows carry no intensity, matching the shared-vocabulary design above.

Retirement is a soft delete: a retired row stops appearing in selection but keeps its rating attribution and its history. Nothing is ever deleted, so a retirement can be undone by clearing `retired_at`. No job does that on its own — the weekly evolution job proposes only values the peer has never held, so a retired descriptor stays retired until someone brings it back deliberately.

The table is an enhancement, not a precondition. Seeding failure, an unreachable database, and a vocabulary missing any axis all fall back to the seed constants. A partial vocabulary is refused outright rather than used, because a half-loaded axis would silently narrow selection — the failure this design exists to prevent. `reward_theme_pool.value` is embedded verbatim into the image prompt. The seed population comes from the repo constants above and requires no sanitization; any writer that introduces user-influenced values must pass `_sanitize_descriptor` before inserting. Values are read from the table without further filtering, so treat `reward_theme_pool.value` as user-influenced text in ops queries.

**Sensitive-task rewards never read this table.** Their allowlist stays a code constant and `_select_theme` returns before any vocabulary lookup, so that path cannot be steered by stored content however it got there. This is structural, not a filter.

#### Vocabulary Evolution

A weekly job (`theme_evolution`, Mondays 04:30 local) is what makes a peer's vocabulary drift away from the seeds. It retires descriptors the user consistently reacts badly to, and proposes new ones from the qualities of the descriptors they react well to. Over a season the vocabulary a peer draws from stops being the one that shipped in the repo.

**Evidence gate.** The job does nothing unless at least 12 new ratings have been recorded since the last time it added anything. Below that it logs and returns without calling the model.

**Growth is rate-limited:** at most 2 new descriptors and 1 retirement per axis per run. One intensity's theme tier is considered per run, rotating by ISO week; style and palette carry no intensity and so are considered every run.

**Retirement requires sustained evidence** — at least 3 negative reactions, a success rate at or below 25%, and an age of at least 14 days — and is a soft delete: the row keeps its rating attribution and its history.

**Why retirement is allowed to remove a descriptor from selection.** A descriptor that has earned three negative reactions and a success rate at or below 25% is producing images the user does not want. Keeping it reachable forever, in the name of preserving every theoretical combination, trades a recurring real cost — images the user dislikes — for a hypothetical novelty benefit. Novelty is the goal because habituation is the failure mode, and a disliked descriptor is not doing novelty work; it is spending a reward on something that does not land.

What actually protects novelty is the size and freshness of the active set, not the immortality of any one member of it, and both are structurally guaranteed:

- **Retirement can never outpace growth**, and this is enforced per run rather than implied by the constants. A run inserts first and then retires at most as many descriptors on an axis as it actually inserted there, capped at 1 against a maximum of 2 added. An axis that gained nothing — every proposal rejected by the sanitizer, or all duplicates — loses nothing. The active set can hold steady or grow; it cannot shrink.
- **Growth is counted honestly.** Proposals are deduplicated against every value the peer has ever held, retired ones included, not just the active set. A proposal matching a retired value would otherwise be accepted, lose to the unique index, store nothing, and still count as the growth that licenses a retirement.
- **Each axis has a hard floor** it may never fall below: 5 themes per intensity, 4 styles, 4 palettes. Retirement stops at the floor regardless of how negative the evidence is.
- **Nothing is deleted.** A retired row keeps its history and can come back.

This is a different guarantee from the `_SELECTION_EPSILON` floor described under **The novelty floor**, and the two operate at different timescales. Epsilon governs a single draw: no amount of negative feedback can make an active descriptor unreachable. Retirement governs the season: a descriptor with sustained negative evidence leaves the active set and a new one takes its place.

**The model sees no private data.** The prompt is built from descriptor strings and integer rating counts only — never task titles, peer identifiers, emoji, timestamps, or artifact paths. Descriptor strings are restricted to values present in the peer's active `reward_theme_pool` with origin `seed` or `evolved`; seed descriptors are repo-authored constants, and evolved descriptors passed `_sanitize_descriptor` before storage. This is the same discipline `_build_image_prompt` enforces.

**Model output is untrusted** and passes the same `_sanitize_descriptor` checks as user preference text before it can be stored, plus a duplicate check against every value the peer has ever held, retired rows included. Rejected proposals are dropped with a count-only log.

**Failure is inert.** An unreachable database, an LLM outage, and unparseable model output all leave the existing vocabulary exactly as it was. Proposals are built before any write, so nothing is touched until there is something to store. Insertion and retirement then run in one transaction on one connection, so a failure between them rolls back both rather than leaving retirements committed against insertions that never landed. The job never raises into the scheduler.

**Early runs are closer to guided vocabulary expansion than to preference learning**, because 12 ratings is still 12 ratings. That is the intended tradeoff: novelty is the product goal and learned taste is the bonus. New descriptors enter at a neutral weight, so they are drawn at exactly baseline rate rather than being promoted on arrival.

**Vocabulary size is a budget, not a feature.** Separating a liked descriptor from a disliked one takes roughly 40 ratings for that descriptor. At this system's volume that is reachable over a season only while the style and palette vocabularies stay around eight entries each. Every value added dilutes per-value evidence linearly, so growth is deliberate and bounded rather than open-ended. Repetition is addressed by recombination, not by longer lists.

Layers 2, 3, and 4 are multipliers on the same per-axis weights, so a motif-suited descriptor the user also likes compounds. Every descriptor keeps a non-zero weight — the `_SELECTION_EPSILON` floor applies after every bias, so no layer can make a scene unreachable.

##### Task Motif Classification

The image is about what the user finished, and the task title still never leaves the local network. Those hold together because classification and generation talk to different places: text inference runs against the local LLM proxy, while image generation calls the external provider.

- `classify_task_motif()` runs on the cheap tier and returns one key from the motif vocabulary in `app/tools/rewards.py` (`errand`, `communication`, `cleanup`, `repair`, `admin`, `creative`, `movement`, `learning`, `planning`, `social`), or nothing.
- The local-only claim is enforced, not assumed. `setup/model-tiers.json` can point any tier at an external provider without touching the call site, so `classify_task_motif()` checks `app.models.is_local_tier("cheap")` first and declines to send the title when the tier resolves to a non-local model family. A tier swap costs a themed image, never the title.
- The motif's scene phrase is fixed generic English. No task text is interpolated into it, so the motif label is the only task-derived value that reaches the image provider.
- Output is checked against the vocabulary allowlist. A task title is user-controlled text, so a title that tries to steer the classifier can at worst select a different celebration scene.
- A blank title, an off-vocabulary answer, or a classifier failure yields no motif. The prompt then omits the motif line entirely and falls back to generic progress imagery — an unclassifiable task still earns its image.
- Sensitive tasks are never classified. Their title is not sent to any model, and their imagery stays abstract.
- Classification only runs when an image is actually possible. Without `OPENAI_API_KEY` there is no prompt to steer, and a model round-trip before a text-only fallback is latency the user pays for nothing — immediate gratification is the point of the reward.

**How the motif reaches the picture.** It biases the theme axis and adds one scene line to the prompt. The bias is a multiplier on theme descriptors mapped to that motif, applied alongside the preference bonus and under the same novelty floor: a motif shifts which scene is likely, never which scenes are possible. Style and palette are untouched — they describe how the scene is rendered, not what was accomplished.

The affinity map covers the seed vocabulary only. A descriptor added by the weekly evolution job carries no motif affinity and draws at its unbiased weight; relevance then rests on the motif line, which applies to every descriptor. Inferring affinity from descriptor text would mean matching against strings the model proposed, and a wrong match reads worse than no match.

`_build_image_prompt()` assembles the selected theme-family, style, palette, motif phrase, streak marker count, humor level, and feedback guidance. It never embeds task text.

The motif is persisted on `reward_manifests.motif`, which is what makes image relevance auditable: generated PNGs live in a Docker volume, so the manifest row is where the connection between a task and the picture it earned can be checked after the fact.

#### Reward Delivery Contract

The COMPLETE reward phase has a strict visible-output boundary. All work before
delivery is hidden implementation detail: status updates, reward scoring, streak math, channel selection, `app/tools/rewards.py` invocations, fallback diagnostics, and image-generation progress must not be sent to the user.

Do not narrate reward preparation. A COMPLETE turn must not contain visible text
like "calculating the reward", "updating Notion", "generating an image", score
breakdowns, tool names, or progress updates before the reward.

The final user-visible reward content contains celebration text and, when
image generation succeeds, a PNG attachment delivered via Signal. If
`OPENAI_API_KEY` is not set or image generation fails, a fallback real-life
reward suggestion is appended to the celebration text instead (medium, high,
epic intensities only). Sensitive tasks receive muted emoji text only, no
fallback or image.

Reward delivery is scoped to the user turn, not to each internal task update.
If one user message completes multiple tasks, complete all hidden task/state
work first, calculate the combined reward intensity, generate one representative
image for the combined win, and send one final reward reply.
That reply still contains exactly one celebration message and at most one image.
Do not send one reward reply per task.

Reward Delivery Checklist:

- [ ] No interim user-visible messages were sent during reward scoring, state updates, Notion updates, or image generation
- [ ] Visible user copy is celebration only — no orchestration notes, no "Now let me...", no tool narration
- [ ] At most one reward reply per user COMPLETE turn
- [ ] If image generation fails, fall back to emoji/text only — no error message shown to user
- [ ] If the turn also includes an outing suggestion or other follow-up, send that as a separate plain-text message after the reward reply
- [ ] If multiple completions are handled in one user turn, per-task score math and tool work remain hidden; user-visible output is one combined celebration with one representative image/fallback at most

#### Theme Pools by Intensity

Each intensity level has 5+ theme candidates. Selection is weighted by preferences and bounded feedback.

| Intensity | Theme Style | Examples |
|-----------|-------------|---------|
| Low | Gentle, warm, cozy | Cheerful bird with sparkle, paper airplane in clouds, happy cat in sunbeam |
| Medium | Enthusiastic, joyful | Fox dancing in wildflowers, confetti explosion, otter on rainbow waterfall |
| High | Majestic, powerful | Phoenix rising from golden flames, astronaut planting flag, whale in starfield |
| Epic | Cosmic, transcendent | Galaxy forming a crown, reality folding into light cathedral, cosmic phoenix |

#### Sensitive Task Guardrail

When the task classifier detects a private or shame-heavy completion (therapy, medical, legal, financial, or private admin work), image generation is skipped entirely:

- the task is not classified into a motif — its title is sent to no model
- image generation is not called; there is no abstract fallback image
- the user receives muted emoji text only, consistent with the Reward Delivery Contract

#### Reward Preference Schema

Canonical reward image preferences live under key `rewards` in `user_prefs.prefs_json` (Postgres JSONB):

```json
{
  "preferred_styles": ["storybook watercolor", "paper collage illustration"],
  "preferred_palettes": ["cozy pastel glow", "aurora jewel tones"],
  "avoid": ["clinical imagery", "spiders"],
  "favorite_subjects": ["deep space", "sleeping cats"],
  "humor_level": "playful"
}
```

This object is what `load_reward_prefs` returns — it is the value of `prefs_json -> 'rewards'`, not a column of its own.

Every value here passes `_sanitize_descriptor` before it reaches selection or the prompt, so a value the sanitizer rejects is silently a no-op rather than an error. Write preferences in its terms: lowercase words, spaces, commas, apostrophes and hyphens only; at most 8 words and 60 characters; and none of the banned terms, which include prompt-framing words (`ignore`, `system`, `text`, `caption`, `logo`), people (`person`, `child`, `celebrity`), and every sensitive-task keyword — `medical`, `therapy`, `legal`, and the rest. That last rule is why an avoid entry like "medical literal" stores fine and then does nothing.

Supported preference dimensions:

- **Styles** - e.g. watercolor, collage, 3D, graphic illustration
- **Palettes** - warm, pastel, jewel-tone, neon, nature-led
- **Avoid list** - tags or vibes to suppress
- **Humor level** - `subtle`, `playful`, or `maximal`
- **Subjects** (`favorite_subjects`) - joins the theme vocabulary for the draw and is favored within it, the same way stated styles and palettes extend their own axes

**How preferences reach image generation:** `maybe_reward` loads the profile with `load_reward_prefs(peer)`, which reads `prefs_json -> 'rewards'` for that peer. A caller may pass preferences explicitly via the `user_prefs` argument instead — any non-`None` value (including `{}`) wins and no lookup happens; no caller in the graph passes this argument, so the stored profile is what runs in production. Preferences are read from Postgres at reward time rather than carried in LangGraph State, because State is the checkpoint unit — a copy living there would be persisted per conversation thread and drift from the table on every edit.

The lookup fails open: a missing row, a missing or wrongly-typed `rewards` subtree, or a database error all yield an empty profile and neutral generation. A preferences failure never blocks a reward.

**Input constraint:** preference values are intended as visual descriptors — art styles, palettes, and subject categories — and must not contain personal detail. `preferred_styles`, `preferred_palettes`, and `favorite_subjects` are sanitized by `_sanitize_descriptor` before entering selection vocabularies; rejected values are dropped without content logging. The drawn descriptor — which may be a sanitized preference value or a seed constant — is persisted on `reward_manifests` as `style` and `palette`, so treat those manifest columns as user-influenced text in ops queries. See **Descriptors are untrusted input** below for the full sanitizer contract.

Every preference dimension that reaches the prompt passes the same gate, not just the ones that become selection vocabulary. `avoid` terms are sanitized by the same function before they are joined into the prompt's avoid clause, and `humor_level` is validated against its three defined values rather than interpolated. Preferences only became live once `load_reward_prefs` started running for every eligible completion, which is what turned an unread column into text in front of the external image provider.

#### Streak Enhancements

Streak count modifies generated image:

| Streak | Visual Enhancement |
|--------|--------------------|
| 1 | One small glowing progress marker |
| 1 < N ≤ 10 | Exactly N small glowing progress markers, one per completed task in the current streak |
| N > 10 | Ten markers. The marker is a composition detail, not a counter — past roughly a dozen the model stops rendering them as countable objects, so the ask stops growing. |

#### Feedback Loop

Each reward delivery writes a row to the `reward_manifests` Postgres table. `app/tools/rewards.record_reward_feedback` is the direct Postgres write handler for recording emoji reactions against reward manifest rows.

**Emoji-to-score mapping** (`_FEEDBACK_EMOJI_SCORES`):

| Emoji | Score | Signal |
|-------|-------|--------|
| 👍 ❤️ 🎉 🔥 😍 💯 | +1 | Positive |
| 👎 😞 😕 💔 | -1 | Negative |
| Any other emoji | 0 | Neutral acknowledgment |

Unknown emojis are recorded with score 0 — the reaction is stored as a "received" signal but carries no positive or negative weight.

**Reaction routing:** Signal reaction envelopes are administrative events. The listener records feedback only when the sender is in `AUTHORIZED_PEERS` and the reaction target author matches the bot Signal account. Reactions do not invoke the LangGraph conversation path.

**Lookup window:** `record_reward_feedback` converts Signal's target message timestamp from milliseconds to UTC and matches the closest unrated `reward_manifests` row for the peer where `delivered_at` falls within ±30 seconds of that target timestamp. The `match_window_seconds` value is configurable per call. This tight window accounts for local manifest-write time, send latency, and clock skew without attributing reactions on unrelated nearby messages to older rewards.

**Storage:** Three feedback columns on `reward_manifests`:

- `feedback_score` (INT) — -1, 0, or +1
- `feedback_emoji` (TEXT) — the raw emoji character(s), stored verbatim
- `feedback_at` (TIMESTAMPTZ) — when the reaction was recorded

Each delivery also records the visual choices that produced the image, so a
reaction can be attributed to them:

- `theme_family` (TEXT) — the selected theme
- `style` (TEXT) — the selected art style
- `palette` (TEXT) — the selected color palette

These columns are NULL for emoji-only rewards and for rows written when image generation fails.

Two further columns describe the task-to-image relationship rather than the image itself:

- `motif` (TEXT) — the classified task motif. Recorded even when generation fails, because it describes the task, not the picture. NULL for sensitive and emoji-only rewards and when classification yields nothing.
- `image_failure_reason` (TEXT) — why a delivery fell back to text: `no_api_key`, `not_eligible`, `api_error`, or `empty_response`. NULL when an image was generated or none was attempted. Fixed vocabulary, never user-authored text.

**Idempotency:** `feedback_at IS NULL` prevents double-counting. If a user reacts twice, only the first reaction for a given reward row is recorded. A later reaction may still match another unrated reward inside the tight timestamp window.

**Weighted selection:** `load_feedback_history` loads recent rated rewards for the peer from the last 90 days. `_select_theme` then draws each axis separately with `_draw_attribute`, weighting the axis' vocabulary with `_attribute_weight`, then applying the stated-preference bonus and — on the theme axis — the task motif's affinity bonus.

For one descriptor on one axis, ratings that name that descriptor accumulate into decayed positive and negative counts:

- Contributions decay exponentially with a 45-day half-life: a rating counts fully the day it is given, at half strength after 45 days, and at quarter strength at the 90-day edge of the load window. Ratings older than the window are not loaded at all.
- Ratings scoring `0` (unknown emoji) are recorded as acknowledgment and contribute to neither count.
- The counts combine into a Beta-smoothed success rate, scaled by how much evidence exists. With no ratings the weight is exactly `1.0`.
- The result is bounded by a per-axis cap: theme `±0.25`, style `±0.50`, palette `±0.50`.

The decay curve has no hard cutoff inside the window, so the load window is the only place a rating stops counting. Both numbers live in `app/tools/rewards.py` as `_FEEDBACK_WINDOW_DAYS` and `_FEEDBACK_HALF_LIFE_DAYS`, and the load window feeds `load_feedback_history`'s default so the two cannot drift apart.

Why exponential rather than a fixed expiry: at this system's rating volume — a handful of image rewards a day, only some of them reacted to — a hard cutoff throws away most of the evidence the user has given. Gradual decay keeps old ratings contributing something while still letting recent taste dominate.

Why theme is capped lowest: theme is the highest-cardinality axis and the one that rarely repeats, so it is where novelty is spent and where evidence is thinnest. Style and palette repeat constantly across rewards, so they are where preference can actually be learned, and they carry the larger caps.

**The novelty floor.** Per-axis weights are normalized into a probability distribution and then mixed with a uniform distribution at `_SELECTION_EPSILON` (`0.15`). Every active descriptor therefore keeps at least `0.15 / vocabulary_size` probability, no matter how negative its history. This is what keeps habituation — the failure mode the image system exists to prevent — from creeping back in through feedback: within a draw, feedback biases selection and can never eliminate a descriptor. Because the floor bounds probability directly rather than bounding a weight, it holds unchanged as vocabularies grow. A single reaction shifts the odds slightly; a consistent pattern shifts them meaningfully; no active descriptor's odds ever reach zero.

The floor is a property of the draw, and it applies to the active vocabulary. Membership of that vocabulary is decided elsewhere, on a much slower clock: the weekly `theme_evolution` job retires descriptors with sustained negative evidence and adds new ones at a faster rate than it retires. See **Vocabulary Evolution** for why removal is permitted there and what bounds it.

**Stated preferences bias, they do not dictate.** `preferred_styles`, `preferred_palettes`, and `favorite_subjects` are appended to their axis' vocabulary and receive a `1.5` weight multiplier. They do not replace the vocabulary — a single stated style would otherwise appear on every image, removing style novelty entirely.

**Descriptors are untrusted input.** Every preference-derived value passes `_sanitize_descriptor` before it can reach a vocabulary: NFKC normalization, rejection of line breaks and control characters, a length and word-count bound, a character allowlist of `a-z 0-9 space comma apostrophe hyphen`, and rejection of instruction verbs, text-rendering terms, identity terms, and sensitive keywords. The character allowlist is the control that matters — it removes every character usable to break out of the `Theme: {x}.` framing in `_build_image_prompt`; the term lists are defense in depth and are not assumed complete. Rejected values are dropped silently and logged as a count only, never as content.

**Prompt guidance:** Image generation additionally summarizes feedback in the prompt only after at least three ratings:

- More positive than negative ratings adds guidance to lean energetic and celebratory.
- More negative than positive ratings adds guidance to be a bit more subdued.
- Small or balanced samples add no feedback guidance.

The prompt guidance is intentionally short and coarse so feedback nudges future rewards without turning one reaction into a hard preference.

#### Novelty Mechanics

Image generation system inherently addresses novelty:

1. **Independent axis draws** - theme, style, and palette are drawn separately, so each intensity reaches the product of its three vocabularies (currently 5 x 8 x 8 = 320 combinations) rather than a fixed list of triples
2. **Weighted selection with a floor** - preferences, task motif, and bounded feedback nudge each axis rather than dictating it, and the `_SELECTION_EPSILON` uniform mixture guarantees every active descriptor keeps at least `0.15 / vocabulary_size` probability; no bias can drive an active descriptor's odds to zero
3. **Task motifs** - the accomplished task changes the scene, both by favoring theme descriptors that suit it and by adding its own scene line, so the same intensity tier reads differently across different kinds of work
4. **AI variation** - same prompt produces different images each time
5. **Streak-responsive** - visual elements change as streaks grow
6. **Evolving pools** - the weekly `theme_evolution` job grows each axis faster than it retires from it, so the reachable combination count rises over time while descriptors the user consistently dislikes drop out

#### Graceful Degradation — Offline Fallback Rewards

If image generation unavailable (API outage, missing key, network error, malformed response), script **does not fail silently**. Suggests fun non-digital real-life reward from pool of 12:

- Favorite snack, cupcake, ice cream, chocolate
- 30 minutes of a favorite video game
- Fancy coffee or hot chocolate
- A walk outside, stretches, or yoga
- Mini dance party, calling a friend, watching a show
- Ordering favorite takeout

Fallback writes suggestion to `.txt` file (instead of `.png`) and exits successfully — reward pipeline always delivers something. Prevents "expected reward didn't arrive" anti-pattern from Hallowell-Ratey's ADHD framework.

#### Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API authentication for image generation |
| `REWARD_ARTIFACTS_DIR` | Directory for storing generated reward images (default: `/tmp/reward_artifacts`) |

#### Image Archive & Collection

Every generated reward image is stored under the `reward_artifacts` Docker volume:

- **File naming**: `YYYY-MM-DD_HHMMSS_<intensity>.png`
- **Manifest record**: `reward_manifests` Postgres table tracks peer, intensity, streak, delivered_at, artifact_path, the visual descriptors (`theme_family`, `style`, `palette`), and feedback columns per delivery. task_title is stored as a private column — never logged.
- **Persistent**: Images survive across sessions on the `reward_artifacts` volume — celebration history preserved

#### Weekly Recap Video

The `weekly_recap` APScheduler job (Sundays at 18:00 USER_TZ) compiles all reward images from the past week into a card-flip transition video. See `app/scheduler/jobs.py` for the job definition.

Audio and video recap features are deferred to v1.1 — see `docs/python-rewrite/reward-deferred.md`.

Features:
- **Card-flip transitions** between images (fadegrays, circlecrop, radial, etc.)
- **Variety in transitions** — each cut uses different style
- **Fade-out ending** for polished finish
- **Output**: `rewards/weekly-recap-YYYY-MM-DD.mp4`

Recap = tangible accomplishment record — scrolling a week of unique celebration images is itself a reward.

```mermaid
flowchart LR
    subgraph Archive["Image Archive"]
        Mon["Mon: 2 images"]
        Tue["Tue: 3 images"]
        Wed["Wed: 1 image"]
        Thu["Thu: 4 images"]
        Fri["Fri: 2 images"]
    end

    subgraph Recap["Weekly Recap"]
        Video["Card-flip video<br/>12 images, ~40 seconds"]
    end

    Archive --> Recap
    Recap --> Deliver["Send via Signal/Telegram"]
```

#### Technical Details

| Setting | Value |
|---------|-------|
| Model | `gpt-image-1` |
| Size | 1024x1024 |
| Quality | `auto` (low-high), `high` (epic) |
| Output format | PNG (images), MP4 (recap video) |
| Typical generation time | 10-20 seconds |
| Archive location | `rewards/` |
| Video codec | H.264 (libx264) |
| Display per image | 2.5 seconds |
| Transition duration | 0.8 seconds |

---

### Music Playback (Home Audio Integration)

Home automation plays celebratory music on task completion.

```mermaid
flowchart TB
    subgraph HomeAudio["Home Audio Systems"]
        Sonos[Sonos]
        HomePod[Apple HomePod]
        Echo[Amazon Echo]
        GoogleHome[Google Home]
        Custom[Custom Systems<br/>via API/MQTT]
    end

    subgraph Integration["Integration Layer"]
        API[Home Automation API]
        MQTT[MQTT Bridge]
        HomeAssistant[Home Assistant]
    end

    subgraph Playback["Music Selection"]
        Favorites[User Favorites Playlist]
        Victory[Victory Songs Playlist]
        Mood[Mood-matched Music]
    end

    Complete([Task Completed]) --> Integration
    Integration --> HomeAudio
    Playback --> Integration
```

#### Music Playback Configuration

```mermaid
flowchart LR
    subgraph Config["User Configuration"]
        Enable["Enable/Disable"]
        Volume["Volume Level"]
        Duration["Play Duration<br/>15s / 30s / Full song"]
        Times["Active Hours<br/>Don't wake the baby"]
        Rooms["Target Rooms"]
    end

    subgraph Playlists["Playlist Sources"]
        Spotify[Spotify Playlist ID]
        Apple[Apple Music Playlist]
        Local[Local Music Library]
    end

    subgraph Rules["Playback Rules"]
        TimeCheck["Check active hours"]
        LocationCheck["Check presence"]
        FrequencyLimit["Rate limit<br/>Max 3/hour"]
    end

    Config --> Rules
    Playlists --> Rules
    Rules --> Play[Trigger Playback]
```

#### Example Music Triggers

| Achievement | Music Selection | Duration |
|-------------|-----------------|----------|
| Quick task | Random from "Victory Jingles" | 15 seconds |
| Focus task | Random from "Triumphant" | 30 seconds |
| Major milestone | User's favorite song | Full song |
| All tasks cleared | "We Are The Champions" | Full song |

#### Home Automation Integration Points

| System | Integration Method | Notes |
|--------|-------------------|-------|
| Sonos | Sonos API | Direct HTTP calls |
| Apple HomePod | HomeKit/Shortcuts | Via Shortcuts automation |
| Amazon Echo | Alexa Skills | Custom skill or routines |
| Google Home | Google Home API | Cast-enabled playback |
| Home Assistant | REST API | Universal bridge for any system |
| Custom | MQTT | Publish to configured topic |

---

## Interpersonal Rewards

### Text Significant Other

Auto-notify loved one on task completion — external positive reinforcement + social accountability.

```mermaid
flowchart TD
    subgraph Trigger["Completion Trigger"]
        Task["Task Completed"]
        Streak["Streak Achieved"]
        Parent["Project Finished"]
    end

    subgraph Filter["Notification Filter"]
        Frequency["Frequency Limit<br/>Max N per day"]
        Significance["Significance Threshold"]
        OptIn["Task Opt-in Check"]
    end

    subgraph Compose["Message Composition"]
        Template["Select Template"]
        Personalize["Add Task Context"]
        Tone["Match Relationship Tone"]
    end

    subgraph Deliver["Delivery"]
        SMS[SMS via Twilio]
        iMessage[iMessage via Shortcuts]
        WhatsApp[WhatsApp API]
        Telegram[Telegram Bot]
    end

    Trigger --> Filter
    Filter --> Compose
    Compose --> Deliver
```

#### Notification Configuration

| Setting | Options | Default |
|---------|---------|---------|
| recipient | Phone number or contact ID | Required |
| delivery_method | sms, imessage, whatsapp, telegram | sms |
| frequency_limit | 1-10 per day | 3 |
| min_significance | low, medium, high, epic | medium |
| active_hours | Time range | 9am-9pm |
| task_opt_in | all, tagged, manual | tagged |

#### Message Templates

```mermaid
flowchart LR
    subgraph Style["Message Styles"]
        Casual["Casual<br/>'Hey! [Name] just crushed [task]!'"]
        Supportive["Supportive<br/>'[Name] finished [task]! Maybe tell them nice work?'"]
        Celebratory["Celebratory<br/>'🎉 [Name] completed [task]! Celebration time!'"]
        Informative["Informative<br/>'FYI: [Name] completed [task]'"]
    end
```

| Trigger | Example Message |
|---------|-----------------|
| Single task | "Hey! [Name] just finished '[task]' - maybe give them a high five later? 🙌" |
| Streak (3+) | "[Name] is on a roll - [N] tasks done today! 🔥" |
| Difficult task | "[Name] just conquered a big one: '[task]'. They might need a hug! 💪" |
| Parent complete | "BIG NEWS: [Name] finished the entire '[project]'! Celebration dinner? 🎉" |
| All cleared | "[Name] cleared their ENTIRE task list! This calls for ice cream 🍦" |

#### Privacy & Consent

```mermaid
flowchart TD
    subgraph Consent["Consent Model"]
        UserConsent["User enables feature"]
        RecipientConsent["Recipient agrees to receive"]
        TaskTagging["User tags shareable tasks"]
    end

    subgraph Privacy["Privacy Controls"]
        Anonymize["Option to anonymize task names"]
        Categories["Share category only, not details"]
        Veto["User can veto before sending"]
    end

    Consent --> Active[Feature Active]
    Privacy --> Active
```

---

### Outing Suggestions

After completing tasks (especially difficult), suggest fun activities aligned with user interests — creates anticipation + self-reward.

```mermaid
flowchart TD
    subgraph Triggers["Suggestion Triggers"]
        MajorComplete["Major task completed"]
        DayClear["Day's tasks cleared"]
        LongStreak["Long streak achieved"]
        FridayComplete["Friday completions"]
    end

    subgraph Analysis["Context Analysis"]
        Time["Time of day"]
        Weather["Weather check"]
        Energy["User energy level"]
        Interests["User interests"]
        Location["User location"]
    end

    subgraph Suggestions["Outing Categories"]
        Food["Food & Drink<br/>Favorite restaurant, coffee shop"]
        Active["Active<br/>Hiking, gym, sports"]
        Social["Social<br/>Call friend, game night"]
        Relaxation["Relaxation<br/>Movie, spa, reading"]
        Adventure["Adventure<br/>New experience, exploration"]
    end

    Triggers --> Analysis
    Analysis --> Suggestions
    Suggestions --> Present[Present Suggestion]
```

#### User Interest Configuration

```mermaid
flowchart LR
    subgraph Interests["Interest Categories"]
        I1["Food preferences<br/>Cuisines, dietary"]
        I2["Activity level<br/>Low, medium, high"]
        I3["Social preference<br/>Solo, partner, group"]
        I4["Hobbies<br/>Sports, arts, games"]
        I5["Favorite spots<br/>Saved locations"]
    end

    subgraph Matching["Match Algorithm"]
        TimeMatch["Time-appropriate"]
        EnergyMatch["Energy-appropriate"]
        WeatherMatch["Weather-appropriate"]
        BudgetMatch["Budget-conscious"]
    end

    Interests --> Matching
    Matching --> Suggestion[Personalized Suggestion]
```

#### Suggestion Templates

| Context | Example Suggestions |
|---------|---------------------|
| After focus work (tired) | "You've earned a break! How about grabbing a coffee from [favorite_cafe]? ☕" |
| After physical task | "Nice work! Maybe reward yourself with [favorite_food] from [restaurant]? 🍕" |
| Friday afternoon | "Weekend's calling! Movie night with [partner] at [theater]? 🎬" |
| All tasks cleared | "EVERYTHING DONE! Time for an adventure - what about [saved_activity]? 🎉" |
| Long streak | "5 tasks in a row! You deserve [favorite_treat] 🏆" |
| Morning completion | "Great start! Save room for [lunch_spot] later? 🌮" |

#### External Integrations

| Service | Use Case |
|---------|----------|
| Google Maps | Location search, directions |
| Yelp API | Restaurant recommendations |
| Weather API | Weather-appropriate suggestions |
| Calendar | Check availability |
| Partner's calendar | Coordinate joint activities |

---

## Reward Scaling Algorithm

```mermaid
flowchart TD
    subgraph Input["Reward Inputs"]
        TaskDifficulty["Task Difficulty<br/>time + energy"]
        StreakCount["Current Streak"]
        TriggerType["Trigger Type<br/>initiation | completion"]
        TimeOfDay["Time of Day"]
        RecentRewards["Recent Reward History"]
        UserPrefs["User Preferences"]
    end

    subgraph Calculate["Intensity Calculation"]
        Base["Base Score<br/>from task difficulty"]
        Weight["Apply initiation_base_weight<br/>(1.0 for completion)"]
        Multiplier["Streak Bonus<br/>streak_count × 5"]
        Diminishing["Diminishing Returns<br/>reduce if many recent rewards"]
        Cap["Apply initiation_ceiling<br/>(100 for completion)"]
    end

    subgraph Output["Reward Selection"]
        Intensity["Intensity Level<br/>lightest|low|medium|high|epic"]
        Channels["Active Channels"]
        Content["Specific Content"]
    end

    Input --> Calculate
    Calculate --> Output
```

### Intensity Levels

| Level | Score Range | Emoji Count | AI Image | Music | Text SO | Outing | Used For |
|-------|-------------|-------------|----------|-------|---------|--------|----------|
| Lightest | 0-10 | 0 | No | No | No | No | Initiation only |
| Low | 11-25 | 1-2 | Gentle theme | No | No | No | Initiation + Completion |
| Medium | 26-50 | 2-4 | Enthusiastic theme | Maybe | Maybe | No | Initiation (max) + Completion |
| High | 51-75 | 4-6 | Majestic theme | Yes | Yes | Maybe | Completion only |
| Epic | 76-100 | 6+ | Cosmic theme (high quality) | Yes | Yes | Yes | Completion only |

### Score Calculation

Same formula for **both** initiation and completion rewards. Initiation triggers apply weight + ceiling to keep lighter.

```
# --- Shared base calculation (initiation + completion) ---
base_score = (time_estimate / 15) * 10 + (energy_level * 10)
streak_bonus = streak_count * 5
milestone_bonus = is_parent_complete ? 25 : 0
milestone_bonus += is_all_cleared ? 50 : 0

raw_score = base_score + streak_bonus + milestone_bonus
diminishing = max(0, (rewards_in_last_hour - 2) * 10)

# --- Completion rewards ---
completion_score = min(100, max(0, raw_score - diminishing))

# --- Initiation rewards ---
# initiation_base_weight: per-trigger multiplier (see table above)
#   task_accepted = 0.3, first_step = 0.4, resumed = 0.5, multi_start = 0.4
# initiation_ceiling: per-trigger max score
#   task_accepted = 10, first_step = 25, resumed = 50, multi_start = 25
weighted_score = (base_score * initiation_base_weight) + streak_bonus
initiation_score = min(initiation_ceiling, max(0, weighted_score - diminishing))
```

**Why two adjustments?**
- `initiation_base_weight` scales down task-difficulty component — user hasn't done work yet, only started.
- `initiation_ceiling` guarantees no initiation reward ever reaches `High` or `Epic`, keeping those tiers exclusively for completion. Starting never feels more rewarding than finishing.
- `streak_bonus` kept at full value for initiation — building a *starting* streak is genuinely hard for ADHD, deserves recognition.

---

## Configuration Schema

### Reward Delivery Settings (runtime config)

Image-style preferences live in `user_prefs.rewards` (Postgres). The schema below is separate: it covers delivery-channel toggles and channel-specific runtime settings, not the user's reward-image taste profile.

```mermaid
erDiagram
    REWARD_CHANNEL_SETTINGS {
        boolean emoji_enabled "Default: true"
        boolean image_enabled "Default: true"
        boolean music_enabled "Default: false"
        boolean video_enabled "Default: false"
        boolean text_so_enabled "Default: false"
        boolean outing_enabled "Default: true"
    }

    MUSIC_CONFIG {
        string home_system "sonos|homepod|echo|google|homeassistant"
        string playlist_id "Spotify/Apple Music ID"
        string[] target_rooms "Living Room, Office"
        int volume_level "0-100"
        string active_hours "09:00-21:00"
        int max_per_hour "3"
    }

    TEXT_SO_CONFIG {
        string recipient_phone "+1234567890"
        string delivery_method "sms|imessage|whatsapp|telegram"
        int max_per_day "3"
        string min_significance "medium"
        string active_hours "09:00-21:00"
        string message_style "casual|supportive|celebratory"
        boolean anonymize_tasks "false"
    }

    OUTING_CONFIG {
        string[] food_preferences "Italian, Mexican, Coffee"
        string activity_level "medium"
        string social_preference "partner"
        string[] favorite_spots "Cafe Luna, Central Park"
        string[] hobbies "hiking, movies, board games"
    }
```

---

## Integration with Existing Flows

### Completion Flow Enhancement

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant R as Reward Engine
    participant HA as Home Audio
    participant SMS as SMS Service
    participant N as Notion

    U->>AI: "Done!"
    AI->>N: Update task status → completed
    AI->>R: Trigger reward evaluation

    R->>R: Calculate intensity score
    R->>R: Select reward channels

    par Parallel Reward Delivery
        R->>AI: Celebration message text
        R->>AI: AI-generated image path
        R->>HA: Play victory music
        R->>SMS: Text significant other
    end

    AI->>U: "CRUSHED IT! 🔥💪✨" + single MEDIA attachment

    opt High intensity + cleared schedule
        R->>AI: Outing suggestion
        AI->>U: "You've earned it - coffee at Luna Cafe? ☕"
    end
```

### State Diagram Update

```mermaid
stateDiagram-v2
    [*] --> Pending: Task created

    Pending --> InProgress: User accepts
    InProgress --> Completed: User finishes

    Pending --> InitiationReward: User accepts (initiation trigger)
    InProgress --> InitiationReward: First step done / Resumed

    InitiationReward --> RewardEvaluation: Calculate score (weighted + capped)
    Completed --> RewardEvaluation: Calculate score (full)

    RewardEvaluation --> RewardDelivery: Map score to intensity level

    state RewardDelivery {
        [*] --> Emoji
        Emoji --> Image: if enabled + score ≥ Medium
        Image --> Music: if enabled + score ≥ High
        Music --> TextSO: if enabled + score ≥ High
        TextSO --> Outing: if score = Epic
        Outing --> [*]
    }

    InitiationReward --> InProgress: Continue working
    RewardDelivery --> [*]: All rewards delivered
```

---

## Agent Commands

Capabilities exposed via conversation commands. The app graph handles these directly.

| Command | Purpose |
|---------|---------|
| Reward settings | Get or update current reward settings |
| Test music | Test music integration |
| Test SMS | Test SMS delivery |
| Reward history | Get recent reward history |
| Home status | Check home system connectivity |
| List rooms | List available rooms |
| Play music | Trigger music playback |
| Stop music | Stop current playback |

---

## Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `OPENAI_API_KEY` | OpenAI API for image generation | `sk-proj-xxxxxxxx` |
| `DATABASE_URL` | Postgres connection string; reward prefs loaded from `user_prefs` table | `postgresql://hml:hml@postgres:5432/hml` |
| `TWILIO_ACCOUNT_SID` | Twilio authentication | `ACxxxxxxxx` |
| `TWILIO_AUTH_TOKEN` | Twilio authentication | `xxxxxxxx` |
| `TWILIO_PHONE_NUMBER` | Sender phone number | `+1234567890` |
| `SONOS_API_KEY` | Sonos integration | `xxxxxxxx` |
| `HOME_ASSISTANT_URL` | Home Assistant endpoint | `http://ha.local:8123` |
| `HOME_ASSISTANT_TOKEN` | Home Assistant auth | `xxxxxxxx` |
| `OPENWEATHER_API_KEY` | Weather for outings | `xxxxxxxx` |

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Task completion rate | +20% | Compare before/after |
| Session duration | +15% | Average time in app |
| Return rate | +25% | Users returning within 24h |
| Streak length | +30% | Average consecutive completions |
| User satisfaction | 4.5/5 | Post-session survey |
| Reward engagement | 80%+ | Rewards not dismissed immediately |
