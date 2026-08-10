# Bug #655: COMPLETE must resolve the task named in the message

COMPLETE resolved its target from the checkpointed `active_task` and an unresolved
`recent_outbound` reminder, and never from the message text. Both sources expire after
24h, so a completion message that named its task outright could not be resolved at all:
the node logged a fully null `complete_node.resolved_target` and asked which task was
meant. The answer to that question resolved the same way and produced the same reply.

The regression tests assert that a task named in the message resolves on its own, that a
named task outranks an active task pointing elsewhere rather than completing the wrong
page, and that the lookup stays additive — a message naming a task the user has NOT
finished, a sub-threshold match, and a Notion failure during matching each leave the
previous resolution intact.
