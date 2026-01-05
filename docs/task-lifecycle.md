# Task Lifecycle

## Overview

A task in hide-my-list goes through several states from creation to completion. This document details each phase of that journey.

## Complete Task Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Intake: User describes task

    Intake --> Labeling: Task captured
    Labeling --> Pending: Labels assigned

    Pending --> Selected: User requests task
    Pending --> Pending: Time passes (urgency static)

    Selected --> InProgress: User accepts
    Selected --> Rejected: User rejects

    Rejected --> Pending: Rejection recorded
    Rejected --> Selected: Alternative suggested

    InProgress --> Completed: User finishes
    InProgress --> Pending: User abandons

    Completed --> [*]
```

## Task States

| State | Description | Notion Status |
|-------|-------------|---------------|
| Intake | Task being captured, AI asking questions | N/A (not yet saved) |
| Labeling | AI assigning work type, urgency, time estimate | N/A (not yet saved) |
| Pending | Task saved, waiting to be selected | `pending` |
| Selected | Task suggested to user, awaiting response | `pending` |
| In Progress | User actively working on task | `in_progress` |
| Rejected | User declined, giving feedback | `pending` |
| Completed | Task finished | `completed` |

## Phase 1: Task Intake

```mermaid
flowchart TD
    Start([User sends message]) --> Parse[AI parses message]
    Parse --> IsTask{Is this a task?}

    IsTask -->|No| Chat[Handle as conversation]
    IsTask -->|Yes| Extract[Extract task details]

    Extract --> Confidence{Confidence level?}

    Confidence -->|High >80%| SaveDirect[Save with inferred labels]
    Confidence -->|Medium 50-80%| MentionLabels[Save, mention inferred labels]
    Confidence -->|Low <50%| AskQuestion[Ask ONE clarifying question]

    AskQuestion --> UserAnswer[User responds]
    UserAnswer --> Extract

    SaveDirect --> Saved([Task saved to Notion])
    MentionLabels --> Saved
```

## Phase 2: Label Assignment

```mermaid
flowchart LR
    subgraph Input["Task Description"]
        Desc["Review Sarah's proposal<br/>by Friday"]
    end

    subgraph Analysis["AI Analysis"]
        Keywords[Keyword extraction]
        Context[Context inference]
        Deadline[Deadline detection]
    end

    subgraph Labels["Assigned Labels"]
        Type[WorkType: focus]
        Urgency[Urgency: 65/100]
        Time[TimeEstimate: 30 min]
        Energy[EnergyRequired: medium]
    end

    Input --> Keywords
    Input --> Context
    Input --> Deadline

    Keywords --> Type
    Context --> Type
    Context --> Time
    Context --> Energy
    Deadline --> Urgency
```

### Label Inference Rules

```mermaid
flowchart TD
    subgraph WorkType["Work Type Inference"]
        W1[/"write, analyze, code, research"/] --> Focus[focus]
        W2[/"brainstorm, ideate, design, explore"/] --> Creative[creative]
        W3[/"call, meet, email, discuss"/] --> Social[social]
        W4[/"file, organize, pay, book"/] --> Independent[independent]
    end

    subgraph UrgencyRules["Urgency Inference"]
        U1[/"today, ASAP, urgent"/] --> High[70-100]
        U2[/"this week, by Friday"/] --> Medium[40-70]
        U3[/"whenever, no rush"/] --> Low[0-40]
    end

    subgraph TimeRules["Time Estimation"]
        T1[/"quick, brief, short"/] --> Quick[15-30 min]
        T2[/"meeting, review"/] --> Med[30-60 min]
        T3[/"project, deep work"/] --> Long[90+ min]
    end
```

## Phase 3: Task Selection

```mermaid
flowchart TD
    Request([User: "I have 30 min, feeling tired"]) --> Parse[Parse time + mood]
    Parse --> Fetch[Fetch pending tasks from Notion]
    Fetch --> Score[Score each task]

    subgraph Scoring["Scoring Algorithm"]
        TimeFit[Time Fit × 0.3]
        MoodMatch[Mood Match × 0.4]
        UrgencyScore[Urgency × 0.2]
        History[History Bonus × 0.1]

        TimeFit --> Total[Total Score]
        MoodMatch --> Total
        UrgencyScore --> Total
        History --> Total
    end

    Score --> Scoring
    Total --> Select[Select highest score]
    Select --> Present([Present to user])
```

### Scoring Details

```mermaid
flowchart LR
    subgraph TimeFit["Time Fit Score"]
        Available[Available: 30 min]
        Estimate[Task: 25 min]
        Available --> Calc1{Fits?}
        Estimate --> Calc1
        Calc1 -->|Yes, buffer OK| T1["1.0"]
        Calc1 -->|Tight| T2["0.5"]
        Calc1 -->|Too long| T3["0.0"]
    end

    subgraph MoodMatch["Mood Match Score"]
        UserMood[User: tired]
        TaskType[Task: independent]
        UserMood --> Calc2{Match?}
        TaskType --> Calc2
        Calc2 -->|Perfect| M1["1.0"]
        Calc2 -->|Neutral| M2["0.5"]
        Calc2 -->|Mismatch| M3["0.0"]
    end
```

### Mood to Work Type Matching

```mermaid
quadrantChart
    title Mood to Work Type Affinity
    x-axis Low Match --> High Match
    y-axis Low Energy --> High Energy
    quadrant-1 Creative Tasks
    quadrant-2 Focus Tasks
    quadrant-3 Independent Tasks
    quadrant-4 Social Tasks
    "Tired User": [0.2, 0.2]
    "Focused User": [0.8, 0.7]
    "Creative User": [0.7, 0.8]
    "Social User": [0.8, 0.6]
```

## Phase 4: Task Execution

```mermaid
stateDiagram-v2
    state "Task Presented" as Presented
    state "Decision Point" as Decision
    state "Working" as Working
    state "Completion Check" as Check

    [*] --> Presented
    Presented --> Decision

    Decision --> Working: Accept
    Decision --> Rejected: Reject

    Working --> Check: User signals done
    Working --> Interrupted: User needs to switch

    Check --> Completed: Confirmed done
    Check --> Working: Need more time

    Interrupted --> Pending: Task returned to queue
    Rejected --> Alternative: Get new suggestion
    Alternative --> Presented

    Completed --> [*]
```

## Phase 5: Rejection Handling

```mermaid
flowchart TD
    Reject([User rejects task]) --> Why{Ask why}

    Why --> Timing["Takes too long"]
    Why --> Mood["Not in the mood"]
    Why --> Blocked["Waiting on something"]
    Why --> Done["Already done"]
    Why --> Other["Just not feeling it"]

    Timing --> UpdateTime[Adjust time estimate]
    Mood --> RecordMood[Record mood mismatch]
    Blocked --> MarkBlocked[Mark as blocked]
    Done --> Complete[Mark completed]
    Other --> IncrementReject[Increment rejection count]

    UpdateTime --> FindAlt[Find alternative]
    RecordMood --> FindAlt
    IncrementReject --> FindAlt

    MarkBlocked --> Retry([Return to queue])
    Complete --> Celebrate([Celebrate completion])
    FindAlt --> Present([Present new task])
```

### Rejection Learning

```mermaid
flowchart LR
    subgraph Pattern["Pattern Detection"]
        R1[3+ rejections<br/>same work type] --> Learn1[Lower work type<br/>affinity for time of day]
        R2[Rejected when<br/>user said 'tired'] --> Learn2[Increase energy<br/>requirement]
        R3[Time estimate<br/>mismatch] --> Learn3[Adjust estimate<br/>multiplier]
    end

    subgraph Action["Action Taken"]
        Learn1 --> A1[Avoid suggesting<br/>focus work at night]
        Learn2 --> A2[Only suggest<br/>low-energy tasks]
        Learn3 --> A3[Multiply estimates<br/>by 1.2x]
    end
```

## Phase 6: Task Completion

```mermaid
flowchart TD
    Done([User: "Done!"]) --> Update[Update Notion status]
    Update --> Feedback{Ask for feedback?}

    Feedback -->|Optional| HowFelt["How did that feel?"]
    Feedback -->|Skip| Summary

    HowFelt --> Easier["Easier than expected"]
    HowFelt --> Right["About right"]
    HowFelt --> Harder["Harder than expected"]

    Easier --> AdjustDown[Lower time estimate]
    Right --> NoChange[Keep estimate]
    Harder --> AdjustUp[Increase time estimate]

    AdjustDown --> Summary[Session Summary]
    NoChange --> Summary
    AdjustUp --> Summary

    Summary --> Prompt{Continue?}
    Prompt -->|Yes| NextTask([Get another task])
    Prompt -->|No| Celebrate([End session])
```

## Complete Task Journey Example

```mermaid
journey
    title Task: "Review Sarah's proposal"
    section Intake
      User describes task: 5: User
      AI asks about deadline: 3: AI
      User says "by Friday": 5: User
      AI confirms and labels: 4: AI
    section Waiting
      Task sits in Notion: 3: System
      2 days pass: 2: System
    section Selection
      User has 30 minutes: 5: User
      AI suggests this task: 4: AI
      User accepts: 5: User
    section Execution
      User reviews proposal: 4: User
      User marks done: 5: User
      AI celebrates: 5: AI
```
