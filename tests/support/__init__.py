"""Shared test doubles for the hide-my-list test rig.

Every layer above `tests/unit/` needs the same three fakes: an in-memory Notion
database, a Signal capture sink, and the shame-safety regex catalog. Before this
package each test file rolled its own, which is how bug class 3 (permissive mock
hides a silent failure) kept recurring — a locally-defined `AsyncMock` asserts
only what its author remembered to assert.

The doubles here are deliberately strict: `FakeNotion` raises on writes to pages
it has never seen, and `SignalSink` pins its own signature against the real
`app.tools.signal_client.send_message`. A fake that drifts from the function it
replaces is worse than no test at all.
"""
from __future__ import annotations

from tests.support.notion_fake import FakeNotion, NotionWrite, UnknownPageError, as_notion_page
from tests.support.shame import BANNED_PATTERNS, find_banned_phrases
from tests.support.signal_sink import SentMessage, SignalSink

__all__ = [
    "BANNED_PATTERNS",
    "FakeNotion",
    "NotionWrite",
    "SentMessage",
    "SignalSink",
    "UnknownPageError",
    "as_notion_page",
    "find_banned_phrases",
]
