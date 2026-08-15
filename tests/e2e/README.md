# E2E conversation layer

Scripted multi-turn conversations driven through the **real** compiled LangGraph
graph, against a **real** Postgres checkpointer, using the **real** LLM via the
LiteLLM proxy. Notion and Signal are faked (`tests/support/`); the model is not.

The unit of test is a conversation, not a node call. Every other layer calls node
functions directly with a hand-built `State` dict, which cannot observe the seam
where turn N writes state and turn N+M reads it — the seam bug #641 lived on.

## Running locally

```bash
docker run -d --rm --name hml-e2e-pg \
  -e POSTGRES_USER=hml -e POSTGRES_PASSWORD=hml -e POSTGRES_DB=hml \
  -p 5432:5432 postgres:16-alpine

ENABLE_E2E_CONVERSATIONS=true \
DATABASE_URL=postgresql://hml:hml@localhost:5432/hml \
LLM_PROXY_BASE_URL=https://llm.featherback-mermaid.ts.net/v1 \
LLM_PROXY_API_KEY=fake-key \
SIGNAL_ACCOUNT=+15550009999 \
REWARD_ARTIFACTS_DIR=/tmp/hml-reward-artifacts \
pytest tests/e2e/ -q
```

The database must be named `hml`: `migrations/0005_readonly_user.sql` issues a
literal `GRANT CONNECT ON DATABASE hml`. The proxy is tailnet-only, so this needs
a machine on the tailnet — the same reason the CI job runs on the `homelab`
runner rather than a GitHub-hosted one.

Without `ENABLE_E2E_CONVERSATIONS`, or with any required variable missing, the
whole directory skips.

## Failure taxonomy

Two failure types, and the distinction matters when triaging:

| Type | Meaning | Response |
|---|---|---|
| `IntentMisrouteError` | The classifier chose a different intent than the scenario declared. The state machine is intact; the **model** disagreed. | Read the prompt diff. If the model is right and the scenario is wrong, fix the scenario. Never add a retry — retrying hides exactly the drift this layer detects. |
| `AssertionError` | An invariant (`tests/support/invariants.py`) or a scenario's own `Expect` contract broke. | A code regression. Treat as a real bug. |

`E2E_MISROUTE_BLOCKING` is not implemented yet; misroutes fail the run. If model
drift proves noisy in practice, that knob is the intended escape hatch.

## Writing a scenario

Assert what the system **did**, not what it **said**. Notion status, which page
ids were written, `recent_outbound.awaiting_reply`, the checkpoint's
`active_task`, and the number of messages sent are all deterministic under a
nondeterministic model. Wording is not.

```python
await conversation.say(
    "done",
    expect=Expect(
        intent="COMPLETE",
        notion_status={page_a: "Completed"},
        notion_untouched=[page_b],
        db_awaiting_reply=0,
        sent_count=1,
    ),
)
```

`regex_require` / `regex_forbid` exist but should stay rare; judged text quality
is the eval layer's job, and it has a model to score it with.

Two rules that are easy to get wrong:

- **Deliver reminders through `conversation.deliver_reminder()`**, never a
  fixture `INSERT INTO recent_outbound`. That INSERT in `reminder_worker` is the
  table's only writer and the row a later COMPLETE resolves against; a fixture
  insert would keep passing with the production INSERT deleted, which is
  precisely the pre-#641 state of the world.
- **Seed preconditions with `seed_active_task()` / `age_active_task()`**, not by
  running extra live turns. It keeps the assertion pointed at the seam and cuts
  the LLM calls a scenario costs.

The invariants in `tests/support/invariants.py` run after every turn
automatically. A scenario only needs to state what is specific to itself.
