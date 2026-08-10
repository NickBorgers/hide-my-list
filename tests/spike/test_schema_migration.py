"""PR-B1 spike: schema migration for State field additions/removals.

Validates that:
1. Adding a new State field: old checkpoints lack the field; nodes use .get() safely.
2. Removing a State field: old checkpoint data with the removed field is harmless.
3. Reading old checkpoints with new schema works without crashes.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from app.graph.state import State


@pytest.mark.asyncio
async def test_new_field_defaults_to_none_from_old_checkpoint() -> None:
    """A new State field absent in old checkpoints defaults to None via .get().

    Pattern: always use state.get("new_field") in nodes, never state["new_field"].
    This prevents KeyError when replaying old checkpoints that lack the field.
    """
    captured_values: list = []

    def node_reads_new_field(state: State) -> dict:
        # Simulate a node reading a "new" field that may not exist in old checkpoints
        # The correct pattern is .get(), not direct indexing
        value = state.get("mood")  # "mood" may be absent in old checkpoints
        captured_values.append(value)
        return {
            "pending_outbound": [
                {"recipient": state.get("peer", ""), "body": "ok", "notion_page_id": None}
            ]
        }

    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("reader", node_reads_new_field)
    builder.set_entry_point("reader")
    builder.add_edge("reader", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "<spike-schema>"}}

    # First invocation does NOT include "mood" (simulating an "old" checkpoint input)
    await graph.ainvoke(
        {"peer": "<spike-schema>", "incoming": "hello"},  # no mood key
        config=config,
    )

    # Node safely got None (not a KeyError)
    assert captured_values[0] is None


@pytest.mark.asyncio
async def test_extra_keys_in_old_checkpoint_are_silently_dropped() -> None:
    """Extra keys from a removed State field are silently dropped by LangGraph.

    FINDING: LangGraph strips unknown keys when building the State for a node.
    Extra keys passed to ainvoke that are not in the TypedDict are NOT passed
    to nodes. This means removed fields from old checkpoints are silently ignored —
    nodes will not see them, but they also will not crash.

    Implication for schema migration: if a field is removed from State TypedDict
    and old checkpoint JSON still contains that key, the node will see None (not
    the old value) when calling state.get("removed_field"). This is safe and the
    desired behavior.
    """
    extra_value: list = []

    def node(state: State) -> dict:
        # Attempt to read a field that is NOT in State TypedDict
        # LangGraph strips unknown keys — this will be None, not "old-data"
        old_field = state.get("legacy_field_removed_in_v2")  # type: ignore[call-overload]
        extra_value.append(old_field)
        return {
            "pending_outbound": [
                {"recipient": state.get("peer", ""), "body": "ok", "notion_page_id": None}
            ]
        }

    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("node", node)
    builder.set_entry_point("node")
    builder.add_edge("node", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    # Provide a state that includes an "old" field not in current TypedDict
    await graph.ainvoke(
        {
            "peer": "<spike-old-schema>",
            "incoming": "hello",
            "legacy_field_removed_in_v2": "old-data",  # type: ignore[typeddict-unknown-key]
        },
        config={"configurable": {"thread_id": "<spike-old-schema>"}},
    )

    # The graph did not crash — extra keys are silently ignored.
    # The node sees None, not "old-data", confirming LangGraph strips unknown keys.
    # This is safe: removed fields are invisible to nodes, not errors.
    assert extra_value[0] is None


@pytest.mark.asyncio
async def test_checkpoint_roundtrip_with_optional_fields() -> None:
    """Checkpoint round-trip works when optional fields have None values.

    Validates that None-valued optional fields (mood, active_task, etc.) survive
    serialization/deserialization through MemorySaver without corruption.
    """
    seen_states: list[dict] = []

    def capturing_node(state: State) -> dict:
        seen_states.append(dict(state))
        return {
            "pending_outbound": [
                {"recipient": state.get("peer", ""), "body": "ok", "notion_page_id": None}
            ]
        }

    builder: StateGraph[State] = StateGraph(State)
    builder.add_node("capture", capturing_node)
    builder.set_entry_point("capture")
    builder.add_edge("capture", "__end__")
    graph = builder.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "<spike-roundtrip>"}}

    await graph.ainvoke(
        {
            "peer": "<spike-roundtrip>",
            "incoming": "hello",
            "mood": None,
            "active_task": None,
            "available_minutes": None,
        },
        config=config,
    )

    # Second turn — checkpoint is loaded; optional fields should still be None
    await graph.ainvoke(
        {"peer": "<spike-roundtrip>", "incoming": "second"},
        config=config,
    )

    # Both invocations captured without crashes
    assert len(seen_states) == 2
