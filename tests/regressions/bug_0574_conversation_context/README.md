# Bug 0574: Cross-Message Conversation Context Lost

**Issue:** #574
**Fix PR:** #574

## Bug Story

After a user described a task, follow-up messages like "I need to do it by Friday"
or short pronoun references ("it", "that") were classified as isolated CHAT turns
with no memory of the prior exchange. The bot would ask "What's the 'it'?" and
re-plan from scratch.

Root cause: the `messages` channel in LangGraph state was declared with the
`add_messages` reducer but no node ever returned `{"messages": ...}`, so the
list was always empty. `classify_intent` also never read history. Postgres
checkpointing per `thread_id=peer` worked correctly; the channel simply had
nothing to checkpoint or pass to the classifier.

Fix: `send_node` now writes the turn's user message and each outbound reply to
`state["messages"]`. `classify_intent` windows the last five messages from state
and includes them in the `Prior conversation` block of the intent detection prompt.

## Regression Tests

**Unit (CI gate):** test lives in `tests/unit/test_conversation_context.py`.
Covers `send_node` write behavior (live + dormant paths) and classifier reads
from windowed state history.
