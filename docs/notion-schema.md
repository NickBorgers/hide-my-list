# Notion Database Schema

## Overview

hide-my-list uses Notion as its database, leveraging Notion's API for all CRUD operations. This approach provides zero database setup, a visual backup interface, and rich querying capabilities.

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
        relation parent_task_id FK "Parent task (if sub-task)"
        number sequence "Order within parent (1, 2, 3...)"
        rich_text progress_notes "What user accomplished"
    }

    TASKS ||--o{ TASKS : "has sub-tasks"
```

## Property Definitions

### Title (title)
The main task description as entered by the user.

```mermaid
flowchart LR
    User["User: Review Sarah's proposal"] --> Stored["Title: Review Sarah's proposal"]
```

**Constraints:**
- Required field
- Maximum 200 characters (enforced by application)
- Plain text only

---

### Status (select)

Tracks task lifecycle state.

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

**Note:** There is no "rejected" status. Rejected tasks return to `pending` with rejection notes appended.

**Note:** Tasks with `has_subtasks` status are never directly suggested to users. Only their pending sub-tasks are surfaced.

---

### WorkType (select)

Categorizes the nature of the work required.

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

0-100 scale indicating time sensitivity. **Static** - does not auto-increase.

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
| "tomorrow", "soon" | 71-80 |
| "this week", "by Friday" | 51-70 |
| "this month", "next week" | 31-50 |
| "whenever", "no rush" | 0-30 |

---

### TimeEstimate (number)

Estimated minutes to complete the task.

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

### EnergyRequired (select)

Indicates cognitive/physical energy needed.

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

Timestamp when task was added. Auto-populated on creation.

```
Format: ISO 8601 (2025-01-04T10:30:00Z)
```

---

### CompletedAt (date)

Timestamp when task was marked complete. Null until completion.

```mermaid
flowchart LR
    Created["CreatedAt: Jan 4, 10am"] --> Completed["CompletedAt: Jan 6, 3pm"]
    Note["Duration: ~2 days in queue"]
```

---

### RejectionCount (number)

Number of times user rejected this task when suggested. Starts at 0.

```mermaid
flowchart LR
    R0["0 rejections<br/>Normal priority"] --> R1["1-2 rejections<br/>Slight penalty"]
    R1 --> R3["3+ rejections<br/>Consider deprioritizing"]
```

**Impact on Selection:**
- 0 rejections: No penalty
- 1-2 rejections: -0.05 from score
- 3+ rejections: -0.10 from score

---

### RejectionNotes (rich text)

Append-only log of rejection reasons with timestamps.

```
Format:
[2025-01-04 10:30] Not in the mood for focus work
[2025-01-05 14:15] Takes too long right now
[2025-01-06 09:00] Waiting on Sarah's input
```

**Used for:**
- Pattern detection (always rejected at certain times)
- Identifying blocking dependencies
- Learning user preferences

---

### AIContext (rich text)

Stores the original intake conversation for reference.

```
Format:
User: I need to review Sarah's proposal
AI: Got it. Is this time-sensitive?
User: She needs feedback by Friday
AI: Added - focused work, ~30 min, moderate urgency.
```

**Used for:**
- Debugging label assignments
- Providing context when task is suggested
- Improving future intake prompts

---

### ParentTaskId (relation)

Links sub-tasks to their parent task. Null for standalone tasks.

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
- Self-referential relation to same database
- Null for parent tasks and standalone tasks
- Set on sub-task creation

**Note:** This relation is used internally and never exposed to users.

---

### Sequence (number)

Order of sub-task within its parent. Determines which sub-task to offer next.

| Value | Meaning |
|-------|---------|
| 1 | First sub-task (offered first) |
| 2 | Second sub-task |
| 3+ | Subsequent sub-tasks |
| null | Not a sub-task |

**Used for:**
- Determining next sub-task to suggest after completion
- Maintaining logical order of work
- Skipping to later sub-tasks if earlier ones are blocked

---

### ProgressNotes (rich text)

Tracks what the user accomplished, especially during CANNOT_FINISH events.

```
Format:
[2025-01-04 10:30] User started: "outlined the main points"
[2025-01-04 11:00] CANNOT_FINISH: "wrote intro, need to continue with body"
[2025-01-05 09:00] Sub-task 1 completed
```

**Used for:**
- Understanding what work remains after CANNOT_FINISH
- Creating accurate sub-tasks for remaining work
- Providing context when resuming work

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

A parent task automatically moves to `completed` when all its sub-tasks are completed:

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
    participant Server as Go Server
    participant Notion as Notion API

    Server->>Notion: POST /v1/pages
    Note over Server,Notion: Request body includes:<br/>parent: {database_id}<br/>properties: {all fields}
    Notion-->>Server: 200 OK with page ID
```

**Required Fields on Create:**
- Title
- Status: `pending`
- WorkType
- Urgency
- TimeEstimate
- EnergyRequired
- CreatedAt

---

### Query Tasks

```mermaid
sequenceDiagram
    participant Server as Go Server
    participant Notion as Notion API

    Server->>Notion: POST /v1/databases/{id}/query
    Note over Server,Notion: Filter: status = pending<br/>Sort: urgency DESC
    Notion-->>Server: Array of task pages
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
    participant Server as Go Server
    participant Notion as Notion API

    Server->>Notion: PATCH /v1/pages/{id}
    Note over Server,Notion: Update specific properties
    Notion-->>Server: 200 OK
```

**Common Updates:**

| Action | Fields Updated |
|--------|----------------|
| Accept task | `status → in_progress` |
| Complete task | `status → completed, completedAt → now` |
| Reject task | `rejectionCount += 1, rejectionNotes += reason` |
| Unblock task | Clear blocked status in rejectionNotes |
| Cannot finish | `status → has_subtasks, progressNotes += progress` |
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
5. Copy the "Internal Integration Token"

### 2. Create Database

1. Create a new Notion page
2. Add a full-page database (table view)
3. Add properties matching the schema above
4. Copy the database ID from the URL

```
URL: https://notion.so/abc123...?v=xyz
Database ID: abc123...
```

### 3. Share with Integration

1. Open the database page
2. Click "Share" in the top right
3. Invite your integration by name
4. Grant "Can edit" access

### 4. Configure Environment

```bash
export NOTION_API_KEY="secret_..."
export NOTION_DATABASE_ID="abc123..."
```

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

| Title | WorkType | Urgency | Time | Energy | Status | Parent |
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

| Title | WorkType | Urgency | Time | Energy | Status | Parent | Seq |
|-------|----------|---------|------|--------|--------|--------|-----|
| Complete Q4 report | focus | 70 | 165 | high | has_subtasks | — | — |
| Draft outline | focus | 70 | 30 | medium | pending | Q4 report | 1 |
| Write introduction | focus | 70 | 45 | high | pending | Q4 report | 2 |
| Write analysis | focus | 70 | 60 | high | pending | Q4 report | 3 |
| Edit and finalize | focus | 70 | 30 | medium | pending | Q4 report | 4 |

**User Experience:** When the user asks for a task, they see: "How about drafting the outline for the Q4 report? Should take about 30 minutes." They never see the parent task or full breakdown.
