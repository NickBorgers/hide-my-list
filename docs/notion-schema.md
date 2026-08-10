---
layout: default
title: Notion Schema
---

# Notion Database Schema

## Overview

hide-my-list uses Notion as database, Notion API for all CRUD. Zero DB setup, visual backup, rich querying.

## Scope and Ownership

**The task database belongs to one person.** There is no owner, peer, or tenant
column on `TASKS`, and none is planned. Every task in the database is that one
person's task.

`AUTHORIZED_PEERS` is a list of Signal addresses, but the addresses are one
user's devices and numbers, not separate people. Any authorized address may
read, select, reject, or complete any task in the database. Code that queries
the whole open-task list and writes to whatever it finds is operating within
scope — the whole list is the whole of one person's list.

Why this matters to reviewers: several code paths look like missing
authorization checks against a multi-tenant schema. `notion.query_all()` with
no peer filter is the normal case, not a gap. The security boundary in this
system is the `AUTHORIZED_PEERS` allowlist at the Signal ingress
(`app/ingress/signal_listener.py`), which refuses to start when empty. Nothing
past that boundary partitions tasks, because there is nothing to partition.

A future multi-user version would need an owner column here, peer-scoped Notion
queries, and per-peer filtering in selection and completion. None of that
exists, and no current code should be read as if it does.

## Database Structure

```mermaid
erDiagram
    TASKS {
        string id PK "Notion page ID"
        string title "Task description"
        select status "pending|in_progress|completed|has_subtasks"
        select work_type "focus|creative|social|independent"
        number urgency "0-100 scale"
        number time_estimate "Minutes"
        select energy_required "high|medium|low"
        date created_at "Auto-set on creation"
        date completed_at "Set on completion"
        number rejection_count "Times rejected"
        rich_text rejection_notes "Append-only log"
        rich_text ai_context "Intake conversation"
        rich_text inline_steps "Numbered steps for simple tasks"
        relation parent_task_id FK "Parent task (if sub-task)"
        number sequence "Order within parent (1, 2, 3...)"
        rich_text progress_notes "What user accomplished"
        date started_at "Set when user accepts task"
        number steps_completed "Sub-steps finished"
        number resume_count "Times user returned to task"
        date last_resumed_at "De-dup guard for resume detection"
        checkbox is_reminder "True if task is a timed reminder"
        date remind_at "Wall-clock time to fire reminder (ISO 8601)"
        select reminder_status "pending|sent (legacy: missed)"
        date due_at "Hard deadline for the task (ISO 8601, optional)"
        date reminder_scheduled_at "Timestamp set by daemon when reminder series is created (nullable)"
    }

    USER_PREFERENCES {
        string id PK "Notion page ID"
        string user_id "User identifier"
        select preferred_beverage "tea|coffee|water|none"
        rich_text comfort_spot "Favorite working spot"
        rich_text transition_ritual "Between-task ritual"
        select focus_music "lo-fi|classical|silence|none"
        rich_text break_activity "Preferred break activity"
        rich_text focus_prefs "JSON: focus work preferences"
        rich_text creative_prefs "JSON: creative work preferences"
        rich_text social_prefs "JSON: social work preferences"
        rich_text independent_prefs "JSON: independent work preferences"
        rich_text task_patterns "JSON: task pattern preferences"
        rich_text time_prefs "JSON: time-of-day preferences"
        rich_text energy_prefs "JSON: energy level adjustments"
        date updated_at "Last preference update"
    }

    TASKS ||--o{ TASKS : "has sub-tasks"
```

## Property Definitions

### Title (title)
Main task description as entered by user.

```mermaid
flowchart LR
    User["User: Review Sarah's proposal"] --> Stored["Title: Review Sarah's proposal"]
```

**Constraints:**
- Required
- Max 200 chars (app-enforced)
- Plain text only

---

### Status (select)

Tracks task lifecycle.

```mermaid
stateDiagram-v2
    [*] --> pending: Task created
    [*] --> has_subtasks: Complex task broken down
    pending --> in_progress: User accepts task
    in_progress --> completed: User finishes
    in_progress --> pending: User abandons
    in_progress --> has_subtasks: User cannot finish (breakdown)
    pending --> completed: User says "already done"
    has_subtasks --> completed: All sub-tasks completed
```

| Value | Description | Trigger |
|-------|-------------|---------|
| `pending` | Waiting to be worked on | Default on creation |
| `in_progress` | Currently active | User accepts suggestion |
| `completed` | Finished | User marks done |
| `has_subtasks` | Parent task with hidden sub-tasks | Complex task or CANNOT_FINISH |

**Note:** No "rejected" status. Rejected tasks return to `pending` with rejection notes appended.

**Note:** Tasks with `has_subtasks` never suggested directly. Only pending sub-tasks surfaced.

---

### Work Type (select)

Categorizes work nature.

```mermaid
flowchart TD
    subgraph Focus["focus"]
        F1[Deep thinking]
        F2[Analysis]
        F3[Writing]
        F4[Coding]
        F5[Research]
    end

    subgraph Creative["creative"]
        C1[Brainstorming]
        C2[Ideation]
        C3[Design work]
        C4[Exploration]
    end

    subgraph Social["social"]
        S1[Calls]
        S2[Meetings]
        S3[Emails]
        S4[Collaboration]
    end

    subgraph Independent["independent"]
        I1[Filing]
        I2[Organizing]
        I3[Errands]
        I4[Admin work]
    end
```

| Value | Energy Level | Example Tasks |
|-------|--------------|---------------|
| `focus` | High | Write report, debug code, analyze data |
| `creative` | Medium-High | Brainstorm ideas, design logo, explore options |
| `social` | Medium | Call client, team meeting, reply to emails |
| `independent` | Low | Organize files, pay bills, book appointments |

---

### Urgency (number)

0-100 time sensitivity. **Static** — no auto-increase.

```mermaid
flowchart LR
    subgraph Scale["Urgency Scale"]
        direction TB
        Low["0-20<br/>Someday/Maybe"]
        MedLow["21-40<br/>This month"]
        Med["41-60<br/>This week"]
        MedHigh["61-80<br/>Few days"]
        High["81-100<br/>Today/Overdue"]
    end

    Low --> MedLow --> Med --> MedHigh --> High
```

**Inference Rules:**

| Signal | Urgency Range |
|--------|---------------|
| "today", "ASAP", "urgent" | 81-100 |
| "tomorrow", "soon" | 61-80 |
| "this week", "by Friday" | 41-60 |
| "whenever", "no rush", "this month", "next week" | 0-40 |

---

### Time Estimate (min) (number)

Estimated minutes to complete.

```mermaid
flowchart LR
    subgraph Buckets["Time Buckets"]
        Quick["15-30 min<br/>Quick tasks"]
        Medium["30-60 min<br/>Standard tasks"]
        Substantial["60-120 min<br/>Focused work"]
        Extended["120+ min<br/>Major tasks"]
    end
```

**Inference Guidelines:**

| Task Type | Base Estimate |
|-----------|---------------|
| Phone call | 15 min |
| Email batch | 20 min |
| Quick meeting | 30 min |
| Standard meeting | 60 min |
| Writing (short) | 30-45 min |
| Writing (long) | 90-120 min |
| Coding (bug fix) | 45 min |
| Coding (feature) | 120+ min |

---

### Energy Required (select)

Cognitive/physical energy needed.

```mermaid
flowchart TD
    subgraph Mapping["Work Type to Energy"]
        Focus[focus] --> High[high]
        Creative[creative] --> MedHigh[medium-high]
        Social[social] --> Med[medium]
        Independent[independent] --> Low[low]
    end
```

| Value | Best For | Avoid When |
|-------|----------|------------|
| `high` | Well-rested, morning, caffeinated | Tired, end of day |
| `medium` | Normal energy, mid-day | Exhausted |
| `low` | Tired, low energy, winding down | — |

---

### CreatedAt (date)

Auto-populated on creation.

```
Format: ISO 8601 (2025-01-04T10:30:00Z)
```

---

### CompletedAt (date)

Set on completion. Null until then.

```mermaid
flowchart LR
    Created["CreatedAt: Jan 4, 10am"] --> Completed["CompletedAt: Jan 6, 3pm"]
    Note["Duration: ~2 days in queue"]
```

---

### RejectionCount (number)

Times user rejected this task. Starts at 0.

```mermaid
flowchart LR
    R0["0 rejections<br/>Normal priority"] --> R1["1-2 rejections<br/>Slight penalty"]
    R1 --> R3["3+ rejections<br/>Consider deprioritizing"]
```

**Score impact:**
- 0 rejections: No penalty
- 1-2: -0.05
- 3+: -0.10

---

### RejectionNotes (rich text)

Append-only log with timestamps.

```
Format:
[2025-01-04 10:30] Not in the mood for focus work
[2025-01-05 14:15] Takes too long right now
[2025-01-06 09:00] Waiting on Sarah's input
```

**Used for:** Pattern detection, blocking dependencies, learning preferences.

---

### AIContext (rich text)

Original intake conversation for reference.

```
Format:
User: I need to review Sarah's proposal
AI: Got it. Is this time-sensitive?
User: She needs feedback by Friday
AI: Added - focused work, ~30 min, moderate urgency.
```

**Used for:** Debugging labels, context on suggestion, improving intake prompts.

---

### InlineSteps (rich text)

Numbered action steps for simple tasks (not requiring hidden sub-tasks).

```
Format:
1. Find a quiet spot
2. Make the call
3. Note any follow-ups needed
```

**Core Principle:** Vague goals feel infinite — users avoid them. Concrete steps make every task feel achievable.

**Used for:** Showing what to do on accept, on-demand breakdown, step-by-step guidance.

**When populated:**
- All tasks with `time_estimate` ≤ 60 min
- All standalone tasks (no `parent_task_id`)
- Even "simple" tasks like "Call mom"

**Example values:**

| Task | Inline Steps |
|------|--------------|
| Call mom | 1. Find quiet spot\n2. Make call\n3. Note any follow-ups |
| Review proposal | 1. Read intro\n2. Check numbers\n3. Note concerns\n4. Draft feedback |
| Pay bills | 1. Open banking app\n2. Find payee\n3. Enter amount and pay |

---

### ParentTaskId (relation)

Links sub-tasks to parent. Null for standalone.

```mermaid
flowchart TD
    subgraph Parent["Parent Task"]
        P["Complete Q4 report<br/>ID: abc123<br/>Status: has_subtasks"]
    end

    subgraph SubTasks["Sub-tasks"]
        S1["Draft outline<br/>parent_task_id: abc123<br/>sequence: 1"]
        S2["Write body<br/>parent_task_id: abc123<br/>sequence: 2"]
        S3["Edit and finalize<br/>parent_task_id: abc123<br/>sequence: 3"]
    end

    P --> S1
    P --> S2
    P --> S3
```

**Constraints:**
- Self-referential relation
- Null for parents and standalones
- Set on sub-task creation

**Note:** Internal only — never exposed to users.

---

### Sequence (number)

Sub-task order within parent. Determines next suggestion.

| Value | Meaning |
|-------|---------|
| 1 | First sub-task (offered first) |
| 2 | Second sub-task |
| 3+ | Subsequent |
| null | Not a sub-task |

**Used for:** Next sub-task suggestion, logical order, skipping blocked steps.

---

### ProgressNotes (rich text)

Tracks accomplishments, especially during CANNOT_FINISH.

```
Format:
[2025-01-04 10:30] User started: "outlined the main points"
[2025-01-04 11:00] CANNOT_FINISH: "wrote intro, need to continue with body"
[2025-01-05 09:00] Sub-task 1 completed
```

**Used for:** Understanding remaining work, creating accurate sub-tasks, context on resume.

### Started At (date)

Set when user accepts and begins task. Used for:
- Actual duration calc (with CompletedAt)
- Initiation rewards
- Per-user time estimation for time blindness compensation

### StepsCompleted (number)

Count of sub-steps finished. Used for:
- First-step rewards (0→1)
- Partial progress tracking during CANNOT_FINISH
- Sub-task completion encouragement

### ResumeCount (number)

Times user returned after stepping away. Used for:
- "Back at it" rewards (re-starting is hard)
- Work pattern understanding (frequent breaks vs. sustained)
- Escalating resume messages (1st, 2nd, 3rd+)

Incremented by resume detection (see [task-lifecycle.md Phase 5.1](./task-lifecycle.md#phase-51-resume-detection)) when ALL:
1. Task `status = in_progress`
2. Gap ≥ 15 min since last user message
3. No resume already recorded for this gap (checked via `last_resumed_at`)

---

### LastResumedAt (date)

Most recent resume detection timestamp. De-dup guard — prevents multiple resume events per inactivity gap.

```
Format: ISO 8601 (2025-01-04T10:30:00Z)
```

**Set when:** Resume detection fires (all three conditions met)
**Checked by:** Resume detection gate — if `last_resumed_at` falls after gap start, resume already recorded, skip
**Reset when:** Never — each resume overwrites with new timestamp

See [task-lifecycle.md Phase 5.1](./task-lifecycle.md#phase-51-resume-detection) for full detection mechanism.

---

### Is Reminder (checkbox)

Flags task as time-specific reminder, not normal work item. Not surfaced in normal task selection. At intake, the agent creates the Notion reminder row and writes a `reminder_outbox` row for delivery by the APScheduler reminder worker at `remind_at`.

| Value | Description |
|-------|-------------|
| `true` | Timed reminder |
| `false` | Normal task (default) |

**Set when:** AI detects reminder language during intake (e.g., "remind me at 6pm", "ping me at 3pm to call Sarah").

---

### Remind At (date)

Wall-clock time reminder becomes due. Full ISO 8601 with timezone for comparison against current time.

```
Format: ISO 8601 with timezone (e.g., 2025-01-04T18:00:00-06:00)
```

**Set when:** Task created with `is_reminder = true`. AI parses time reference (including timezone like "6pm PT") and converts to full ISO 8601. Relative phrases such as "tomorrow", "tonight", and day-of-week names must be resolved against the user's configured timezone, not UTC message metadata; use `scripts/user-time-context.sh` when a UTC timestamp needs conversion first.

**Used by:** `app/scheduler/reminder_worker.py` — the Postgres outbox worker claims rows with `state='pending'` and `due_at <= now()`, delivers via signal-cli, and marks the outbox row delivered on success. Rows with `kind='reminder'` then call `app.tools.notion.complete_reminder(page_id, "sent")`. Rows with `kind='deadline'` leave the task page open. Delivery failures retry with exponential backoff (states: `pending` → `scheduled` → `delivering` → `delivered`/`failed`/`dead`). At-least-once contract: prefer duplicate delivery over loss; duplicates are detectable via `signal_timestamp`.

---

### Reminder Status (select)

Tracks reminder delivery.

| Value | Description | Trigger |
|-------|-------------|---------|
| `pending` | Not yet delivered | Default on creation |
| `sent` | Delivered successfully | Delivering session confirms after one-shot delivery or handoff-file fallback |

**Note:** When marked `sent`, task `Status` also → `Completed` — reminder action (notifying user) done.

**Legacy compatibility:** Older deployments may still have a `missed` select option or historical rows using it. Runtime behavior no longer produces that value; if a legacy handoff or record is touched during delivery, normalize it to `sent`.

**Design constraint:** The reminder worker does not set `Reminder Status` to `sent` until after the Signal delivery succeeds. Pre-delivery mutation would cause the row to drop from the `pending` query before confirmed delivery, which could result in silent loss.

---

### Due At (date)

Hard deadline for the task. Optional — null for tasks without a time-bound.

```
Format: ISO 8601 with timezone (e.g., 2026-06-03T17:00:00-05:00)
```

**Set by:** `create_task(due_at_iso=...)` when a deadline is provided. Independent of `urgency` — urgency reflects relative priority; `Due At` is a hard time bound.

**Read by:** `query_tasks_with_unscheduled_deadlines()` filters tasks where `Due At is_not_empty` and `Reminder Scheduled At is_empty` to surface deadline-bearing tasks that have not yet had reminders scheduled. `query_scheduled_tasks_with_deadlines()` filters tasks where both fields are set so the deadline scheduler can compare current Notion deadlines with the Postgres ledger and refresh edited series.

---

### Reminder Scheduled At (date)

Timestamp marking that the automated reminder series for this task has been created. Nullable — null until set.

```
Format: ISO 8601 UTC (e.g., 2026-06-01T04:00:00+00:00)
```

**Set by:** `mark_reminder_scheduled(page_id)` — PATCHes this field to UTC now. Serves as an idempotency guard: tasks with this field set are excluded by `query_tasks_with_unscheduled_deadlines()`.

**Read by:** `query_tasks_with_unscheduled_deadlines()` filters to `Reminder Scheduled At is_empty` so only unprocessed tasks are returned. `query_scheduled_tasks_with_deadlines()` filters to `Reminder Scheduled At is_not_empty` so the backstop can detect deadline edits after a series already exists.

---

## User Preferences Properties

Personalized settings for task execution success. See [user-preferences.md](./user-preferences.md) for full docs.

### PreferredBeverage (select)

| Value | Description |
|-------|-------------|
| `tea` | Hot tea (default for social/creative) |
| `coffee` | Coffee (default for focus) |
| `water` | Water or no preference |
| `none` | No beverage prompts |

---

### ComfortSpot (rich text)

Favorite working location(s).

```
Examples:
- "Cozy chair in the living room"
- "Standing desk in the office"
- "Kitchen table with morning light"
- "Patio when weather is nice"
```

**Used for:** Environment setup in task breakdowns.

---

### TransitionRitual (rich text)

Between-task activity.

```
Examples:
- "Quick stretch"
- "3 deep breaths"
- "Walk to get water"
- "Pet the cat"
```

**Used for:** Suggesting breaks, helping user reset.

---

### FocusMusic (select)

Music preference during focused work.

| Value | Description |
|-------|-------------|
| `lo-fi` | Lo-fi beats, ambient electronic |
| `classical` | Classical, instrumentals |
| `silence` | Quiet environment |
| `none` | No music suggestions |

---

### BreakActivity (rich text)

Preferred post-task activity.

```
Examples:
- "Quick walk around the block"
- "Cup of tea on the patio"
- "5 minutes with a book"
- "Check in with partner"
```

---

### Work Type Preferences (rich text - JSON)

Four fields with work-type-specific preferences:

**FocusPrefs:**
```json
{
  "beverage": "coffee",
  "environment": "quiet office, door closed",
  "prep_steps": ["put phone in another room", "close email"],
  "music": "lo-fi",
  "ideal_duration": "45-90 min"
}
```

**CreativePrefs:**
```json
{
  "beverage": "tea",
  "environment": "natural light, open space",
  "prep_steps": ["brief walk", "grab notebook"],
  "tools": "paper before digital"
}
```

**SocialPrefs:**
```json
{
  "beverage": "tea",
  "environment": "comfortable, quiet spot",
  "prep_steps": ["review context", "set intention"],
  "follow_up": "note key takeaways"
}
```

**IndependentPrefs:**
```json
{
  "beverage": "water",
  "environment": "anywhere",
  "batching": true,
  "reward": "treat after batch"
}
```

---

### TaskPatterns (rich text - JSON)

Preferences for recurring task types.

```json
{
  "phone_calls": {
    "prep_steps": ["find quiet room", "review last interaction"],
    "environment": "comfortable seat",
    "beverage": "tea"
  },
  "writing": {
    "warmup": "2 min free-write",
    "breaks": "every 25 min"
  },
  "email_batch": {
    "setup": ["close tabs", "set timer"],
    "approach": "quick ones first"
  }
}
```

---

### TimePrefs (rich text - JSON)

Time-of-day preferences.

```json
{
  "morning": {
    "beverage": "coffee",
    "best_for": ["focus", "creative"],
    "energy_tolerance": "high"
  },
  "afternoon": {
    "beverage": "tea",
    "best_for": ["social", "creative"],
    "note": "post-lunch dip"
  },
  "evening": {
    "beverage": "herbal tea",
    "best_for": ["independent"],
    "energy_tolerance": "low"
  }
}
```

---

### EnergyPrefs (rich text - JSON)

Adjustments by current energy level.

```json
{
  "high": {
    "prep_style": "minimal",
    "task_duration": "longer ok"
  },
  "medium": {
    "prep_style": "standard rituals",
    "task_duration": "normal"
  },
  "low": {
    "prep_style": "extended, comfort-focused",
    "task_duration": "shorter tasks",
    "extra_steps": ["comfortable spot", "warm drink"]
  }
}
```

---

## Sub-task Relationships

```mermaid
erDiagram
    PARENT_TASK ||--o{ SUB_TASK : contains
    PARENT_TASK {
        string id PK
        string title
        select status "has_subtasks"
        rich_text progress_notes
    }
    SUB_TASK {
        string id PK
        string title
        select status "pending|in_progress|completed"
        relation parent_task_id FK
        number sequence
    }
```

### Parent Task Completion

Parent auto-moves to `completed` when all sub-tasks complete:

```mermaid
flowchart TD
    Check{All sub-tasks<br/>completed?}
    Check -->|Yes| Complete[Parent status → completed]
    Check -->|No| Wait[Parent stays has_subtasks]
```

---

## API Operations

### Create Task

```mermaid
sequenceDiagram
    participant Agent as App
    participant Notion as Notion API

    Agent->>Notion: POST /v1/pages
    Note over Agent,Notion: Request body includes:<br/>parent: {database_id}<br/>properties: {all fields}
    Notion-->>Agent: 200 OK with page ID
```

**Required on Create:**
- Title
- Status: `pending`
- Work Type
- Urgency
- Time Estimate (min)
- Energy Required
- CreatedAt

---

### Query Tasks

```mermaid
sequenceDiagram
    participant Agent as App
    participant Notion as Notion API

    Agent->>Notion: POST /v1/databases/{id}/query
    Note over Agent,Notion: Filter: status = pending<br/>Sort: urgency DESC
    Notion-->>Agent: Array of task pages
```

**Common Queries:**

| Purpose | Filter |
|---------|--------|
| All pending | `status = "pending"` |
| Short tasks | `status = "pending" AND time_estimate <= 30` |
| High urgency | `status = "pending" AND urgency >= 70` |
| Focus work | `status = "pending" AND work_type = "focus"` |
| Sub-tasks of parent | `parent_task_id = "{parent_id}"` |
| Next sub-task | `parent_task_id = "{parent_id}" AND status = "pending"` (sort by sequence) |
| Standalone tasks only | `parent_task_id IS NULL AND status != "has_subtasks"` |
| Parent tasks | `status = "has_subtasks"` |

---

### Update Task

```mermaid
sequenceDiagram
    participant Agent as App
    participant Notion as Notion API

    Agent->>Notion: PATCH /v1/pages/{id}
    Note over Agent,Notion: Update specific properties
    Notion-->>Agent: 200 OK
```

**Common Updates:**

| Action | Fields Updated |
|--------|----------------|
| Accept task | `status → in_progress` |
| Complete task | `status → completed, completedAt → now` |
| Reject task | `rejectionCount += 1, rejectionNotes += reason` |
| Unblock task | Clear blocked status in rejectionNotes |
| Cannot finish | `status → has_subtasks, progressNotes += progress` |
| Resume task | `resume_count += 1, last_resumed_at → now, progressNotes += "[ts] Resumed (gap: Xm)"` |
| Create sub-task | `parent_task_id, sequence, status = pending` |
| Complete sub-task | `status → completed` (check if parent complete) |

---

## Data Flow Diagram

```mermaid
flowchart TD
    subgraph Intake["Task Intake"]
        I1[User message] --> I2[AI parsing]
        I2 --> I3[Label inference]
        I3 --> I4{Complex task?}
        I4 -->|No| I5[Create single task]
        I4 -->|Yes| I6[Create parent + sub-tasks]
    end

    subgraph Storage["Notion Database"]
        DB[(Tasks)]
    end

    subgraph Selection["Task Selection"]
        S1[Query pending] --> S2[Score tasks]
        S2 --> S3[Return best match]
    end

    subgraph Update["State Updates"]
        U1[Accept → in_progress]
        U2[Complete → completed]
        U3[Reject → append notes]
        U4[Cannot finish → breakdown]
    end

    I5 --> DB
    I6 --> DB
    DB --> S1
    S3 --> U1
    S3 --> U2
    S3 --> U3
    U1 --> U4
    U4 --> DB
    U1 --> DB
    U2 --> DB
    U3 --> DB
```

## Notion Setup Instructions

### 1. Create Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click "New integration"
3. Name: `hide-my-list`
4. Capabilities: Read, Update, Insert content
5. Copy "Internal Integration Token"

### 2. Create Database

1. Create new Notion page
2. Add full-page database (table view)
3. Add properties matching schema above
4. Copy database ID from URL

```
URL: https://notion.so/abc123...?v=xyz
Database ID: abc123...
```

### 3. Share with Integration

1. Open database page
2. Click "Share" top right
3. Invite integration by name
4. Grant "Can edit"

### 4. Configure Environment

```bash
cp .env.template .env
# Then edit .env with your real values:
# NOTION_API_KEY="secret_..."
# NOTION_DATABASE_ID="abc123..."
```

`.env` is canonical source of truth. Exported shell vars supported as optional overrides for manual/ad hoc runs.

## Sample Data

### Standalone Tasks

```mermaid
flowchart TD
    subgraph Sample["Example Tasks"]
        T1["Review Sarah's proposal<br/>focus | 65 | 30min | medium"]
        T2["Call mom<br/>social | 25 | 15min | low"]
        T3["Organize receipts<br/>independent | 30 | 20min | low"]
    end
```

| Title | Work Type | Urgency | Time | Energy | Status | Parent |
|-------|----------|---------|------|--------|--------|--------|
| Review Sarah's proposal | focus | 65 | 30 | medium | pending | — |
| Call mom | social | 25 | 15 | low | pending | — |
| Organize receipts | independent | 30 | 20 | low | pending | — |
| Book dentist appointment | independent | 15 | 10 | low | completed | — |

### Parent Task with Sub-tasks (Hidden from User)

```mermaid
flowchart TD
    subgraph Parent["Parent Task"]
        P["Complete Q4 report<br/>focus | 70 | — | high<br/>Status: has_subtasks"]
    end

    subgraph SubTasks["Sub-tasks (Hidden)"]
        S1["1. Draft outline<br/>30 min | pending"]
        S2["2. Write introduction<br/>45 min | pending"]
        S3["3. Write analysis<br/>60 min | pending"]
        S4["4. Edit and finalize<br/>30 min | pending"]
    end

    P --> S1
    P --> S2
    P --> S3
    P --> S4
```

| Title | Work Type | Urgency | Time | Energy | Status | Parent | Seq |
|-------|----------|---------|------|--------|--------|--------|-----|
| Complete Q4 report | focus | 70 | 165 | high | has_subtasks | — | — |
| Draft outline | focus | 70 | 30 | medium | pending | Q4 report | 1 |
| Write introduction | focus | 70 | 45 | high | pending | Q4 report | 2 |
| Write analysis | focus | 70 | 60 | high | pending | Q4 report | 3 |
| Edit and finalize | focus | 70 | 30 | medium | pending | Q4 report | 4 |

**User Experience:** User asks for task, sees: "How about drafting the outline for the Q4 report? Should take about 30 minutes." Never sees parent or full breakdown.
