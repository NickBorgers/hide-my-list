# Bug 0618: Rejection Alternative Task Unnamed

**Issue:** #618

## Bug Story

`rejection_node` could attach a valid alternative task page id to an outbound
draft while sending a message that described the task only by duration. The
user-visible body did not include the task title, and `send_node` does not render
the attached page id, so the conversation had no actionable next task.

## Fix

The rejection prompt uses a literal `{task}` placeholder whenever a selected
alternative task appears in `user_message`. `rejection_node` replaces that token
with the exact title for `alternative_task_id`. If the model returns an
alternative id without the placeholder, the node appends a short deterministic
sentence naming the selected task.

## Regression Tests

Tests live in this directory:

- `test_alternative_task_title.py` replays the broken shape where the model
  selects an alternative id but omits `{task}` from the message, and asserts the
  sent body names the selected task.
- The same file asserts the `{task}` prompt convention is replaced with the
  exact selected title.

The eval fixture at `tests/evals/fixtures/rejection/not_that_one.yaml` also
contains a judge contract requiring the response to name the specific
alternative task when an alternative is suggested.
