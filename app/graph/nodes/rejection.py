"""REJECT node: shame-safe rejection handling.

When the user rejects a suggested task, classifies the reason, updates
rejection count in Notion, and suggests an alternative.

Implements docs/ai-prompts/rejection.md behavior.
"""
from __future__ import annotations

import json
import re
from typing import Any, cast

import structlog

from app.graph.nodes._task_token import render_task_token
from app.graph.state import OutboundDraft, State

log = structlog.get_logger(__name__)

async def rejection_node(state: State) -> dict[str, Any]:
    """REJECT handler: classify rejection and suggest alternative."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.prompts.loader import render_with_defaults
        from app.tools import notion

        incoming = state.get("incoming", "")
        active_task = state.get("active_task")
        available_minutes = state.get("available_minutes") or 30
        mood = state.get("mood") or "neutral"

        task_title = (active_task.get("title") or "").strip() if active_task else ""
        task_title = task_title or "the suggested task"
        rejected_page_id = active_task.get("page_id", "") if active_task else ""

        # Fetch remaining tasks for alternative suggestion
        tasks_raw = await notion.query_pending()
        tasks = tasks_raw.get("results", [])
        remaining = [
            {
                "id": t.get("id", ""),
                "title": _extract_title(t.get("properties", {})),
                "work_type": _extract_select(t.get("properties", {}), "Work Type"),
                "time_estimate": _extract_number(t.get("properties", {}), "Time Estimate (min)", 30),
            }
            for t in tasks
            if t.get("id", "") != rejected_page_id
        ]

        # Load rejection prompt
        prompt_text = render_with_defaults(
            "rejection.md.j2",
            {
                "task_title": task_title,
                "rejection_reason": incoming,
                "remaining_tasks_json": json.dumps(remaining[:10], indent=2),
                "available_minutes": available_minutes,
                "mood": mood,
            },
            defaults={
                "task_title": "the suggested task",
                "rejection_reason": "",
                "remaining_tasks_json": "[]",
                "available_minutes": 30,
                "mood": "neutral",
            },
        )

        model = llm("medium", caller="rejection")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content=f"The user said: {incoming!r}"),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        user_message, alternative_id = _parse_rejection_response(response_text)
        alternative_title = _alternative_task_title(alternative_id, remaining)
        user_message = render_task_token(user_message, title=alternative_title)

        # Update rejection count in Notion
        if rejected_page_id:
            try:
                await notion.update_property(
                    rejected_page_id,
                    {
                        "properties": {
                            "Rejection Count": {
                                "number": active_task.get("rejection_count", 0) + 1
                                if active_task
                                else 1
                            }
                        }
                    },
                )
            except Exception:
                log.exception("rejection_node.notion_update_failed", page_id=rejected_page_id)

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": alternative_id,
        }
        if alternative_title:
            draft["notion_page_title"] = alternative_title

        log.info("rejection_node.alternative", peer=peer, alternative_id=alternative_id)
        return {
            "pending_outbound": [draft],
            "active_task": None,
            "conversation_state": "selection",
        }

    except Exception:
        log.exception("rejection_node.error", peer=peer)
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "No problem — that helps me learn. Want me to find something different?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _parse_rejection_response(response_text: str) -> tuple[str, str | None]:
    """Parse LLM JSON response. Returns (user_message, alternative_task_id)."""
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            loaded = json.loads(json_match.group())
            if not isinstance(loaded, dict):
                return response_text[:300], None
            data = cast(dict[str, Any], loaded)
            user_message = data.get("user_message", response_text[:300])
            alternative_id = data.get("alternative_task_id")
            return (
                user_message if isinstance(user_message, str) else response_text[:300],
                alternative_id if isinstance(alternative_id, str) else None,
            )
        except json.JSONDecodeError:
            pass
    return response_text[:300] if response_text else "No problem. Want something different?", None


def _alternative_task_title(
    alternative_id: str | None,
    remaining: list[dict[str, Any]],
) -> str | None:
    """Return the title the model selected as the alternative, if it exists."""
    if not alternative_id:
        return None

    title_by_id = {
        task.get("id"): task.get("title")
        for task in remaining
        if isinstance(task.get("id"), str) and isinstance(task.get("title"), str)
    }
    return title_by_id.get(alternative_id)


def _extract_title(props: dict[str, Any]) -> str:
    title_prop = props.get("Title", {})
    if not isinstance(title_prop, dict):
        return ""
    items = title_prop.get("title", [])
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            plain_text = item.get("plain_text", "")
            if isinstance(plain_text, str):
                parts.append(plain_text)
    return "".join(parts)


def _extract_select(props: dict[str, Any], key: str) -> str:
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select") or {}
    if not isinstance(sel, dict):
        return ""
    name = sel.get("name", "")
    return name if isinstance(name, str) else ""


def _extract_number(props: dict[str, Any], key: str, default: int = 0) -> int:
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return default
    num = prop.get("number")
    return int(num) if num is not None else default
