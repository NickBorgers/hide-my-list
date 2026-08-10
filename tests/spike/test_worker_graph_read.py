"""PR-B1 spike: worker-to-graph state read pattern.

Validates the documented pattern: a worker writes to the recent_outbound table
(outside the graph), and the graph node reads it via a direct DB query at turn start.

Uses an in-memory dict to simulate the Postgres table, since this spike runs
without a live database.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from app.graph.state import State

# Simulated recent_outbound table (keyed by peer)
_FAKE_RECENT_OUTBOUND: dict[str, list[dict]] = {}


def _worker_write_recent_outbound(peer: str, entry: dict) -> None:
    """Simulate a reminder worker writing to recent_outbound."""
    _FAKE_RECENT_OUTBOUND.setdefault(peer, []).append(entry)


def _graph_read_recent_outbound(peer: str) -> list[dict]:
    """Simulate a graph node reading from recent_outbound."""
    return [e for e in _FAKE_RECENT_OUTBOUND.get(peer, []) if e.get("awaiting_reply")]


@pytest.mark.asyncio
async def test_worker_written_row_visible_to_graph_node() -> None:
    """A row written by the worker is visible to the graph node on the next turn.

    This validates the pattern: recent_outbound is NOT part of LangGraph State.
    It lives in Postgres and is read by graph nodes at turn start via direct query.
    """
    peer = "<spike-worker-read>"

    # Worker writes a recent_outbound row (simulates reminder delivery)
    _worker_write_recent_outbound(peer, {
        "notion_page_id": "<page-spike-001>",
        "title": "Placeholder task title",
        "reminder_type": "reminder",
        "awaiting_reply": True,
    })

    # Graph node reads from "Postgres" at turn start
    read_rows: list[dict] = []

    def classifier_node(state: State) -> dict:
        rows = _graph_read_recent_outbound(state.get("peer", ""))
        read_rows.extend(rows)
        return {
            "pending_outbound": [
                {"recipient": state.get("peer", ""), "body": "ack", "notion_page_id": None}
            ]
        }

    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("classify", classifier_node)
    builder.set_entry_point("classify")
    builder.add_edge("classify", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    await graph.ainvoke(
        {"peer": peer, "incoming": "I did it"},
        config={"configurable": {"thread_id": peer}},
    )

    # The node saw the worker-written row
    assert len(read_rows) == 1
    assert read_rows[0]["notion_page_id"] == "<page-spike-001>"
    assert read_rows[0]["awaiting_reply"] is True


@pytest.mark.asyncio
async def test_no_recent_outbound_returns_empty() -> None:
    """When no recent_outbound exists for a peer, the graph node gets empty list."""
    peer = "<spike-no-recent>"

    seen_rows: list[list] = []

    def classifier_node(state: State) -> dict:
        rows = _graph_read_recent_outbound(state.get("peer", ""))
        seen_rows.append(rows)
        return {
            "pending_outbound": [
                {"recipient": state.get("peer", ""), "body": "ack", "notion_page_id": None}
            ]
        }

    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("classify", classifier_node)
    builder.set_entry_point("classify")
    builder.add_edge("classify", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    await graph.ainvoke(
        {"peer": peer, "incoming": "hello"},
        config={"configurable": {"thread_id": peer}},
    )

    assert seen_rows == [[]]
