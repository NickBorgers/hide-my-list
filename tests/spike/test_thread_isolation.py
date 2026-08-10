"""PR-B1 spike: per-peer thread isolation under concurrency.

Validates that two simultaneous graph.ainvoke() calls with different thread_ids
do not bleed state. Uses MemorySaver so no Postgres is needed.
"""
from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.graph import build_graph


@pytest.mark.asyncio
async def test_concurrent_peer_isolation() -> None:
    """Two peers invoked concurrently get independent pending_outbound."""
    graph = build_graph(checkpointer=MemorySaver())

    result_a, result_b = await asyncio.gather(
        graph.ainvoke(
            {"peer": "<spike-peer-a>", "incoming": "message from A"},
            config={"configurable": {"thread_id": "<spike-peer-a>"}},
        ),
        graph.ainvoke(
            {"peer": "<spike-peer-b>", "incoming": "message from B"},
            config={"configurable": {"thread_id": "<spike-peer-b>"}},
        ),
    )

    # Each peer gets its own outbound
    assert result_a["pending_outbound"][0]["recipient"] == "<spike-peer-a>"
    assert result_b["pending_outbound"][0]["recipient"] == "<spike-peer-b>"

    # No state bleed: A's outbound does not reference B, and vice versa
    for item in result_a["pending_outbound"]:
        assert "<spike-peer-b>" not in item["body"]
    for item in result_b["pending_outbound"]:
        assert "<spike-peer-a>" not in item["body"]


@pytest.mark.asyncio
async def test_sequential_peer_isolation() -> None:
    """Two peers in sequence maintain independent conversation state."""
    graph = build_graph(checkpointer=MemorySaver())

    await graph.ainvoke(
        {"peer": "<spike-peer-x>", "incoming": "x says hello"},
        config={"configurable": {"thread_id": "<spike-peer-x>"}},
    )
    result_y = await graph.ainvoke(
        {"peer": "<spike-peer-y>", "incoming": "y says hello"},
        config={"configurable": {"thread_id": "<spike-peer-y>"}},
    )

    assert result_y["pending_outbound"][0]["recipient"] == "<spike-peer-y>"
    # Y's messages should not contain X's peer identifier
    for item in result_y["pending_outbound"]:
        assert "<spike-peer-x>" not in item["body"]
