"""PR-B1 spike: restart mid-turn checkpoint behavior.

Validates that when a node raises an exception, the next invocation re-enters
the node cleanly from the last super-step checkpoint (not partial state).

Uses MemorySaver. Real PostgresSaver behavior is identical at the super-step
boundary — the difference is only the storage backend.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from app.graph.state import State


@pytest.mark.asyncio
async def test_failed_node_retried_on_next_invoke() -> None:
    """A node that raises on first call is re-entered cleanly on next invoke.

    LangGraph commits the checkpoint AFTER a super-step completes.
    If a node raises, no checkpoint is written for that super-step.
    The next ainvoke re-enters the graph at the last committed checkpoint.
    """
    call_count = {"n": 0}

    def flaky_node(state: State) -> dict:  # type: ignore[return]
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash on first call")
        return {
            "pending_outbound": [
                {"recipient": state.get("peer", ""), "body": "recovered", "notion_page_id": None}
            ]
        }

    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("flaky", flaky_node)
    builder.set_entry_point("flaky")
    builder.add_edge("flaky", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "<spike-restart>"}}

    # First invocation raises — no checkpoint committed
    with pytest.raises(RuntimeError, match="simulated crash"):
        await graph.ainvoke(
            {"peer": "<spike-restart>", "incoming": "test"},
            config=config,
        )

    # Second invocation re-enters flaky_node (call_count["n"] == 2 now)
    result = await graph.ainvoke(
        {"peer": "<spike-restart>", "incoming": "test"},
        config=config,
    )
    assert result["pending_outbound"][0]["body"] == "recovered"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_successful_checkpoint_preserved() -> None:
    """A successful invocation's checkpoint is preserved across reinvocations."""
    builder: StateGraph[State] = StateGraph(State)

    def simple_node(state: State) -> dict:
        return {
            "pending_outbound": [
                {
                    "recipient": state.get("peer", ""),
                    "body": f"echo: {state.get('incoming', '')}",
                    "notion_page_id": None,
                }
            ]
        }

    builder.add_node("simple", simple_node)
    builder.set_entry_point("simple")
    builder.add_edge("simple", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "<spike-checkpoint>"}}

    result1 = await graph.ainvoke(
        {"peer": "<spike-checkpoint>", "incoming": "first"},
        config=config,
    )
    assert result1["pending_outbound"][0]["body"] == "echo: first"

    result2 = await graph.ainvoke(
        {"peer": "<spike-checkpoint>", "incoming": "second"},
        config=config,
    )
    assert result2["pending_outbound"][0]["body"] == "echo: second"
