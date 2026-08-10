"""Send (terminal) node: drains pending_outbound via Signal.

Consumes all OutboundDraft items from pending_outbound, sends each via
app/tools/signal_client.send_message(). Idempotency keys are generated from
the recipient + body hash to allow signal-cli deduplication where supported.

Failure handling: log and re-queue (do not block graph completion).
Individual send failures do not raise — the graph always completes successfully.
The reminder outbox handles guaranteed delivery for reminders separately.
This send node is for conversation replies only.
"""
from __future__ import annotations

import hashlib
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.graph.nodes._task_token import render_task_token
from app.graph.state import State

log = structlog.get_logger(__name__)

async def send_node(state: State) -> dict[str, Any]:
    """Terminal node: drain pending_outbound and send via Signal.

    Ordering: drafts are sent in list order (preserved from upstream nodes).
    Failures: logged, not raised. Graph completion is not blocked by send failures.

    Returns a messages delta — HumanMessage for the incoming turn and AIMessage per
    non-empty outbound draft — for the add_messages reducer, or an empty dict if
    nothing to record. Send failures are logged and non-blocking.
    """
    pending = state.get("pending_outbound", [])

    # Build the conversation-history delta for this turn so future turns retain
    # context. The State `messages` channel uses the `add_messages` reducer,
    # so returning a list here appends; bounding lives in the consumers (each
    # node windows to the last few messages when prompting the LLM).
    incoming = state.get("incoming", "")
    new_messages: list[BaseMessage] = []
    if incoming:
        new_messages.append(HumanMessage(content=incoming))

    if not pending:
        return {"messages": new_messages} if new_messages else {}

    from app.tools import signal_client

    for draft in pending:
        recipient = draft.get("recipient", "")
        body = draft.get("body", "")
        notion_page_id = draft.get("notion_page_id")
        attachment_path: str | None = draft.get("attachment_path")

        if not recipient or not body:
            # Log booleans only — no body content (private data discipline)
            log.warning(
                "send_node.skip_empty_draft",
                has_recipient=bool(recipient),
                has_body=bool(body),
            )
            continue

        # Enforce the naming invariant before hashing so the idempotency key
        # matches what actually goes out. A draft that declares a task title is
        # asserting the body names that task; if the model drifted and left it
        # out, name it here rather than sending an unactionable suggestion.
        rendered = render_task_token(body, title=draft.get("notion_page_title"))
        if rendered != body:
            log.warning(
                "send_node.task_title_injected",
                notion_page_id=notion_page_id,
                # body and title are private — booleans only
                had_task_token="{task}" in body,
            )
            body = rendered

        # Generate idempotency key from content hash for deduplication
        key_source = f"{recipient}:{body}"
        idempotency_key = hashlib.sha256(key_source.encode()).hexdigest()[:32]

        # Build attachment list for signal_client — attachment_path is private;
        # log attachment_count only, never the path.
        send_kwargs: dict[str, Any] = {
            "idempotency_key": idempotency_key,
        }
        if attachment_path:
            send_kwargs["attachment_paths"] = [attachment_path]

        try:
            result = await signal_client.send_message(
                recipient=recipient,
                message=body,
                **send_kwargs,
            )
            log.info(
                "send_node.sent",
                has_recipient=bool(recipient),
                notion_page_id=notion_page_id,
                timestamp=result.get("timestamp"),
                attachment_count=1 if attachment_path else 0,
            )
            new_messages.append(AIMessage(content=body))
        except Exception:
            log.exception(
                "send_node.send_failed",
                has_recipient=bool(recipient),
                notion_page_id=notion_page_id,
                attachment_count=1 if attachment_path else 0,
                # attachment_path intentionally omitted — private data
            )
            # Do not re-raise — terminal node must not block graph completion.
            # For reminders, the outbox handles delivery. For conversation replies,
            # logging is the signal for operator visibility.

    return {"messages": new_messages} if new_messages else {}
