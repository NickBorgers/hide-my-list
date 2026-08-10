"""CHAT node: friendly fallback for unclassified or general messages.

Uses medium-tier LLM to provide brief, helpful responses.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def chat_node(state: State) -> dict[str, Any]:
    """CHAT handler: general conversation fallback using medium-tier LLM."""
    peer = state.get("peer", "")
    incoming = state.get("incoming", "")

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        # Build conversation context from message history
        messages_history: list[AnyMessage] = state.get("messages", [])
        context_lines: list[str] = []
        for msg in messages_history[-5:]:  # Last 5 messages for context
            role = getattr(msg, "type", "message")
            content = str(getattr(msg, "content", ""))
            context_lines.append(f"{role}: {content[:200]}")
        conversation_context = "\n".join(context_lines) if context_lines else "No prior context."

        prompt_text = render_with_defaults(
            "chat.md.j2",
            {
                "user_message": incoming,
                "conversation_context": conversation_context,
            },
        )

        model = llm("medium", caller="chat")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        # Truncate to 500 chars as a safety measure
        if len(response_text) > 500:
            response_text = response_text[:497] + "..."

        draft = {
            "recipient": peer,
            "body": response_text,
            "notion_page_id": None,
        }

        log.info("chat_node.response", peer=peer)
        return {"pending_outbound": [draft]}

    except Exception:
        log.exception("chat_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Having trouble thinking right now — try again?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}
