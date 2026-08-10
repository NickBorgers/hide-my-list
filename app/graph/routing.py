"""Intent classification and routing for the LangGraph pipeline.

Security note: low-confidence classifications default to CHAT rather than
escalating to a potentially wrong intent. This is a prompt-injection mitigation —
if a malicious message tries to force ADD_TASK or COMPLETE via injection, the
classifier errs toward the safer CHAT fallback.
"""
from __future__ import annotations

from collections.abc import Hashable
from typing import Any

import structlog

from app.graph.state import Intent, State

log = structlog.get_logger(__name__)

_INTENT_SYSTEM_PROMPT = """\
You are an intent classifier for a task management assistant called hide-my-list.

Classify the user's message into EXACTLY ONE of these intents:
- ADD_TASK: User wants to add a new task (mentions something they need to do)
- GET_TASK: User wants something to work on (mentions time available, asks what to do)
- COMPLETE: User finished a task — the one they are on, or one they name (says done, finished, completed)
- REJECT: User doesn't want the suggested task (says no, not that one, something else)
- CANNOT_FINISH: User indicates current task is too large or overwhelming
- NEED_HELP: User wants help breaking down or starting their current task
- CHECK_IN: System-initiated follow-up — NEVER classify user messages as this
- CHAT: General conversation or questions — USE THIS when unsure

If the prior conversation shows an in-progress task discussion, treat short
follow-ups (deadlines, clarifications, pronouns like "it") as continuations of
that intent. For example, if the previous turn was the user describing a task
and the current message is "I need to do it by Friday", classify as ADD_TASK.

Rules:
- If unsure or confidence is low, output CHAT (never guess at a wrong intent)
- CHECK_IN is NEVER triggered by user messages — it is system-only
- Respond with ONLY the intent label, nothing else

Examples:
"I need to call the dentist" → ADD_TASK
"I have 30 minutes" → GET_TASK
"Done!" → COMPLETE
"Not that one" → REJECT
"This is too big" → CANNOT_FINISH
"How do I start?" → NEED_HELP
"Hello" → CHAT
"""


async def classify_intent(state: State) -> dict[str, Intent | None]:
    """Classify the incoming message intent using an LLM.

    Uses the cheap tier, which routes to a think=false model configuration
    in app/models.py — classification needs a label, not reasoning. Defaults
    low-confidence to CHAT as a prompt-injection mitigation.
    """
    incoming = state.get("incoming", "").strip()
    if not incoming:
        return {"intent": "CHAT"}

    try:
        from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

        from app.models import llm

        model = llm("cheap", caller="classify")

        # Pull a small window of prior turns so short follow-ups can resolve
        # against the active discussion (e.g. "by Friday" after an ADD_TASK turn).
        # State.messages is populated by the terminal send node.
        messages_history: list[AnyMessage] = state.get("messages", [])
        context_lines = [
            f"{getattr(m, 'type', 'message')}: {str(getattr(m, 'content', ''))[:200]}"
            for m in messages_history[-5:]
        ]
        prior_context = (
            "\n".join(context_lines) if context_lines else "No prior context."
        )

        messages = [
            SystemMessage(content=_INTENT_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Prior conversation:\n{prior_context}\n\n"
                    f"Current message: {incoming!r}"
                )
            ),
        ]

        response = await model.ainvoke(messages)
        raw = str(response.content).strip().upper()

        # Parse and validate the response
        valid_intents: set[Intent] = {
            "ADD_TASK", "GET_TASK", "COMPLETE", "REJECT",
            "CANNOT_FINISH", "CHECK_IN", "NEED_HELP", "CHAT",
        }

        # Extract the first word that matches a valid intent
        classified: Intent = "CHAT"
        for word in raw.split():
            word_clean = word.strip(".,;:\"'")
            if word_clean in valid_intents:
                classified = word_clean
                break

        # CHECK_IN must never be inferred from user messages
        if classified == "CHECK_IN":
            # Log peer only — no message content (private data discipline)
            log.warning(
                "classify_intent.check_in_from_user_message",
                peer=state.get("peer"),
            )
            classified = "CHAT"

        log.info(
            "classify_intent.classified",
            peer=state.get("peer"),
            intent=classified,
        )
        return {"intent": classified}

    except Exception:
        log.exception("classify_intent.error", peer=state.get("peer"))
        # On any error, default to CHAT — never let classification failure break the graph
        return {"intent": "CHAT"}


def route_intent(state: State) -> str:
    """Return the next node name based on the classified intent.

    Maps each intent to its handler node. Unknown intents route to chat.
    """
    intent = state.get("intent")

    routing: dict[Intent | None, str] = {
        "ADD_TASK": "intake",
        "GET_TASK": "selection",
        "COMPLETE": "complete",
        "REJECT": "rejection",
        "CANNOT_FINISH": "cannot_finish",
        "CHECK_IN": "check_in",
        "NEED_HELP": "need_help",
        "CHAT": "chat",
        None: "chat",
    }

    return routing.get(intent, "chat")


def build_routing_map() -> dict[Hashable, str]:
    """Return the routing map for conditional edges (all nodes must be declared)."""
    return {
        "intake": "intake",
        "selection": "selection",
        "complete": "complete",
        "rejection": "rejection",
        "cannot_finish": "cannot_finish",
        "check_in": "check_in",
        "need_help": "need_help",
        "chat": "chat",
    }


def check_in_route(state: State) -> dict[str, Any]:
    """System-only CHECK_IN injection.

    Called by APScheduler check_in_dispatcher job to inject a CHECK_IN turn.
    Sets intent=CHECK_IN in state so the graph routes to the check_in node.
    Not invokable from user messages (classify_intent guards against this).
    """
    return {"intent": "CHECK_IN"}
