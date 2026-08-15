"""Multi-turn conversation harness.

The unit of test here is a *conversation*, not a node call. Bug #641 — the
system being told a task was complete and unable to work out which task — lived
entirely in the seam between turns: `selection_node` writes `active_task` to the
LangGraph checkpoint, `reminder_worker` writes `recent_outbound` to Postgres, and
`complete_node` reads both several turns later. A test that calls one node with a
hand-built State dict cannot see that seam, which is why the whole class stayed
invisible.

Two deliberate choices:

**Entry is through `SignalListener`, not `graph.ainvoke`.** Three things under
test sit upstream of the graph. `thread_id` is derived from the peer E.164
(signal_listener.py) — enter at the graph and the test asserts its own assumption
about checkpoint partitioning, so a change to that derivation would leave every
chain green while every real conversation silently lost its history.
`_extract_peer_and_text` is production's only inbound parser. And the auth gate
plus the read-receipt and typing-indicator background tasks run *concurrently
with* `ainvoke`, which is the only place a background task could race the graph.

**The clock is never faked.** `complete_node` reads `datetime.now(UTC)` while
Postgres reads `now()`; faking one and not the other invents a skew that exists
in no deployment. Staleness is produced by writing backdated values instead —
`age_active_task` through `graph.aupdate_state`, `expire_recent_outbound`
through SQL.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from tests.support.notion_fake import FakeNotion
from tests.support.signal_sink import SentMessage, SignalSink

# How long to wait for one turn to finish traversing the graph. Generous: a live
# reasoning-tier call on a self-hosted model can take tens of seconds.
_TURN_TIMEOUT_SECONDS = float(os.environ.get("E2E_TURN_TIMEOUT_SECONDS", "180"))

# How long to wait when we expect the listener to drop a message without
# invoking the graph (unauthorized peer, unparseable envelope). Bounded because
# the assertion is that nothing happens, and "nothing" has no completion signal.
_DROP_SETTLE_SECONDS = 1.0


class IntentMisrouteError(AssertionError):
    """The classifier chose a different intent than the scenario declared.

    A distinct type because it means something different from a broken
    invariant: the state machine is intact and the *model* disagreed. Nightly
    reports tally the two separately so an operator can tell model drift from a
    code regression at a glance. Never retried — retrying hides the drift this
    layer exists to detect.
    """


@dataclass
class Expect:
    """Per-turn contract.

    Deliberately side-effect-heavy. Under a real model the wording of a reply is
    not reproducible, but which Notion page was written, whether a reminder row
    was resolved, and how many messages went out all are. Text assertions are
    available but should stay rare — judged text quality belongs to the eval
    layer, which has a model to score it with.
    """

    intent: str | None = None
    notion_status: dict[str, str] = field(default_factory=dict)
    notion_untouched: list[str] = field(default_factory=list)
    db_awaiting_reply: int | None = None
    sent_count: int | None = None
    regex_require: list[str] = field(default_factory=list)
    regex_forbid: list[str] = field(default_factory=list)
    allow_events: set[str] = field(default_factory=set)
    allow_duplicate_send: bool = False


@dataclass
class TurnResult:
    """Everything observable about one turn."""

    text: str
    sent: list[SentMessage]
    state: dict[str, Any]
    logs: list[dict[str, Any]]
    notion_writes_since: int
    awaiting_reply_before: int
    awaiting_reply_after: int
    graph_invoked: bool

    @property
    def intent(self) -> str | None:
        value = self.state.get("intent")
        return str(value) if value else None

    @property
    def bodies(self) -> list[str]:
        return [message.body for message in self.sent]


@dataclass
class ReminderDelivery:
    """Result of pushing one reminder through the real worker."""

    reminder_id: uuid.UUID
    notion_page_id: str
    signal_timestamp: int
    body: str


class _ObservedGraph:
    """Delegate that runs the real compiled graph and signals turn completion.

    `SignalListener` accepts `graph=` as a production-supported injection point,
    so the graph under test is the genuine compiled article on a genuine
    Postgres checkpointer — only the completion signal is added.
    """

    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self.last_state: dict[str, Any] | None = None
        self.call_count = 0
        self.done = asyncio.Event()

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> Any:
        self.call_count += 1
        try:
            result = await self._graph.ainvoke(state, config)
            self.last_state = dict(result) if result else {}
            return result
        finally:
            self.done.set()


class Conversation:
    """Drives a scripted multi-turn exchange with one peer."""

    def __init__(
        self,
        *,
        peer: str,
        graph: Any,
        observed: _ObservedGraph,
        notion: FakeNotion,
        signal: SignalSink,
        database_url: str,
        enqueue_envelope: Callable[[dict[str, Any]], None],
        call_meter: Any = None,
    ) -> None:
        self.call_meter = call_meter
        self.peer = peer
        self.graph = graph
        self.notion = notion
        self.signal = signal
        self._observed = observed
        self._database_url = database_url
        self._enqueue = enqueue_envelope
        self._inbound_timestamp = 1_600_000_000_000
        # Pages the peer has actually been shown, plus pages created on their
        # behalf. Invariant I3 asserts no write ever escapes this set.
        self.offered: set[str] = set()

    # -- checkpoint access -------------------------------------------------

    @property
    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.peer}}

    async def state(self) -> dict[str, Any]:
        snapshot = await self.graph.aget_state(self._config)
        return dict(snapshot.values) if snapshot and snapshot.values else {}

    async def seed_active_task(self, **fields: Any) -> None:
        """Write an `active_task` directly into the checkpoint.

        Preconditions are seeded rather than produced by an extra live turn:
        it keeps the assertion pointed at the seam under test instead of at the
        selection prompt, and it halves the LLM calls a scenario costs.
        """
        active_task = {
            "page_id": "",
            "title": "",
            "status": "In Progress",
            "selected_at": datetime.now(UTC).isoformat(),
            **fields,
        }
        page_id = str(active_task.get("page_id") or "")
        if page_id:
            self.offered.add(page_id)
        await self._write_state({"active_task": active_task, "conversation_state": "active"})

    async def age_active_task(self, hours: float) -> None:
        """Backdate the checkpoint's `selected_at` by `hours`.

        `complete_node` expires an `active_task` older than 24h. Reaching that
        state by rewriting the stored timestamp keeps every production clock
        call honest.
        """
        current = await self.state()
        active_task = dict(current.get("active_task") or {})
        if not active_task:
            raise AssertionError("no active_task in the checkpoint to age")
        active_task["selected_at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        await self._write_state({"active_task": active_task})

    async def _write_state(self, values: dict[str, Any]) -> None:
        """Write checkpoint values as if the graph had just finished a turn.

        `as_node` is required: on a thread with no completed run LangGraph cannot
        infer which node an update came from and raises InvalidUpdateError.
        Attributing the write to the terminal `send` node leaves the thread with
        no pending next step, so the following `ainvoke` starts a fresh run at
        `classify_intent` rather than resuming mid-graph.
        """
        await self.graph.aupdate_state(self._config, values, as_node="send")

    # -- database access ---------------------------------------------------

    @asynccontextmanager
    async def db(self) -> AsyncIterator[Any]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(
            self._database_url, autocommit=True
        ) as conn:
            yield conn

    async def awaiting_reply_count(self) -> int:
        async with self.db() as conn:
            cursor = await conn.execute(
                """
                SELECT count(*) FROM recent_outbound
                 WHERE peer = %s AND awaiting_reply = true AND expires_at > now()
                """,
                (self.peer,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def expire_recent_outbound(self) -> None:
        async with self.db() as conn:
            await conn.execute(
                "UPDATE recent_outbound SET expires_at = now() - interval '1 hour' WHERE peer = %s",
                (self.peer,),
            )

    # -- reminders ---------------------------------------------------------

    async def deliver_reminder(
        self, *, page_id: str, body: str, due_at: datetime | None = None
    ) -> ReminderDelivery:
        """Enqueue a reminder and dispatch it through the real worker.

        The `recent_outbound` INSERT in `reminder_worker` is that table's only
        writer, and it is the row a later COMPLETE turn resolves against. A
        fixture `INSERT INTO recent_outbound` here would keep passing with that
        INSERT deleted — which is precisely the pre-#641 state of the world — so
        the reminder has to travel through production code.
        """
        import psycopg

        from app.scheduler.reminder_worker import dispatch_due_reminders
        from app.tools import reminders

        before = self.signal.mark()
        async with await psycopg.AsyncConnection.connect(
            self._database_url, autocommit=False
        ) as conn:
            reminder_id = await reminders.enqueue(
                conn,
                notion_page_id=page_id,
                peer=self.peer,
                body=body,
                due_at=due_at or (datetime.now(UTC) - timedelta(minutes=1)),
                idempotency_key=f"e2e-{uuid.uuid4()}",
            )
            await conn.commit()
            await dispatch_due_reminders(conn, signal_send_fn=self.signal.send_message)

        delivered = self.signal.since(before)
        if len(delivered) != 1:
            raise AssertionError(
                f"expected the worker to deliver exactly one reminder, got {len(delivered)}"
            )

        # Self-check: if the recent_outbound INSERT ever stops running, fail
        # here with a clear cause rather than three turns later as a puzzling
        # "which task did you mean?".
        async with self.db() as conn:
            cursor = await conn.execute(
                """
                SELECT notion_page_id, awaiting_reply FROM recent_outbound
                 WHERE peer = %s AND signal_timestamp = %s
                """,
                (self.peer, delivered[0].timestamp),
            )
            row = await cursor.fetchone()
        if row is None:
            raise AssertionError(
                "reminder_worker delivered the reminder but wrote no recent_outbound row; "
                "a later COMPLETE turn would have nothing to resolve against"
            )
        if not row[1]:
            raise AssertionError("recent_outbound row was written already resolved")

        self.offered.add(page_id)
        return ReminderDelivery(
            reminder_id=reminder_id,
            notion_page_id=page_id,
            signal_timestamp=delivered[0].timestamp,
            body=body,
        )

    # -- turns -------------------------------------------------------------

    def _envelope(self, text: str, *, peer: str) -> dict[str, Any]:
        self._inbound_timestamp += 1
        return {
            "envelope": {
                "source": peer,
                "timestamp": self._inbound_timestamp,
                "dataMessage": {"message": text, "timestamp": self._inbound_timestamp},
            }
        }

    async def say(self, text: str, *, expect: Expect | None = None) -> TurnResult:
        """Send one inbound message and wait for the turn to complete."""
        return await self._turn(
            self._envelope(text, peer=self.peer),
            expect_graph_call=True,
            expect=expect or Expect(),
        )

    async def unauthorized(self, text: str, *, peer: str) -> TurnResult:
        """Send a message from a peer outside the allowlist.

        The listener drops it before the graph, so there is no completion signal
        to wait on — settle briefly and assert on the absence.
        """
        return await self._turn(
            self._envelope(text, peer=peer), expect_graph_call=False, expect=Expect()
        )

    async def _turn(
        self, envelope: dict[str, Any], *, expect_graph_call: bool, expect: Expect
    ) -> TurnResult:
        from tests.support.invariants import assert_expectations, assert_turn_invariants

        sent_cursor = self.signal.mark()
        notion_cursor = self.notion.mark()
        awaiting_before = await self.awaiting_reply_count()
        calls_before = self._observed.call_count
        self._observed.done.clear()

        with structlog.testing.capture_logs() as logs:
            self._enqueue(envelope)
            if expect_graph_call:
                try:
                    await asyncio.wait_for(
                        self._observed.done.wait(), timeout=_TURN_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    raise AssertionError(
                        f"turn did not complete within {_TURN_TIMEOUT_SECONDS}s"
                    ) from None
            else:
                await asyncio.sleep(_DROP_SETTLE_SECONDS)
            # Let the receipt and typing background tasks finish so their
            # effects are inside the captured window.
            for _ in range(20):
                await asyncio.sleep(0)

        if self.call_meter is not None:
            self.call_meter.record(list(logs))

        graph_invoked = self._observed.call_count > calls_before
        if expect_graph_call and not graph_invoked:
            raise AssertionError("the listener never invoked the graph for this turn")

        sent = self.signal.since(sent_cursor)
        state = await self.state() if graph_invoked else {}

        # A page counts as "offered" once it is attached to a draft this turn.
        # Matching drafts to delivered messages by body does not work: send_node
        # rewrites the body when it substitutes the task token, so the draft text
        # and the sent text legitimately differ.
        if sent:
            for draft in state.get("pending_outbound", []) or []:
                page_id = str(draft.get("notion_page_id") or "")
                if page_id:
                    self.offered.add(page_id)
        for write in self.notion.writes[notion_cursor:]:
            if write.op in ("create_task", "create_reminder"):
                self.offered.add(write.page_id)

        result = TurnResult(
            text=" ".join(message.body for message in sent),
            sent=sent,
            state=state,
            logs=list(logs),
            notion_writes_since=notion_cursor,
            awaiting_reply_before=awaiting_before,
            awaiting_reply_after=await self.awaiting_reply_count(),
            graph_invoked=graph_invoked,
        )
        assert_turn_invariants(self, result, expect)
        assert_expectations(self, result, expect)
        return result
