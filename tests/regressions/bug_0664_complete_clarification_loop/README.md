# Bug #664: COMPLETE asked the same question three times without ever consulting the model

Three consecutive COMPLETE turns 35 seconds apart each logged a fully null
`complete_node.resolved_target` and sent a byte-identical body — one shared
idempotency key across all three sends. Turn 1 named nothing and resolved from
empty context, correctly. Turns 2 and 3 were the user answering "which task did
you mean?", and both read Notion, scored every open task below
`_TITLE_MATCH_MIN_SCORE`, and returned before building a prompt: the model that
exists to adjudicate the candidate list never saw one. The reply then cleared
`conversation_state` and `active_task` and recorded nothing about having asked,
so each turn re-entered cold and failed the same way.

Three causes, one symptom. Token overlap was acting as a veto rather than a
ranking — a message that describes a task instead of quoting its title scores
zero, and zero was treated as "no candidates" instead of "rank them all". A
question the agent asked left no trace in state, so nothing could tell the
second turn from the first. And once the answer was routed back, the matching
prompt still judged it by the standalone rule — "match only when the message
asserts that candidate is done" — which a bare noun phrase like "the garden
one" can never satisfy, because the assertion was made on the turn before.
Routing without reframing produced the same loop by a longer path.

## Coverage

`test_complete_clarification_loop.py` covers all three at the node and routing
layer: a zero-overlap paraphrase reaching the model, the write bar holding at
0.90 through the widened path, the #655 rejection guard staying scoped to
candidates the message actually overlaps, the attempt count surviving across
turns and terminating, a CHAT-shaped answer routing back to the node that asked
while an expired, malformed, or moved-on-from clarification does not, and the
two prompt framings staying separate — answer mode must not leak into a
standalone completion, where the assertion rule is the guard.

The third cause is the one that argues for this layer's limits. Every
node-level test passed while it was live, because a mocked model returns
whatever the test says it returns regardless of what the prompt asked. Only a
real model reading a real prompt could reject "the garden one", and only the
chain below observed it.

The chain itself lives in `tests/e2e/scenarios/test_complete_clarification.py`,
which runs the real graph against the real checkpointer. That layer is the only
one that can observe the actual complaint — the second reply differing from the
first is a property of two turns, not of one — and it is where the previous two
instances of this bug class (#641, #655) would also have surfaced.
