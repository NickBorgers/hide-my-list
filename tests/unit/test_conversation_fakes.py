"""Contract tests for the shared test doubles in `tests/support/`.

A fake that has drifted from the thing it replaces is worse than no test: it
reports green while the real call site has changed underneath it. That is bug
class 10 (silent degradation masked by a permissive mock), and these tests apply
it to the rig itself.

Three properties are pinned:
1. `SignalSink.send_message` has the same signature as the real client function.
2. `FakeNotion` covers every verb, and its rendered pages round-trip through the
   real node-side property extractors.
3. The eval runner's Notion stub still behaves exactly as it did before it was
   refactored onto `FakeNotion` — reads return the fixture's declared tasks
   verbatim, writes change nothing.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from tests.support import FakeNotion, SignalSink, UnknownPageError, as_notion_page, notion_fake
from tests.support.notion_fake import FAKED_VERBS
from tests.support.shame import find_banned_phrases

# ---------------------------------------------------------------------------
# SignalSink
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fake_attr", "real_attr"),
    [
        ("send_message", "send_message"),
        ("send_read_receipt", "send_read_receipt"),
        ("send_typing_indicator", "send_typing_indicator"),
    ],
)
def test_signal_sink_signatures_match_real_client(fake_attr: str, real_attr: str) -> None:
    from app.tools import signal_client

    real = inspect.signature(getattr(signal_client, real_attr))
    fake = inspect.signature(getattr(SignalSink(), fake_attr))

    assert fake.parameters == real.parameters, (
        f"SignalSink.{fake_attr} has drifted from signal_client.{real_attr}. "
        "A caller could pass an argument the real function does not accept and "
        "every test using the sink would still pass."
    )


async def test_signal_sink_timestamps_are_monotonic() -> None:
    """`reminder_worker` writes this value into `recent_outbound.signal_timestamp`."""
    sink = SignalSink()
    first = await sink.send_message("+15550000001", "one")
    second = await sink.send_message("+15550000001", "two")

    assert second["timestamp"] > first["timestamp"]
    assert [m.body for m in sink.sent] == ["one", "two"]
    assert sink.since(sink.mark()) == []


async def test_signal_sink_captures_attachments_and_key() -> None:
    sink = SignalSink()
    await sink.send_message(
        "+15550000001",
        "nice work",
        attachment_paths=["/data/reward_artifacts/a.png"],
        idempotency_key="abc123",
    )

    sent = sink.sent[0]
    assert sent.attachment_paths == ("/data/reward_artifacts/a.png",)
    assert sent.idempotency_key == "abc123"


def test_signal_sink_install_patches_listener_namespace() -> None:
    """The listener imports the receipt helpers by value; its namespace is the target."""
    from app.ingress import signal_listener
    from app.tools import signal_client

    sink = SignalSink()
    undo = sink.install()
    try:
        assert signal_client.send_message == sink.send_message
        assert signal_listener.send_read_receipt == sink.send_read_receipt
        assert signal_listener.send_typing_indicator == sink.send_typing_indicator
    finally:
        undo()

    assert signal_client.send_message != sink.send_message
    assert signal_listener.send_read_receipt != sink.send_read_receipt


# ---------------------------------------------------------------------------
# FakeNotion
# ---------------------------------------------------------------------------


def test_faked_verbs_all_exist_on_the_real_client() -> None:
    """A verb renamed in app/tools/notion.py must not silently go unfaked."""
    from app.tools import notion

    missing = [name for name in FAKED_VERBS if not hasattr(notion, name)]
    assert not missing, f"FAKED_VERBS names verbs that no longer exist: {missing}"


def test_fake_notion_covers_every_async_verb() -> None:
    """Every public async verb is either faked or a health/schema probe.

    The two exemptions are the operational probes, which chains never call and
    which the `_client_factory` raiser covers.
    """
    from app.tools import notion

    public_verbs = {
        name
        for name, fn in vars(notion).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(fn)
    }
    unfaked = public_verbs - set(FAKED_VERBS) - {"health_check", "verify_schema"}
    assert not unfaked, f"Notion verbs added without a FakeNotion counterpart: {sorted(unfaked)}"


def test_fake_notion_pages_round_trip_through_node_extractors() -> None:
    """Rendered pages must be readable by the real selection-node extractors."""
    from app.graph.nodes.selection import _extract_number, _extract_select, _extract_title

    fake = FakeNotion()
    page_id = fake.seed_task(
        title="Fold the laundry",
        work_type="Independent",
        energy_required="Low",
        urgency=70,
        time_estimate=15,
    )
    page = as_notion_page(fake.pages[page_id])
    props = page["properties"]

    assert _extract_title(props) == "Fold the laundry"
    assert _extract_select(props, "Work Type") == "Independent"
    assert _extract_select(props, "Energy Required") == "Low"
    assert _extract_number(props, "Urgency") == 70
    assert _extract_number(props, "Time Estimate (min)") == 15


async def test_fake_notion_write_is_visible_to_a_later_read() -> None:
    """The whole point of the mutable store: turn N's write, turn N+M's read."""
    fake = FakeNotion()
    page_id = fake.seed_task(title="Call the dentist")

    assert len((await fake.query_pending())["results"]) == 1

    await fake.update_status(page_id, "Completed")

    assert (await fake.query_pending())["results"] == []
    assert fake.status_of(page_id) == "Completed"


async def test_fake_notion_records_writes_in_order() -> None:
    fake = FakeNotion()
    page_id = fake.seed_task(title="Book the appointment")

    cursor = fake.mark()
    await fake.update_status(page_id, "In Progress")
    await fake.update_status(page_id, "Completed")

    assert [w.op for w in fake.writes[cursor:]] == ["update_status", "update_status"]
    assert fake.written_pages(op="update_status", since=cursor) == {page_id}
    assert fake.written_pages(op="create_task", since=cursor) == set()


async def test_fake_notion_rejects_write_to_unknown_page() -> None:
    """Completing a page the store never issued is always a bug, never a no-op."""
    fake = FakeNotion()

    with pytest.raises(UnknownPageError):
        await fake.update_status("11111111-2222-4333-8444-555555555555", "Completed")


async def test_fake_notion_query_pending_excludes_reminders() -> None:
    fake = FakeNotion()
    fake.seed_task(title="Sort the mail")
    await fake.create_reminder(title="Water the plants", remind_at_iso="2026-08-07T17:00:00+00:00")

    titles = [
        page["properties"]["Title"]["title"][0]["plain_text"]
        for page in (await fake.query_pending())["results"]
    ]
    assert titles == ["Sort the mail"]


async def test_fake_notion_query_pending_sorts_by_urgency_descending() -> None:
    fake = FakeNotion()
    fake.seed_task(title="Low", urgency=10)
    fake.seed_task(title="High", urgency=90)

    titles = [
        page["properties"]["Title"]["title"][0]["plain_text"]
        for page in (await fake.query_pending())["results"]
    ]
    assert titles == ["High", "Low"]


async def test_fake_notion_query_due_reminders_filters_before_iso() -> None:
    """query_due_reminders respects the before_iso cutoff."""
    fake = FakeNotion()
    await fake.create_reminder(title="Past", remind_at_iso="2026-08-01T10:00:00+00:00")
    await fake.create_reminder(title="Future", remind_at_iso="2026-08-20T10:00:00+00:00")

    results = (await fake.query_due_reminders(before_iso="2026-08-15T00:00:00+00:00"))["results"]
    titles = [r["properties"]["Title"]["title"][0]["plain_text"] for r in results]
    assert titles == ["Past"]


async def test_fake_notion_query_due_reminders_excludes_completed() -> None:
    """query_due_reminders excludes reminders that have already been completed."""
    fake = FakeNotion()
    created = await fake.create_reminder(title="Done", remind_at_iso="2026-08-01T10:00:00+00:00")
    await fake.complete_reminder(created["id"], "sent")
    await fake.create_reminder(title="Pending", remind_at_iso="2026-08-01T11:00:00+00:00")

    results = (await fake.query_due_reminders(before_iso="2026-08-15T00:00:00+00:00"))["results"]
    titles = [r["properties"]["Title"]["title"][0]["plain_text"] for r in results]
    assert titles == ["Pending"]


async def test_fake_notion_query_due_reminders_sorts_by_remind_at_ascending() -> None:
    """query_due_reminders returns rows sorted by remind_at ascending (soonest first)."""
    fake = FakeNotion()
    await fake.create_reminder(title="Later", remind_at_iso="2026-08-10T12:00:00+00:00")
    await fake.create_reminder(title="Sooner", remind_at_iso="2026-08-10T08:00:00+00:00")

    results = (await fake.query_due_reminders(before_iso="2026-08-15T00:00:00+00:00"))["results"]
    titles = [r["properties"]["Title"]["title"][0]["plain_text"] for r in results]
    assert titles == ["Sooner", "Later"]


async def test_fake_notion_query_due_reminders_remind_at_property_parseable() -> None:
    """Rendered Remind At property matches the date shape the real client writes."""
    fake = FakeNotion()
    remind_ts = "2026-08-10T09:00:00+00:00"
    await fake.create_reminder(title="Pick up prescription", remind_at_iso=remind_ts)

    results = (await fake.query_due_reminders(before_iso="2026-08-15T00:00:00+00:00"))["results"]
    assert len(results) == 1
    remind_at_prop = results[0]["properties"].get("Remind At")
    assert remind_at_prop is not None, "Remind At property missing from rendered reminder page"
    assert remind_at_prop["date"]["start"] == remind_ts


async def test_fake_notion_query_unscheduled_deadlines_excludes_completed() -> None:
    """Completed tasks do not appear in query_tasks_with_unscheduled_deadlines."""
    fake = FakeNotion()
    pending_id = fake.seed_task(title="Pending", due_at_iso="2026-09-01T00:00:00+00:00")
    completed_id = fake.seed_task(
        title="Done", due_at_iso="2026-09-02T00:00:00+00:00", status="Completed"
    )
    _ = completed_id  # referenced to show intent

    results = (await fake.query_tasks_with_unscheduled_deadlines())["results"]
    ids = [r["id"] for r in results]
    assert pending_id in ids
    assert completed_id not in ids


async def test_fake_notion_query_unscheduled_deadlines_excludes_reminders() -> None:
    """Reminder pages (is_reminder=True) do not appear in unscheduled deadline results."""
    fake = FakeNotion()
    task_id = fake.seed_task(title="Real task", due_at_iso="2026-09-01T00:00:00+00:00")
    reminder_id = fake.seed_task(
        title="Reminder", due_at_iso="2026-09-01T00:00:00+00:00", is_reminder=True
    )
    _ = reminder_id

    results = (await fake.query_tasks_with_unscheduled_deadlines())["results"]
    ids = [r["id"] for r in results]
    assert task_id in ids
    assert reminder_id not in ids


async def test_fake_notion_query_unscheduled_deadlines_sorts_by_due_at_ascending() -> None:
    """query_tasks_with_unscheduled_deadlines returns rows sorted by due_at_iso ascending."""
    fake = FakeNotion()
    fake.seed_task(title="Later", due_at_iso="2026-09-10T00:00:00+00:00")
    fake.seed_task(title="Sooner", due_at_iso="2026-09-01T00:00:00+00:00")

    results = (await fake.query_tasks_with_unscheduled_deadlines())["results"]
    titles = [r["properties"]["Title"]["title"][0]["plain_text"] for r in results]
    assert titles == ["Sooner", "Later"]


async def test_fake_notion_query_scheduled_deadlines_split_from_unscheduled() -> None:
    """mark_reminder_scheduled moves a task from unscheduled to scheduled results."""
    fake = FakeNotion()
    page_id = fake.seed_task(title="File taxes", due_at_iso="2026-09-15T00:00:00+00:00")

    assert len((await fake.query_tasks_with_unscheduled_deadlines())["results"]) == 1
    assert len((await fake.query_scheduled_tasks_with_deadlines())["results"]) == 0

    await fake.mark_reminder_scheduled(page_id)

    assert len((await fake.query_tasks_with_unscheduled_deadlines())["results"]) == 0
    assert len((await fake.query_scheduled_tasks_with_deadlines())["results"]) == 1


async def test_fake_notion_due_at_parseable_by_reminder_scheduler() -> None:
    """Due At rendered by as_notion_page() is parseable by the real scheduler's _parse_page."""
    from app.scheduler.reminder_scheduler import _parse_page

    fake = FakeNotion()
    due_ts = "2026-09-01T12:00:00+00:00"
    page_id = fake.seed_task(title="Submit report", urgency=80, due_at_iso=due_ts)
    page = as_notion_page(fake.pages[page_id])

    result = _parse_page(page)
    assert result is not None, "reminder_scheduler._parse_page returned None for a page with Due At"
    returned_id, deadline, urgency = result
    assert returned_id == page_id
    assert deadline.isoformat().startswith("2026-09-01")
    assert urgency == 80


async def test_fake_notion_create_task_returns_a_usable_page_id() -> None:
    """`intake_node` reads `notion_page["id"]` and later writes against it."""
    fake = FakeNotion()
    created = await fake.create_task(title="Renew the passport", work_type="Independent")

    assert created["id"]
    await fake.update_status(created["id"], "Completed")
    assert fake.status_of(created["id"]) == "Completed"


async def test_fake_notion_complete_reminder_validates_status() -> None:
    fake = FakeNotion()
    created = await fake.create_reminder(
        title="Take the bins out", remind_at_iso="2026-08-07T17:00:00+00:00"
    )

    with pytest.raises(ValueError, match="sent"):
        await fake.complete_reminder(created["id"], "done")

    await fake.complete_reminder(created["id"], "sent")
    assert fake.status_of(created["id"]) == "Completed"


def test_fake_notion_install_blocks_real_egress() -> None:
    from app.tools import notion

    fake = FakeNotion()
    undo = fake.install()
    try:
        assert notion.update_status == fake.update_status
        with pytest.raises(AssertionError, match="egress"):
            notion._client_factory()
    finally:
        undo()

    assert notion.update_status != fake.update_status


# ---------------------------------------------------------------------------
# Eval-runner compatibility
# ---------------------------------------------------------------------------


async def test_eval_mode_reads_return_declared_tasks_verbatim() -> None:
    """Eval semantics: the fixture defines the world, not the run.

    Reads are unfiltered and in declaration order, and a task declaring only
    `{id, title}` must render with only a Title property — a node that reads
    Work Type has to see it absent, exactly as before the refactor.
    """
    tasks = [
        {"id": "page-b", "title": "Second", "status": "Completed"},
        {"id": "page-a", "title": "First"},
    ]
    fake = FakeNotion(tasks, discard_writes=True, filter_reads=False)

    results = (await fake.query_pending())["results"]
    assert [page["id"] for page in results] == ["page-b", "page-a"]
    assert set(results[1]["properties"]) == {"Title"}


async def test_eval_mode_discards_writes() -> None:
    tasks = [{"id": "page-a", "title": "First", "status": "Pending"}]
    fake = FakeNotion(tasks, discard_writes=True, filter_reads=False)

    await fake.update_status("page-a", "In Progress")

    assert fake.pages["page-a"]["status"] == "Pending"
    assert fake.writes == []


async def test_eval_mode_discards_creates_too() -> None:
    """A created page must not become visible to a later read.

    Creates are writes. If one leaked into the store, a node that creates a task
    would change what a subsequent read in the same fixture returns — the
    fixture would stop being the sole definition of the world, which is the
    contract `filter_reads=False` exists to hold. The previous eval stub mapped
    `create_task` to the same discard function as every other write and returned
    `{}`; this keeps that exact shape.
    """
    fake = FakeNotion([{"id": "page-a", "title": "First"}], discard_writes=True, filter_reads=False)

    created = await fake.create_task(title="Brand new", work_type="Independent")
    reminder = await fake.create_reminder(
        title="Also new", remind_at_iso="2026-08-07T17:00:00+00:00"
    )

    assert created == {}
    assert reminder == {}
    assert set(fake.pages) == {"page-a"}
    assert fake.writes == []
    assert [page["id"] for page in (await fake.query_pending())["results"]] == ["page-a"]


async def test_eval_mode_tolerates_writes_to_unknown_pages() -> None:
    """Eval fixtures let a node create then update a page the fixture never declared."""
    fake = FakeNotion([], discard_writes=True, filter_reads=False)

    assert await fake.update_status("never-declared", "Completed") == {}


def test_runner_still_exposes_its_private_translator_names() -> None:
    """`_as_notion_page` and the prop tuples are part of the runner's surface.

    Asserts the re-export is the *same object*, not a frozen copy of the tuple's
    contents. The point is that the eval runner and the conversation layer share
    one reading of `docs/notion-schema.md`; pinning literals here would instead
    make every legitimate addition to the property list look like a failure.
    """
    from tests.evals import runner

    assert runner._as_notion_page is as_notion_page
    assert runner._SELECT_PROPS is notion_fake._SELECT_PROPS
    assert runner._NUMBER_PROPS is notion_fake._NUMBER_PROPS
    assert runner._CHECKBOX_PROPS is notion_fake._CHECKBOX_PROPS

    # A representative property from each shape, so a tuple emptied by accident
    # still fails.
    assert "Status" in runner._SELECT_PROPS
    assert "Urgency" in runner._NUMBER_PROPS
    assert "Is Reminder" in runner._CHECKBOX_PROPS


# ---------------------------------------------------------------------------
# Shame catalog
# ---------------------------------------------------------------------------


def test_shame_catalog_matches_the_prompt_gate() -> None:
    """The extracted catalog must stay identical to the one the prompt gate uses."""
    from tests.support.shame import BANNED_PATTERNS
    from tests.unit import test_shame_safety

    assert [p.pattern for p in BANNED_PATTERNS] == [
        p.pattern for p in test_shame_safety._BANNED_PATTERNS
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Nice work getting that done!", False),
        ("You forgot to do this one.", True),
        ("you haven't started yet", True),
        ("That task is still waiting whenever you're ready.", False),
    ],
)
def test_find_banned_phrases(text: str, expected: bool) -> None:
    assert bool(find_banned_phrases(text)) is expected


def test_find_banned_phrases_reports_every_match() -> None:
    found = find_banned_phrases("You forgot the thing and you never called back.")
    assert len(found) == 2


def test_notion_write_payload_is_copied_not_aliased() -> None:
    """A caller mutating its own dict afterwards must not rewrite history."""
    fake = FakeNotion()
    payload: dict[str, Any] = {"Status": {"select": {"name": "Pending"}}}
    page_id = fake.seed_task(title="Anything")
    fake._record("update_property", page_id, payload)

    payload["Status"] = {"select": {"name": "Completed"}}

    assert fake.writes[-1].payload["Status"] == {"select": {"name": "Pending"}}
