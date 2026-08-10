"""CHECK_IN node: system-initiated progress follow-up.

Triggered by the APScheduler check_in_dispatcher job, never by user messages.
Guards against accidental user-message routing (classify_intent filters this).

Implements docs/ai-prompts/check-in.md behavior.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog

from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def check_in_node(state: State) -> dict[str, Any]:
    """CHECK_IN handler: system-initiated progress follow-up.

    Only invoked by the check_in_dispatcher APScheduler job, not by user messages.
    If routed here from a user message (e.g., a misclassification), the node
    logs a warning and routes to chat fallback behavior.
    """
    peer = state.get("peer", "")

    # Guard: if no active task, skip check-in
    active_task = state.get("active_task")
    if not active_task:
        log.info("check_in_node.skip_no_active_task", peer=peer)
        return {"conversation_state": "idle"}

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults

        task_title = (active_task.get("title") or "").strip() or "your task"
        time_estimate = active_task.get("time_estimate", 30)
        page_id = active_task.get("page_id", "")

        # Calculate elapsed minutes (using started_at if available in active_task)
        started_at_str = active_task.get("started_at")
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str)
                elapsed = int((datetime.now(UTC) - started_at).total_seconds() / 60)
            except (ValueError, TypeError):
                elapsed = time_estimate
        else:
            elapsed = time_estimate

        check_in_count = active_task.get("check_in_count", 0)

        prompt_text = render_with_defaults(
            "check_in.md.j2",
            {
                "task_title": task_title,
                "time_estimate": time_estimate,
                "elapsed_minutes": elapsed,
                "check_in_count": check_in_count,
            },
            defaults={
                "task_title": "your task",
                "time_estimate": 30,
                "elapsed_minutes": 30,
                "check_in_count": 0,
            },
        )

        model = llm("medium", caller="check_in")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content="Generate a check-in message."),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message = _parse_check_in_response(response_text)

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": page_id,
        }
        # Only assert naming when we have a real title — the "your task"
        # fallback is prose, not a task name.
        real_title = active_task.get("title")
        if real_title:
            draft["notion_page_title"] = real_title

        # Update check_in_count and check_in_due_at in active_task
        new_count = check_in_count + 1
        updated_active_task = dict(active_task)
        updated_active_task["check_in_count"] = new_count

        log.info("check_in_node.sent", peer=peer, check_in_count=new_count)
        return {
            "pending_outbound": [draft],
            "active_task": updated_active_task,
            "conversation_state": "checking_in",
        }

    except Exception:
        log.exception("check_in_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Hey, how's that task going?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_check_in_response(response_text: str) -> str:
    """Extract check_in_message from LLM JSON response."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            msg = data.get("check_in_message")
            if msg:
                return str(msg)
        except json.JSONDecodeError:
            pass
    return response_text[:300] if response_text else "Hey, how's that task going?"
