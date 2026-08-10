"""LangGraph graph definition for hide-my-list.

Topology:
  classify_intent -> [conditional route by intent] -> <intent node> -> send -> END

Checkpointer lifecycle:
  AsyncPostgresSaver is an async context manager that must be entered before the
  graph is compiled. Use build_postgres_checkpointer() as an async context manager
  in app startup, then pass the entered instance to build_graph(). For tests,
  pass a MemorySaver directly.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.graph import StateGraph

from app.graph.routing import build_routing_map, classify_intent, route_intent
from app.graph.state import State


@asynccontextmanager
async def build_postgres_checkpointer(
    database_url: str,
) -> AsyncGenerator[Any, None]:
    """Async context manager that sets up and tears down a PostgresSaver.

    Usage:
        async with build_postgres_checkpointer(DATABASE_URL) as cp:
            graph = build_graph(checkpointer=cp)
            ...

    The entered checkpointer is passed to build_graph(); compiling the graph
    with an un-entered AsyncPostgresSaver would leave it without an active
    connection pool.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()
        yield saver


def build_graph(checkpointer: Any = None) -> Any:
    """Build and compile the LangGraph state machine.

    Args:
        checkpointer: An already-entered LangGraph checkpointer instance.
            Pass a MemorySaver for unit tests. In production, pass the
            entered saver from build_postgres_checkpointer().
            When None, falls back to MemorySaver (no persistence).

    Returns:
        Compiled LangGraph app.
    """
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

    from app.graph.nodes.cannot_finish import cannot_finish_node
    from app.graph.nodes.chat import chat_node
    from app.graph.nodes.check_in import check_in_node
    from app.graph.nodes.complete import complete_node
    from app.graph.nodes.intake import intake_node
    from app.graph.nodes.need_help import need_help_node
    from app.graph.nodes.rejection import rejection_node
    from app.graph.nodes.selection import selection_node
    from app.graph.nodes.send import send_node

    builder: StateGraph[State] = StateGraph(State)

    # Intent classifier (entry point)
    builder.add_node("classify_intent", classify_intent)

    # Intent handler nodes
    builder.add_node("intake", intake_node)
    builder.add_node("selection", selection_node)
    builder.add_node("complete", complete_node)
    builder.add_node("rejection", rejection_node)
    builder.add_node("cannot_finish", cannot_finish_node)
    builder.add_node("check_in", check_in_node)
    builder.add_node("need_help", need_help_node)
    builder.add_node("chat", chat_node)

    # Terminal send node
    builder.add_node("send", send_node)

    # Entry point
    builder.set_entry_point("classify_intent")

    # Conditional routing from classifier to intent nodes
    routing_map = build_routing_map()
    builder.add_conditional_edges(
        "classify_intent",
        route_intent,
        routing_map,
    )

    # All intent nodes flow into the terminal send node
    for node_name in routing_map.values():
        builder.add_edge(node_name, "send")

    # Send node is the terminal
    builder.add_edge("send", "__end__")

    return builder.compile(checkpointer=checkpointer)
