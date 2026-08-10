"""Entry point for the hide-my-list Python + LangGraph app.

LangSmith guard: refuses to boot when LANGSMITH_TRACING=true unless
ALLOW_PRIVATE_TRACE_EXPORT=true is also set.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import structlog
from structlog.types import EventDict, WrappedLogger

log = structlog.get_logger()

_RECIPIENT_LOG_FIELDS = frozenset({"peer", "recipient", "phone_number", "signal_account"})
_PRIVATE_TEXT_LOG_FIELDS = frozenset(
    {"message", "body", "task_title", "title", "notion_page_title", "reminder_content"}
)


def _redact_private_log_fields(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Redact private field values before logs reach stdout or file sinks."""
    for key in _RECIPIENT_LOG_FIELDS:
        if key in event_dict:
            event_dict[key] = "<recipient>"
    for key in _PRIVATE_TEXT_LOG_FIELDS:
        if key in event_dict:
            event_dict[key] = "<private>"
    return event_dict


def _check_langsmith_guard() -> None:
    """Refuse boot if LangSmith tracing is on without explicit private-export consent."""
    if os.environ.get("LANGSMITH_TRACING", "false").lower() == "true":
        if os.environ.get("ALLOW_PRIVATE_TRACE_EXPORT", "false").lower() != "true":
            print(  # noqa: T201
                "ERROR: LANGSMITH_TRACING=true but ALLOW_PRIVATE_TRACE_EXPORT is not set. "
                "Refusing to start: private conversation data must not be traced without "
                "explicit operator consent. Set ALLOW_PRIVATE_TRACE_EXPORT=true to override.",
                file=sys.stderr,
            )
            sys.exit(1)


async def _run_app() -> None:
    """Start Signal ingress and APScheduler."""
    from app.graph.graph import build_graph, build_postgres_checkpointer
    from app.ingress.signal_listener import SignalListener
    from app.scheduler.jobs import reconcile_jobstore
    from app.scheduler.scheduler import build_scheduler
    from app.tools.db import get_connection_string, run_migrations

    log.info("app.starting", enable_langgraph_path=True)

    run_migrations()
    scheduler = await build_scheduler(skip_reconcile=True)
    scheduler.start(paused=True)
    reconcile_jobstore(scheduler)
    scheduler.resume()
    log.info("scheduler.started")

    database_url = get_connection_string()
    async with build_postgres_checkpointer(database_url) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        listener = SignalListener(graph=graph)
        log.info("ingress.starting")
        await listener.run()


def _configure_logging() -> None:
    log_file = os.environ.get("LOG_FILE", "")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(handlers=handlers, level=logging.INFO, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_private_log_fields,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )


def main() -> None:
    _check_langsmith_guard()
    _configure_logging()

    from app.models import validate_startup

    validate_startup()
    asyncio.run(_run_app())


if __name__ == "__main__":
    main()
