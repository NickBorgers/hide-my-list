"""NEED_HELP node: breakdown assistance.

When the user needs help starting or continuing a task, provides specific,
actionable guidance matched to their confidence level.

Implements docs/ai-prompts/breakdown.md behavior.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def need_help_node(state: State) -> dict[str, Any]:
    """NEED_HELP handler: provide actionable breakdown guidance."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")

        if not active_task:
            no_task_draft: OutboundDraft = {
                "recipient": peer,
                "body": "Let's get you a task first! How much time do you have?",
                "notion_page_id": None,
            }
            return {"pending_outbound": [no_task_draft]}

        task_title = (active_task.get("title") or "").strip() or "your task"
        page_id = active_task.get("page_id", "")
        # inline_steps may be stored in active_task or fetched from Notion
        inline_steps = active_task.get("inline_steps", "No steps recorded yet.")

        prompt_text = render_with_defaults(
            "need_help.md.j2",
            {
                "task_title": task_title,
                "inline_steps": inline_steps,
                "user_message": incoming,
            },
            defaults={
                "task_title": "your task",
                "inline_steps": "No steps available.",
                "user_message": "",
            },
        )

        model = llm("medium", caller="need_help")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message = _parse_need_help_response(response_text)

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }
        real_title = active_task.get("title")
        if real_title:
            draft["notion_page_title"] = real_title

        log.info("need_help_node.response", peer=peer)
        return {"pending_outbound": [draft]}

    except Exception:
        log.exception("need_help_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Let's make this tiny. What's the very first physical thing you need to do?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_need_help_response(response_text: str) -> str:
    """Extract user_message from LLM JSON response."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            msg = data.get("user_message")
            if msg:
                return str(msg)
        except json.JSONDecodeError:
            pass
    return response_text[:400] if response_text else "Let's break this down step by step."
