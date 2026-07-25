# Check-In Handling

Assumes you've already read `docs/ai-prompts/shared.md` for the base prompt, shame-prevention templates, user preferences context, and output handling.

## Module 6: Check-In Handling

Check-in module invoked by APScheduler's `check_in_dispatcher` job, which polls every 10 minutes and triggers the graph with CHECK_IN intent when a check-in is due. Timing tracked in LangGraph checkpoint state and Notion.

```mermaid
sequenceDiagram
    participant Sched as APScheduler<br/>(check_in_dispatcher)
    participant Agent as AI Assistant
    participant State as Checkpoint State
    participant Notion as Notion API

    Sched->>Agent: Trigger CHECK_IN graph invocation (when due)
    Agent->>State: Load active_task, check_in_due_at, check_in_count
    Agent->>Notion: Fetch active task (status, time estimate) if due
    Agent->>Agent: Determine if check-in should fire
    alt No active task or not due
        Agent->>State: Clear stale check-in fields if needed
        Agent-->>Sched: Log CHECK_IN_SKIPPED
    else Due for check-in
        Agent->>Agent: Generate check-in message (this module)
        Agent->>User: Send check-in
        Agent->>State: Increment check_in_count, set next due time
    end
```

### Runtime Data

When the user accepts a task, the selection module marks the Notion task
`In Progress` and writes `active_task` to checkpoint state:
- `page_id`, `title`, `status`
- `selected_at` (ISO 8601, UTC)
- `work_type`, `urgency`, `time_estimate`, `energy_required`, `rejection_count`

`conversation_state` is set to `"active"`.

**Check-in invocation:** check-ins are system-initiated only. The scheduler's
`check_in_dispatcher` job fires every 10 minutes; system-initiated turns enter
the graph through `check_in_route`, which sets `intent = CHECK_IN` so the
graph routes to this module. If `active_task` is empty, the module skips the
check-in. Otherwise it generates the message from `active_task` (`title`,
`time_estimate`, `check_in_count`, and `started_at` when present — elapsed
time falls back to the estimate without it), increments `check_in_count`, and
sets `conversation_state = "checking_in"`.

### Check-In Prompt

```
The user accepted a task but the expected completion time has passed.
Check in on their progress.

ACTIVE TASK: {task_title}
TIME ESTIMATE: {time_estimate} minutes
TIME ELAPSED: {elapsed_minutes} minutes
TASK CONTEXT: {ai_context}

Generate a brief, friendly check-in message. Keep it casual and non-judgmental.
The user may have:
- Completed the task and forgot to say so
- Still be working on it
- Gotten distracted
- Needed more time than estimated

OUTPUT (JSON):
{
  "check_in_message": "..." (brief, friendly follow-up question)
}
```

### Check-In Response Prompt

```
The user responded to a check-in about their active task.

TASK: {task_title}
USER RESPONSE: "{user_response}"

Classify the response and determine next action.

RESPONSE CATEGORIES:
- done: User completed the task
- still_working: User is still actively working
- distracted: User got sidetracked
- need_more_time: User needs additional time
- abandoned: User wants to stop working on this task

OUTPUT (JSON):
{
  "response_category": "...",
  "task_update": {
    "status": "in_progress" | "completed" | "pending",
    "note": "..." (optional note to append)
  },
  "schedule_follow_up": true | false,
  "follow_up_minutes": null | number,
  "user_message": "..." (response to user)
}
```

### Check-In Message Templates (Shame-Safe)

> **Shame Prevention:** Check-ins = potential shame trigger — user may feel caught for not finishing on time. Keep check-ins curious and warm, never supervisory. Tone: "friend checking in," not "manager following up."

| Scenario | Example Message |
|----------|-----------------|
| First check-in | "How's {task} going? Still at it?" |
| User says done | "Nice! Marking that off. Ready for another?" |
| User still working | "No rush — keep at it! I'll check back in a bit." |
| User got distracted | "Happens to literally everyone. Want to jump back in, or try something else?" |
| User needs more time | "No problem — time estimates are just guesses anyway. About how much longer?" |
| User abandons | "Totally fine — I'll keep it for later. No pressure. What sounds good now?" |

The module writes the literal token `{task}` wherever the message refers to the
active task; the application substitutes the exact stored title. A check-in that
names the task only by attribute ("that focus task") reads as supervisory and
gives the user nothing to orient on.

### Check-In Timing

Store all timings in UTC:

| Event | How to calculate | Where stored |
|-------|------------------|--------------|
| First check-in | `started_at + (time_estimate_minutes × 1.25)` | checkpoint state `active_task.check_in_due_at` |
| Second check-in | `now + (time_estimate_minutes × 0.5)` | overwrite `check_in_due_at` |
| Third check-in | `now + (time_estimate_minutes × 0.25)` | overwrite `check_in_due_at` |

Examples (45 min estimate):
- Start at 14:00 → first check-in at 14:56 UTC
- User still working → second at 15:23 UTC
- Still working again → final at 15:34 UTC

### Repeated Check-Ins

Use `active_task.check_in_count` from checkpoint state to control intensity:

```mermaid
flowchart TD
    Trigger["check_in_due_at reached"] --> Count{check_in_count}
    Count -->|0| First["Friendly check-in"]
    Count -->|1| Second["Brief follow-up"]
    Count -->|2| Third["Suggest break, stop checking in"]
    Third --> Stop[Clear check_in_due_at]

    First --> Response
    Second --> Response

    Response --> Done["Done"] --> Complete[Mark completed, clear state]
    Response --> Working["Still working"] --> NextHalf["Set next due = now + estimate×0.5"]
    Response --> NeedTime["Need more time"] --> Custom["Set next due = now + user_time×1.25"]
    Response --> Distracted["Distracted"] --> Decide{Continue?}
    Response --> Abandon["Stop"] --> Return[Status → pending, clear state]

    Decide -->|Yes| NextHalf
    Decide -->|No| Return

    NextHalf --> Increment[Increment check_in_count] --> DoneLoop{{Loop}}
    Custom --> Increment --> DoneLoop
```

If `check_in_count` reaches 3 without completion, clear `check_in_due_at` and deliver "take a break" message. Further follow-ups require user to re-accept task.

### State Updates After Each Check-In

| Scenario | Required updates |
|----------|------------------|
| Task completed | Update Notion status → `completed`; clear `active_task` from checkpoint state; reset `conversation_state = "idle"` |
| Still working | Increment `check_in_count`; set new `check_in_due_at`; keep `conversation_state = "active"` |
| Needs more time | Same as still working but use user-specified duration |
| Distracted but recommits | Increment `check_in_count`; set new due at 0.5× |
| Distracted and stops | Return task to queue (`pending`), clear `active_task`, reset state |
| Wants to abandon | Same as above |

### Exiting Without Action

If the scheduler fires and no check-in due (no active task, already completed, or window not reached), respond `CHECK_IN_SKIPPED` in session log and ensure `conversation_state` returns to `idle`. Prevents false positives, keeps cron idempotent.


---

See also:
- `docs/ai-prompts/shared.md` — shame-prevention base, base prompt
