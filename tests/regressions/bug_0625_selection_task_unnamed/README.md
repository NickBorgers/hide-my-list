# Bug 0625: Selection Suggests a Task Without Naming It

**Issue:** #625

## Bug Story

`selection_node` attached a valid `selected_task_id` to an outbound draft while
the user-visible body identified the task only by attribute:

> "Perfect timing - how about this focus task? It matches your 30 minutes and neutral mood."

`send_node` does not render the attached page id, so the user received a
suggestion with nothing to act on.

This is the second occurrence of one failure class. `bug_0618_rejection_alt_task_unnamed`
is the first — same shape, different node. The selection prompt still carried the
bracketed placeholder `[task]`, which a model reads as an instruction to
paraphrase rather than a slot to fill with an exact title.

## Fix

The fix targets the class, not just this node:

- `app/graph/nodes/_task_token.py` owns `{task}` substitution for every node.
- `OutboundDraft.notion_page_title` lets a draft assert "this body names the task".
- `send_node` enforces that assertion for every draft before sending, so any
  node — including ones added later — is covered without its own guard.
- `tests/unit/test_prompt_placeholders.py` fails any prompt that reintroduces a
  bracketed task placeholder.
- The dead `_*_SYSTEM_PROMPT` constants in the node modules were removed. They
  duplicated the Jinja templates and drifted silently; the 0618 fix edited one
  of these dead copies.

## Regression Tests

- `test_selected_task_title.py` replays the reported shape — model selects a
  valid task id but writes "this focus task" — and asserts the draft carries the
  title and the delivered message names it.
- The same file asserts exact `{task}` substitution end to end through `send_node`.

Related coverage: `tests/unit/test_send_node_task_title.py` pins the chokepoint
behavior directly, and the eval fixture
`tests/evals/fixtures/selection/short_time_window.yaml` carries a judge contract
requiring the suggestion to name the specific task.
