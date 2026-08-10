"""GET_TASK node: task selection using scoring algorithm.

Reads pending tasks from Notion, scores them per docs/ai-prompts/selection.md,
and drafts a suggestion into pending_outbound.

Uses expensive-tier LLM for nuanced scoring and suggestion text.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

import structlog

from app.graph.state import ActiveTask, OutboundDraft, State

log = structlog.get_logger(__name__)

class _SimplifiedTask(TypedDict):
    id: str
    title: str
    work_type: str
    urgency: int
    time_estimate: int
    energy_required: str
    rejection_count: int


def _mood_to_work_type(mood: str | None) -> str:
    """Map mood string to preferred work type."""
    if not mood:
        return "any"
    mood_lower = mood.lower()
    if any(w in mood_lower for w in ("focus", "sharp", "concentrate")):
        return "focus"
    if any(w in mood_lower for w in ("creative", "inspired", "imaginative")):
        return "creative"
    if any(w in mood_lower for w in ("social", "energetic", "talkative")):
        return "social"
    if any(w in mood_lower for w in ("tired", "low", "exhausted", "slow")):
        return "independent"
    return "any"


def _time_of_day() -> str:
    """Return a string label for the current UTC hour."""
    hour = datetime.now(UTC).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


async def selection_node(state: State) -> dict[str, Any]:
    """GET_TASK handler: query Notion, score tasks with the LLM, and populate pending_outbound."""
    peer = state.get("peer", "")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.models import llm
        from app.tools import notion

        available_minutes = state.get("available_minutes") or 30
        mood = state.get("mood")
        preferred_work_type = _mood_to_work_type(mood)
        time_of_day = _time_of_day()

        # Fetch pending tasks from Notion
        tasks_raw = await notion.query_pending()
        tasks = tasks_raw.get("results", [])

        # Build simplified task list for the prompt
        simplified: list[_SimplifiedTask] = []
        for task in tasks:
            props = task.get("properties", {})
            simplified.append({
                "id": task.get("id", ""),
                "title": _extract_title(props),
                "work_type": _extract_select(props, "Work Type"),
                "urgency": _extract_number(props, "Urgency", 50),
                "time_estimate": _extract_number(props, "Time Estimate (min)", 30),
                "energy_required": _extract_select(props, "Energy Required"),
                "rejection_count": _extract_number(props, "Rejection Count", 0),
            })

        tasks_json = json.dumps(simplified, indent=2)

        # Load and render the selection prompt
        from app.prompts.loader import render_with_defaults
        prompt_context = {
            "available_minutes": available_minutes,
            "mood": mood or "neutral",
            "preferred_work_type": preferred_work_type,
            "time_of_day": time_of_day,
            "tasks_json": tasks_json,
        }
        prompt_text = render_with_defaults("selection.md.j2", prompt_context)

        model = llm("expensive", caller="selection")
        messages = [
            SystemMessage(content=prompt_text),
            HumanMessage(content="Select the best task for me right now."),
        ]

        response = await model.ainvoke(messages)
        response_text = str(response.content).strip()

        # Parse JSON response from LLM
        user_message, selected_page_id = _parse_selection_response(response_text, peer)

        # The prompt writes the literal {task} token; the title comes from the
        # task list we scored, never from the model. send_node substitutes it.
        selected_title = next(
            (t["title"] for t in simplified if t["id"] == selected_page_id and t["title"]),
            None,
        )

        draft: OutboundDraft = {
            "recipient": peer,
            "body": user_message,
            "notion_page_id": selected_page_id,
        }
        if selected_title:
            draft["notion_page_title"] = selected_title

        # Mark selected task In Progress and set active_task in state.
        # This is required for COMPLETE/reward to work correctly (psy-001):
        # without active_task set here, the complete node skips Notion completion
        # and maybe_reward, breaking the dopamine timing loop.
        active_task: ActiveTask | None = None
        if selected_page_id:
            try:
                await notion.update_status(selected_page_id, "In Progress")
            except Exception:
                log.exception("selection_node.mark_in_progress_failed", notion_page_id=selected_page_id)

            # Build a minimal ActiveTask from the simplified task list
            selected_simplified = next((t for t in simplified if t["id"] == selected_page_id), None)
            active_task = ActiveTask(
                page_id=selected_page_id,
                title=selected_simplified["title"] if selected_simplified else "",
                status="In Progress",
                selected_at=datetime.now(UTC).isoformat(),
                work_type=selected_simplified["work_type"] if selected_simplified else "",
                urgency=selected_simplified["urgency"] if selected_simplified else 50,
                time_estimate=selected_simplified["time_estimate"] if selected_simplified else 30,
                energy_required=(
                    selected_simplified["energy_required"] if selected_simplified else "Medium"
                ),
                rejection_count=selected_simplified["rejection_count"] if selected_simplified else 0,
            )

        log.info("selection_node.suggestion", notion_page_id=selected_page_id)
        return {
            "pending_outbound": [draft],
            "active_task": active_task,
            "conversation_state": "active" if active_task else "selection",
        }

    except Exception:
        log.exception("selection_node.error")
        fallback: OutboundDraft = {
            "recipient": peer,
            "body": "Having trouble finding a task right now. Try again in a moment?",
            "notion_page_id": None,
        }
        return {"pending_outbound": [fallback]}


def _extract_title(props: dict[str, Any]) -> str:
    """Extract the title string from a Notion page properties dict."""
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
    """Extract a select property value."""
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select") or {}
    if not isinstance(sel, dict):
        return ""
    name = sel.get("name", "")
    return name if isinstance(name, str) else ""


def _extract_number(props: dict[str, Any], key: str, default: int = 0) -> int:
    """Extract a number property value."""
    prop = props.get(key, {})
    if not isinstance(prop, dict):
        return default
    num = prop.get("number")
    if num is None:
        return default
    return int(num)


def _parse_selection_response(response_text: str, peer: str) -> tuple[str, str | None]:
    """Parse the LLM's JSON selection response. Returns (user_message, page_id)."""
    import re

    # Try to extract JSON block. The pattern must tolerate braces *inside* the
    # payload — user_message carries the literal {task} token — so it cannot
    # exclude brace characters. Greedy + DOTALL matches to the last brace,
    # consistent with the other nodes' parsers.
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            loaded = json.loads(json_match.group())
            if not isinstance(loaded, dict):
                return response_text[:500], None
            data = cast(dict[str, Any], loaded)
            user_message = data.get("user_message", "")
            selected_id = data.get("selected_task_id") or None
            if isinstance(user_message, str) and user_message:
                return user_message, selected_id if isinstance(selected_id, str) else None
        except json.JSONDecodeError:
            pass

    # Fallback: use the raw response text as the user message
    user_message = response_text[:500] if response_text else "No suitable task found right now."
    return user_message, None
