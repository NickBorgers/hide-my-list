# Bug 0617: Intake Duplicate Task Creation

**Issue:** #617
**Fix PR:** #619

## Bug Story

A user described one real-world action in one turn, then described the same
underlying action later with different wording. Intake saved both turns as
separate Notion pages. Completing one page left the other open, so selection
could offer the same real-world action again.

Root cause: the main ADD_TASK intake path called `create_task` without checking
open non-reminder tasks for a near-duplicate. Sub-task creation and parse-failure
capture are separate paths and keep their existing behavior.

Fix: before normal task creation, intake queries open non-reminder tasks, runs a
pure lexical shortlist, and asks the cheap model to adjudicate only that
shortlist. A high-confidence match reuses the existing page and applies any new
deadline to it. Any Notion error, model error, ambiguous response, or unparseable
response falls through to normal task creation.

## Regression Tests

**Unit (CI gate):** test lives in `tests/unit/test_intake_dedup.py` and covers lexical
shortlisting, normalization, unrelated tasks, empty lists, and fail-open
behavior for Notion errors and unparseable dedup model output.

**Integration (CI gate):** test lives in `tests/integration/test_intake.py` and covers creating
when no tasks exist, reusing a clear duplicate, applying and scheduling a
deadline on the matched page, creating unrelated work, and preventing disclosure
of unmatched candidates or candidate counts.

**Eval fixture:** test lives in `tests/evals/fixtures/intake/duplicate_task_no_shame.yaml`
guards against shame framing, confirmation questions, and candidate enumeration
in user-visible intake responses.
