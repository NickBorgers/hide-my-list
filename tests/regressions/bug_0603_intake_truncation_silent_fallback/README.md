# Bug 0603: Intake silently drops reminders on truncated LLM output

**Issue:** #603

## Bug Story

A task phrased as a deadline reminder (`<chore> <weekday> before <time>`) was
acknowledged by intake ("Got it — added.") but **no reminder was ever
scheduled** — no `reminder_outbox` / `reminder_scheduling_ledger` row was
created, so nothing fired. Two stacked bugs caused it:

1. **Trigger — output-token cap truncates intake.** `app/models.py` hardcoded
   `max_tokens: 1024` for every tier. That was a sane default under the original
   `ChatAnthropic` backend (concise; intake JSON ~240 tokens) and rode through
   the swap to a local reasoning model unexamined. On the `medium` tier
   (`think=on`) the model spent its 1024-token budget on reasoning and was cut
   off mid-JSON; every intake call returned `output_tokens: 1024` exactly.
2. **Mask — parse fallback fabricates success.** `_parse_intake_response`
   silently returned a default dict (`is_reminder=False`,
   `confirmation="Got it — added."`) when the output would not parse. The node
   saved a plain task with no reminder and confirmed success. The outer `except`
   had the same lie-on-failure shape.

Structural/unit tests with mocked-clean JSON could not catch this: the failure
only appears when the *model* returns malformed output, and the parser disguised
that as a normal non-reminder task.

## Fix

- `app/models.py`: per-tier `max_tokens` (`_TIER_MAX_TOKENS`). Reasoning tiers
  (expensive/medium/reminder) send no cap; only the label-only `cheap` tier is
  capped, so structured-JSON output is never truncated.
- `app/graph/nodes/intake.py`: `_parse_intake_response` returns `None` on parse
  failure; `intake_node` saves the user's **raw message** as a plain task
  (preserve capture), emits an `intake_parse_failed` ops alert, and returns an
  honest confirmation that flags the un-captured timing — never fakes a reminder.

## Regression Tests

**Output-cap (unit):** test lives in `tests/unit/test_models.py` —
`test_max_tokens_per_tier` asserts reasoning tiers send no `max_tokens` and
`cheap` keeps its cap; `test_llm_constructs_chatopenai_with_expected_kwargs`
asserts the `medium` tier carries no cap.

**Parser (unit):** test lives in `tests/unit/test_intake_parse.py` — truncated
and non-JSON responses return `None`; valid JSON returns a dict.

**Node behavior (integration):** test lives in
`tests/integration/test_intake.py` —
`test_unparseable_output_saves_raw_task_and_alerts` asserts a raw task is saved
titled from the user's words, no reminder is fabricated, an `intake_parse_failed`
ops alert is emitted, and the user-visible message is honest (not "Got it —
added.").

`test_unparseable_output_with_notion_down_does_not_claim_capture` covers the
second-order case: the parse-failure handler saves the raw message to preserve
capture, so if *that* save fails there is nothing on the list. The handler must
let the exception reach the node's error path ("mind sending it again?") rather
than catching it and replying "Added that to your list" over an empty Notion —
which would reproduce this exact bug one level below the parse fix.

**Cross-model efficacy (eval, live-gated):** the regression fixture lives at
`tests/evals/fixtures/intake/remind_deadline_before_time.yaml`, exercised by
`tests/evals/test_evals.py`. A capable model confirms a reminder for the
deadline; an underpowered model that truncates fails the `regex_require`. Run on
demand:

```
ENABLE_LIVE_LLM_EVALS=true EVAL_MODELS=claude-sonnet-4-6 \
  pytest tests/evals/test_evals.py -k remind_deadline_before_time -v
```
