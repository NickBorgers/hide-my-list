# Notion Database Schema

## Overview

hide-my-list uses Notion as its database, leveraging Notion's API for all CRUD operations. This approach provides zero database setup, a visual backup interface, and rich querying capabilities.

## Database Structure

```mermaid
erDiagram
    TASKS {
        string id PK "Notion page ID"
        string title "Task description"
        select status "pending|in_progress|completed"
        select work_type "focus|creative|social|independent"
        number urgency "0-100 scale"
        number time_estimate "Minutes"
        select energy_required "high|medium|low"
        date created_at "Auto-set on creation"
        date completed_at "Set on completion"
        number rejection_count "Times rejected"
        rich_text rejection_notes "Append-only log"
        rich_text ai_context "Intake conversation"
    }
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
    pending --> in_progress: User accepts task
    in_progress --> completed: User finishes
    in_progress --> pending: User abandons
    pending --> completed: User says "already done"
```

| Value | Description | Trigger |
|-------|-------------|---------|
| `pending` | Waiting to be worked on | Default on creation |
| `in_progress` | Currently active | User accepts suggestion |
| `completed` | Finished | User marks done |

**Note:** There is no "rejected" status. Rejected tasks return to `pending` with rejection notes appended.

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

---

## Data Flow Diagram

```mermaid
flowchart TD
    subgraph Intake["Task Intake"]
        I1[User message] --> I2[AI parsing]
        I2 --> I3[Label inference]
        I3 --> I4[Create in Notion]
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
    end

    I4 --> DB
    DB --> S1
    S3 --> U1
    S3 --> U2
    S3 --> U3
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

```mermaid
flowchart TD
    subgraph Sample["Example Tasks"]
        T1["Review Sarah's proposal<br/>focus | 65 | 30min | medium"]
        T2["Call mom<br/>social | 25 | 15min | low"]
        T3["Organize receipts<br/>independent | 30 | 20min | low"]
        T4["Brainstorm Q2 ideas<br/>creative | 45 | 90min | high"]
    end
```

| Title | WorkType | Urgency | Time | Energy | Status |
|-------|----------|---------|------|--------|--------|
| Review Sarah's proposal | focus | 65 | 30 | medium | pending |
| Call mom | social | 25 | 15 | low | pending |
| Organize receipts | independent | 30 | 20 | low | pending |
| Brainstorm Q2 ideas | creative | 45 | 90 | high | pending |
| Book dentist appointment | independent | 15 | 10 | low | completed |
