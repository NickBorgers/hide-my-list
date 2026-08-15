"""In-memory Notion database double.

`tests/evals/runner.py` has long had a Notion stub, but it is *static*: reads
return a frozen pool and writes are accepted and discarded. That is correct for
evals, which score one node's output against one fixed world state. It cannot
support a conversation chain, where the whole question is whether turn N's write
is visible to turn N+M's read — the seam bug #641 lived on.

`FakeNotion` is the mutable generalization. The eval runner's exact semantics are
still available via `discard_writes=True`, and `as_notion_page` moved here so
both layers translate the flat shorthand to Notion's nested property shape
through one function. Property names track `docs/notion-schema.md`.

Strictness is the point. `update_status` on a page id the store has never issued
raises `UnknownPageError` rather than silently succeeding — "completed a page
that does not exist" is a real failure mode, and a permissive double turns it
into a green test.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Notion property names by value shape. Mirrors docs/notion-schema.md; the flat
# shorthand key is derived from the property name (see _flat_key_for).
_SELECT_PROPS = ("Work Type", "Energy Required", "Status", "Reminder Status")
_NUMBER_PROPS = ("Time Estimate (min)", "Rejection Count", "Urgency")
_CHECKBOX_PROPS = ("Is Reminder",)
# (notion_prop_name, flat_key) — emitted as {"date": {"start": value}} when present.
_DATE_PROPS = (
    ("Due At", "due_at_iso"),
    ("Remind At", "remind_at"),
    ("Reminder Scheduled At", "reminder_scheduled_at"),
)

# Stable namespace so generated page ids are reproducible across runs. A failing
# chain then produces the same ids every time, which makes logs diffable.
_PAGE_ID_NAMESPACE = uuid.UUID("6d1a9c1e-0000-4000-8000-000000000641")

# The app.tools.notion attribute names FakeNotion replaces. Kept as an explicit
# tuple so a new verb added to the client shows up as a missing attribute in
# tests/unit/test_conversation_fakes.py rather than silently going unfaked.
FAKED_VERBS: tuple[str, ...] = (
    "create_task",
    "create_reminder",
    "query_pending",
    "query_all",
    "query_due_reminders",
    "update_status",
    "complete_reminder",
    "update_property",
    "get_page",
    "query_tasks_with_unscheduled_deadlines",
    "query_scheduled_tasks_with_deadlines",
    "mark_reminder_scheduled",
)


class UnknownPageError(KeyError):
    """Raised when a write targets a page id the store never issued."""


@dataclass(frozen=True)
class NotionWrite:
    """One recorded mutation, in call order."""

    op: str
    page_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    seq: int = 0


def _select_flat_key(prop: str) -> str:
    return prop.lower().replace(" ", "_").replace("(", "").replace(")", "")


def _number_flat_key(prop: str) -> str:
    return prop.split(" (")[0].lower().replace(" ", "_")


def as_notion_page(task: Mapping[str, Any]) -> dict[str, Any]:
    """Build a Notion page payload from a flat shorthand task dict.

    Callers declare tasks as flat `{id, title, work_type, time_estimate}`
    mappings. Nodes read the nested Notion property shape, so translate here
    rather than making every fixture author hand-write Notion JSON.
    Property names must track `docs/notion-schema.md`.
    """
    props: dict[str, Any] = {
        "Title": {"title": [{"plain_text": task.get("title", "")}]},
    }
    for prop in _SELECT_PROPS:
        key = _select_flat_key(prop)
        if key in task:
            props[prop] = {"select": {"name": task[key]}}
    for prop in _NUMBER_PROPS:
        key = _number_flat_key(prop)
        if key in task:
            props[prop] = {"number": task[key]}
    for prop in _CHECKBOX_PROPS:
        key = _select_flat_key(prop)
        if key in task:
            props[prop] = {"checkbox": bool(task[key])}
    for notion_prop, flat_key in _DATE_PROPS:
        if flat_key in task and task[flat_key] is not None:
            props[notion_prop] = {"date": {"start": task[flat_key]}}
    return {"id": task.get("id", ""), "properties": props}


class FakeNotion:
    """Mutable in-memory stand-in for `app.tools.notion`.

    Pages are held as flat shorthand dicts and rendered to Notion's nested shape
    on read. Every mutation is appended to `writes` in call order, which is what
    the conversation invariants assert against.
    """

    def __init__(
        self,
        tasks: Iterable[Mapping[str, Any]] = (),
        *,
        discard_writes: bool = False,
        filter_reads: bool = True,
    ) -> None:
        """
        Args:
            discard_writes: Accept every write but neither apply nor record it,
                and return `{}` rather than a page id. That covers creates as
                well as updates: a created page that became visible to a later
                read would let one eval fixture's node change what a subsequent
                read returns, which is the same "the fixture defines the world"
                contract the reads honour.
            filter_reads: Apply each verb's real filter and urgency sort. Set
                False for eval semantics, where every read returns the fixture's
                declared tasks verbatim in declaration order — the fixture, not
                the run, defines what the node sees.
        """
        self.pages: dict[str, dict[str, Any]] = {}
        self.writes: list[NotionWrite] = []
        self.discard_writes = discard_writes
        self.filter_reads = filter_reads
        self._counter = 0
        for task in tasks:
            self.add_task(**dict(task))

    # -- store management --------------------------------------------------

    def _next_page_id(self) -> str:
        self._counter += 1
        return str(uuid.uuid5(_PAGE_ID_NAMESPACE, f"page-{self._counter}"))

    def add_task(self, **flat: Any) -> str:
        """Insert a page from flat shorthand, verbatim. Returns its page id.

        Deliberately applies no defaults: `as_notion_page` emits a property only
        for a key that is present, so an absent key means the node sees an absent
        property — exactly what an eval fixture declaring `{id, title}` expects.
        Use `seed_task` when you want a realistically-populated page.
        """
        page_id = str(flat.pop("id", "") or self._next_page_id())
        page: dict[str, Any] = {"id": page_id, **flat}
        self.pages[page_id] = page
        return page_id

    def seed_task(self, **flat: Any) -> str:
        """Insert a fully-populated page. Returns its page id.

        Defaults mirror `app.tools.notion.create_task`, so a page seeded here and
        a page created through the faked verb are indistinguishable to a reader.
        """
        defaults: dict[str, Any] = {
            "title": "",
            "status": "Pending",
            "work_type": "Independent",
            "energy_required": "Medium",
            "urgency": 50,
            "time_estimate": 30,
            "rejection_count": 0,
            "is_reminder": False,
        }
        return self.add_task(**{**defaults, **flat})

    def _record(self, op: str, page_id: str, payload: Mapping[str, Any]) -> None:
        self.writes.append(
            NotionWrite(op=op, page_id=page_id, payload=dict(payload), seq=len(self.writes))
        )

    def _require_page(self, page_id: str, op: str) -> dict[str, Any]:
        page = self.pages.get(page_id)
        if page is None:
            raise UnknownPageError(
                f"{op} targeted page {page_id!r}, which this store never issued. "
                f"Known pages: {sorted(self.pages)}"
            )
        return page

    # -- inspection --------------------------------------------------------

    def mark(self) -> int:
        """Return a cursor into `writes` for later `written_pages(since=...)`."""
        return len(self.writes)

    def status_of(self, page_id: str) -> str:
        return str(self._require_page(page_id, "status_of").get("status", ""))

    def title_of(self, page_id: str) -> str:
        return str(self._require_page(page_id, "title_of").get("title", ""))

    def written_pages(self, op: str | None = None, since: int = 0) -> set[str]:
        """Page ids mutated at or after `since`, optionally filtered by op."""
        return {
            write.page_id
            for write in self.writes[since:]
            if op is None or write.op == op
        }

    # -- faked verbs -------------------------------------------------------

    async def create_task(
        self,
        title: str,
        work_type: str,
        urgency: int = 50,
        time_estimate: int = 30,
        energy_required: str = "Medium",
        inline_steps: str = "",
        status: str = "Pending",
        parent_id: str = "",
        sequence: int | None = None,
        due_at_iso: str | None = None,
    ) -> dict[str, Any]:
        if self.discard_writes:
            return {}
        page_id = self.seed_task(
            title=title,
            work_type=work_type,
            urgency=urgency,
            time_estimate=time_estimate,
            energy_required=energy_required,
            inline_steps=inline_steps,
            status=status,
            parent_id=parent_id,
            sequence=sequence,
            due_at_iso=due_at_iso,
        )
        self._record("create_task", page_id, {"title": title, "status": status})
        return {"id": page_id}

    async def create_reminder(
        self,
        title: str,
        remind_at_iso: str,
        work_type: str = "Independent",
        energy_required: str = "Low",
    ) -> dict[str, Any]:
        if self.discard_writes:
            return {}
        page_id = self.seed_task(
            title=title,
            work_type=work_type,
            energy_required=energy_required,
            urgency=90,
            time_estimate=5,
            is_reminder=True,
            remind_at=remind_at_iso,
            reminder_status="pending",
        )
        self._record("create_reminder", page_id, {"title": title, "remind_at": remind_at_iso})
        return {"id": page_id}

    def _query(self, predicate: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:
        if not self.filter_reads:
            pages = list(self.pages.values())
        else:
            pages = sorted(
                (page for page in self.pages.values() if predicate(page)),
                key=lambda page: page.get("urgency", 0),
                reverse=True,
            )
        return {"results": [as_notion_page(page) for page in pages]}

    def _query_sorted(
        self,
        predicate: Callable[[Mapping[str, Any]], bool],
        sort_key: str,
    ) -> dict[str, Any]:
        """Like _query but sorts by a flat string key ascending (for date-sorted verbs)."""
        if not self.filter_reads:
            pages = list(self.pages.values())
        else:
            pages = sorted(
                (page for page in self.pages.values() if predicate(page)),
                key=lambda page: page.get(sort_key) or "",
            )
        return {"results": [as_notion_page(page) for page in pages]}

    async def query_pending(self) -> dict[str, Any]:
        return self._query(
            lambda page: page.get("status") == "Pending" and not page.get("is_reminder")
        )

    async def query_all(self) -> dict[str, Any]:
        return self._query(lambda _page: True)

    async def query_due_reminders(self, before_iso: str | None = None) -> dict[str, Any]:
        def _predicate(page: Mapping[str, Any]) -> bool:
            if not page.get("is_reminder"):
                return False
            if page.get("status") != "Pending":
                return False
            if page.get("reminder_status") != "pending":
                return False
            if before_iso is not None:
                remind_at = page.get("remind_at")
                if not remind_at or remind_at > before_iso:
                    return False
            return True

        return self._query_sorted(_predicate, "remind_at")

    async def query_tasks_with_unscheduled_deadlines(self) -> dict[str, Any]:
        return self._query_sorted(
            lambda page: bool(page.get("due_at_iso"))
            and not page.get("reminder_scheduled_at")
            and page.get("status") != "Completed"
            and not page.get("is_reminder"),
            "due_at_iso",
        )

    async def query_scheduled_tasks_with_deadlines(self) -> dict[str, Any]:
        return self._query_sorted(
            lambda page: bool(page.get("due_at_iso"))
            and bool(page.get("reminder_scheduled_at"))
            and page.get("status") != "Completed"
            and not page.get("is_reminder"),
            "due_at_iso",
        )

    async def get_page(self, page_id: str) -> dict[str, Any]:
        return as_notion_page(self._require_page(page_id, "get_page"))

    async def update_status(self, page_id: str, new_status: str) -> dict[str, Any]:
        if self.discard_writes:
            return {}
        page = self._require_page(page_id, "update_status")
        page["status"] = new_status
        self._record("update_status", page_id, {"status": new_status})
        return {"id": page_id}

    async def complete_reminder(self, page_id: str, reminder_status: str) -> dict[str, Any]:
        if reminder_status not in ("sent", "missed"):
            raise ValueError(
                f"reminder_status must be 'sent' or 'missed', got {reminder_status!r}"
            )
        if self.discard_writes:
            return {}
        page = self._require_page(page_id, "complete_reminder")
        page["status"] = "Completed"
        page["reminder_status"] = reminder_status
        self._record("complete_reminder", page_id, {"reminder_status": reminder_status})
        return {"id": page_id}

    async def update_property(self, page_id: str, prop_json: dict[str, Any]) -> dict[str, Any]:
        if self.discard_writes:
            return {}
        page = self._require_page(page_id, "update_property")
        props = prop_json.get("properties", prop_json)
        for prop, value in props.items():
            if prop in _SELECT_PROPS and isinstance(value, dict):
                page[_select_flat_key(prop)] = value.get("select", {}).get("name")
            elif prop in _NUMBER_PROPS and isinstance(value, dict):
                page[_number_flat_key(prop)] = value.get("number")
            elif prop in _CHECKBOX_PROPS and isinstance(value, dict):
                page[_select_flat_key(prop)] = value.get("checkbox")
        for prop_name, flat_key in _DATE_PROPS:
            if prop_name in props and isinstance(props[prop_name], dict):
                page[flat_key] = props[prop_name].get("date", {}).get("start")
        self._record("update_property", page_id, prop_json)
        return {"id": page_id}

    async def mark_reminder_scheduled(self, page_id: str) -> dict[str, Any]:
        if self.discard_writes:
            return {}
        page = self._require_page(page_id, "mark_reminder_scheduled")
        # Store an ISO timestamp so as_notion_page() can emit a real date value,
        # matching the real client which PATCHes "Reminder Scheduled At" with a date.
        page["reminder_scheduled_at"] = "2000-01-01T00:00:00+00:00"
        self._record("mark_reminder_scheduled", page_id, {})
        return {"id": page_id}

    # -- installation ------------------------------------------------------

    def install(self) -> Callable[[], None]:
        """Patch the faked verbs onto `app.tools.notion`. Returns an undo callable.

        Also replaces `_client_factory` with a raiser. Every unfaked verb
        (`health_check`, `verify_schema`, anything added later) resolves its HTTP
        client through that factory, so a call this double does not cover fails
        loudly instead of attempting real egress or hanging on a connect timeout.
        """
        from app.tools import notion

        def _no_egress() -> Any:
            raise AssertionError(
                "Notion HTTP egress attempted under FakeNotion. A verb is unfaked — "
                f"add it to FAKED_VERBS. Currently faked: {', '.join(FAKED_VERBS)}"
            )

        original: dict[str, Any] = {
            name: getattr(notion, name) for name in (*FAKED_VERBS, "_client_factory")
        }
        for name in FAKED_VERBS:
            setattr(notion, name, getattr(self, name))
        notion._client_factory = _no_egress

        def _undo() -> None:
            for name, fn in original.items():
                setattr(notion, name, fn)

        return _undo
