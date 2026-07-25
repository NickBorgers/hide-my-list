"""CANNOT_FINISH node: shame-safe breakdown handling.

When the user indicates they can't finish a task, gathers progress info and
creates sub-tasks for the remaining work.

Implements docs/ai-prompts/cannot-finish.md behavior.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def cannot_finish_node(state: State) -> dict[str, Any]:
    """CANNOT_FINISH handler: gather progress and break down remaining work."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")

        task_title = (active_task.get("title") or "").strip() if active_task else ""
        task_title = task_title or "your task"
        page_id = active_task.get("page_id", "") if active_task else ""
        time_estimate = active_task.get("time_estimate", 30) if active_task else 30

        prompt_text = render_with_defaults(
            "cannot_finish.md.j2",
            {
                "task_title": task_title,
                "time_estimate": time_estimate,
                "user_message": incoming,
            },
            defaults={
                "task_title": "your task",
                "time_estimate": 30,
                "user_message": "",
            },
        )

        model = llm("medium", caller="cannot_finish")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=incoming),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message = _parse_cannot_finish_response(response_text)

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }
        real_title = active_task.get("title") if active_task else None
        if real_title:
            draft["notion_page_title"] = real_title

        log.info("cannot_finish_node.response", peer=peer, page_id=page_id)
        return {
            "pending_outbound": [draft],
            "conversation_state": "active",
        }

    except Exception:
        log.exception("cannot_finish_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "No worries — that task was bigger than it looked. What did you get into before stopping?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_cannot_finish_response(response_text: str) -> str:
    """Extract the user-facing message from the LLM JSON response.

    Only `progress_question` (phase=ask_progress) and `next_sub_task_message`
    (phase=analyze_remaining) are user-facing. Models fill any `user_message`
    field with an echo of the user's own words, so it must never be selected —
    replying to "this is too big" with "this is too big" is the failure mode
    this ordering guards against.
    """
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            msg = data.get("progress_question") or data.get("next_sub_task_message")
            if msg:
                return str(msg)
            # Valid JSON with no user-facing field: never send raw JSON (or an
            # echoed user_message) to the user — ask the progress question.
            return "No worries — what did you get into before stopping?"
        except json.JSONDecodeError:
            pass
    return "No worries — what did you get into before stopping?"
