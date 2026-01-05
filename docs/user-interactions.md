# User Interactions

## Overview

All interactions in hide-my-list happen through natural language conversation. The AI determines user intent from their message and responds appropriately. This document details all conversation flows and user journeys.

## Intent Detection

```mermaid
flowchart TD
    Message([User Message]) --> Detect[AI Intent Detection]

    Detect --> AddTask["ADD_TASK<br/>Adding new task"]
    Detect --> GetTask["GET_TASK<br/>Ready to work"]
    Detect --> Complete["COMPLETE<br/>Finished task"]
    Detect --> Reject["REJECT<br/>Don't want this task"]
    Detect --> CannotFinish["CANNOT_FINISH<br/>Task too large"]
    Detect --> NeedHelp["NEED_HELP<br/>Wants breakdown assistance"]
    Detect --> CheckIn["CHECK_IN<br/>System follow-up"]
    Detect --> Chat["CHAT<br/>General conversation"]

    AddTask --> IntakeFlow[Task Intake Flow]
    GetTask --> SelectionFlow[Task Selection Flow]
    Complete --> CompletionFlow[Completion Flow]
    Reject --> RejectionFlow[Rejection Flow]
    CannotFinish --> BreakdownFlow[Task Breakdown Flow]
    NeedHelp --> AssistFlow[Breakdown Assistance Flow]
    CheckIn --> CheckInFlow[Check-In Flow]
    Chat --> ChatResponse[Conversational Response]
```

### Intent Signal Examples

| Intent | Example Messages |
|--------|------------------|
| ADD_TASK | "I need to...", "Add...", "Remind me to...", "New task:" |
| GET_TASK | "I have X minutes", "What should I do?", "I'm ready to work" |
| COMPLETE | "Done", "Finished", "Completed", "I did it" |
| REJECT | "Not that one", "Something else", "I don't want to" |
| CANNOT_FINISH | "This is too big", "I can't finish this", "Too much for one sitting" |
| NEED_HELP | "How do I start?", "What should I do first?", "I'm stuck", "Break this down" |
| CHECK_IN | System-initiated (timer triggered, not user message) |
| CHAT | "Hello", "How does this work?", "What's in my list?" |

## Flow 1: Task Intake

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant N as Notion

    U->>AI: "I need to review Sarah's proposal"

    Note over AI: Parse task, infer labels
    Note over AI: Confidence: WorkType=85%, Urgency=40%, Time=60%

    AI->>U: "Got it. Is this time-sensitive, or can it wait?"
    U->>AI: "She needs feedback by Friday"

    Note over AI: Urgency now 90% confident → 65/100
    Note over AI: All labels confident enough

    AI->>N: Create task with labels
    N-->>AI: Task created
    AI->>U: "Added - focused work, ~30 min, moderate urgency. Ready for another?"
```

### Intake Conversation Tree

```mermaid
flowchart TD
    Start([User describes task]) --> Parse[AI parses description]
    Parse --> WorkType{Work type clear?}

    WorkType -->|Yes| Urgency
    WorkType -->|No| AskType["What kind of work is this?<br/>Focused / Creative / Social / Routine"]
    AskType --> TypeAnswer[User responds]
    TypeAnswer --> Urgency

    Urgency{Urgency clear?}
    Urgency -->|Yes| Time
    Urgency -->|No| AskUrgency["Is this time-sensitive?"]
    AskUrgency --> UrgencyAnswer[User responds]
    UrgencyAnswer --> Time

    Time{Time estimate possible?}
    Time -->|Yes| Save
    Time -->|No| AskTime["About how long will this take?"]
    AskTime --> TimeAnswer[User responds]
    TimeAnswer --> Save

    Save[Save to Notion] --> Confirm([Confirm to user])
```

### Quick Capture vs. Detailed Intake

```mermaid
flowchart LR
    subgraph Quick["Quick Capture (Common)"]
        Q1[User: "Call mom"] --> Q2[AI: "Added - social, 15 min, low urgency"]
    end

    subgraph Detailed["Detailed Intake (When Needed)"]
        D1[User: "Work on the project"] --> D2[AI: "Which project?"]
        D2 --> D3[User: "The Q4 marketing plan"]
        D3 --> D4[AI: "Got it. Is this creative work or more analytical?"]
        D4 --> D5[User: "Creative, brainstorming phase"]
        D5 --> D6[AI: "Added - creative work, 90 min estimate"]
    end
```

## Flow 2: Task Selection

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant N as Notion

    U->>AI: "I have 20 minutes and feeling tired"

    Note over AI: Parse: time=20, mood=tired→independent

    AI->>N: Query pending tasks
    N-->>AI: Return 8 pending tasks

    Note over AI: Score each task
    Note over AI: Best match: "Organize receipts" (score: 0.87)

    AI->>U: "How about organizing your receipts from last week? It's low-energy admin work and should take about 15 minutes."

    alt User accepts
        U->>AI: "Sure"
        AI->>N: Update status → in_progress
        AI->>U: "Great, it's yours. Let me know when you're done!"
    else User rejects
        U->>AI: "Not that one"
        Note over AI: Start rejection flow
    end
```

### Selection Decision Tree

```mermaid
flowchart TD
    Request([User requests task]) --> ParseContext[Parse time + mood]
    ParseContext --> FetchTasks[Fetch pending tasks]
    FetchTasks --> HasTasks{Any tasks?}

    HasTasks -->|No| NoTasks["Your slate is clear!<br/>Want to add something?"]
    HasTasks -->|Yes| FilterTime[Filter by time constraint]

    FilterTime --> HasMatches{Any fit time?}
    HasMatches -->|No| NoFit["Nothing fits that timeframe.<br/>Got more time?"]
    HasMatches -->|Yes| ScoreTasks[Score remaining tasks]

    ScoreTasks --> BestScore{Best score > 0.5?}
    BestScore -->|Yes| Suggest[Suggest task confidently]
    BestScore -->|No| Unsure["Nothing's a perfect match.<br/>Want to try X anyway?"]

    Suggest --> Present([Present to user])
```

### Mood Interpretation

```mermaid
flowchart LR
    subgraph Input["User Says"]
        I1["focused / in the zone / sharp"]
        I2["creative / inspired / brainstormy"]
        I3["social / energetic / chatty"]
        I4["tired / low energy / drained"]
        I5["stressed / anxious / overwhelmed"]
    end

    subgraph Output["Best Work Type"]
        O1[Focus Work]
        O2[Creative Work]
        O3[Social Work]
        O4[Independent/Admin]
        O5[Independent/Admin]
    end

    I1 --> O1
    I2 --> O2
    I3 --> O3
    I4 --> O4
    I5 --> O5
```

## Flow 3: Task Completion

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant R as Reward Engine
    participant N as Notion
    participant HA as Home Audio
    participant SMS as SMS Service

    U->>AI: "Done!"

    AI->>N: Update task status → completed
    AI->>N: Set completedAt timestamp
    N-->>AI: Success

    AI->>R: Trigger reward evaluation
    R->>R: Calculate intensity score

    par Parallel Reward Delivery
        R->>AI: Emoji celebration + GIF
        R->>HA: Play victory music
        R->>SMS: Text significant other
    end

    AI->>U: "CRUSHED IT! 🔥💪✨ [GIF: Taylor Swift party]"

    Note over HA: "We Are The Champions" plays

    alt High intensity achievement
        R->>AI: Generate outing suggestion
        AI->>U: "You've earned it! Coffee at Luna Cafe? ☕"
    end

    alt User provides feedback
        U->>AI: "Easier than expected"
        Note over AI: Reduce time estimate for similar tasks
        AI->>U: "Good to know - I'll remember that. Ready for another?"
    else User skips
        U->>AI: "Next task"
        AI->>U: "What's your energy like now?"
    end
```

### Reward Delivery System

The completion flow triggers a multi-channel reward system designed to maximize dopamine delivery:

```mermaid
flowchart TD
    Done([User: "Done!"]) --> Evaluate[Evaluate Achievement]

    Evaluate --> Score[Calculate Intensity Score]
    Score --> Level{Intensity Level}

    Level -->|Low| LowReward[Emoji only<br/>"Nice! ✨"]
    Level -->|Medium| MedReward[Emoji + GIF<br/>"Crushing it! 🎉💪"]
    Level -->|High| HighReward[Emoji + GIF + Music + Text SO]
    Level -->|Epic| EpicReward[All rewards + AI Video + Outing]

    subgraph SystemRewards["System-Generated Rewards"]
        Emoji[Emoji Explosion]
        GIF[Animated GIF<br/>Taylor Swift, celebrations]
        Video[AI Video via Sora]
        Music[Home Audio Playback<br/>Sonos/HomePod/Echo]
    end

    subgraph InterpersonalRewards["Interpersonal Rewards"]
        TextSO[Text Significant Other<br/>"Your partner crushed a big task!"]
        Outing[Suggest Fun Outing<br/>"Coffee at your favorite spot?"]
    end

    HighReward --> SystemRewards
    HighReward --> InterpersonalRewards
    EpicReward --> SystemRewards
    EpicReward --> InterpersonalRewards
```

### Intensity Scoring

| Factor | Weight | Examples |
|--------|--------|----------|
| Task difficulty | 30% | Time estimate, energy required |
| Current streak | 25% | 3+ tasks = bonus |
| Task type | 20% | Parent complete = major bonus |
| Time of day | 15% | End of day = bonus |
| Recent history | 10% | Diminishing returns if many recent rewards |

### Completion Feedback Loop

```mermaid
flowchart TD
    Done([User: "Done!"]) --> Update[Update Notion]
    Update --> Reward[Trigger Reward Engine]

    Reward --> SessionCheck{First completion today?}

    SessionCheck -->|Yes| Celebrate["FIRST ONE DOWN! 🎯✨"]
    SessionCheck -->|No| Streak["That's 3 done today! 🔥💪🎉"]

    Celebrate --> DeliverRewards
    Streak --> DeliverRewards

    DeliverRewards[Deliver Multi-Channel Rewards] --> Feedback

    Feedback{Ask for feedback?}
    Feedback -->|Sometimes| Ask["How did that feel?"]
    Feedback -->|Often| Skip[Skip to next prompt]

    Ask --> Easier[Easier than expected]
    Ask --> Right[About right]
    Ask --> Harder[Harder than expected]
    Ask --> NoAnswer[User ignores]

    Easier --> LearnEasy[Reduce time estimates]
    Right --> NoChange[Keep estimates]
    Harder --> LearnHard[Increase estimates]
    NoAnswer --> Continue

    LearnEasy --> Continue
    NoChange --> Continue
    LearnHard --> Continue

    Skip --> Continue{Continue working?}
    Continue -->|Yes| NextTask[Get another task]
    Continue -->|No| OutingCheck{High intensity?}
    OutingCheck -->|Yes| SuggestOuting["You've earned a break!<br/>How about [favorite_activity]?"]
    OutingCheck -->|No| End([End session])
    SuggestOuting --> End
```

## Flow 4: Task Rejection

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant N as Notion

    AI->>U: "How about the quarterly report?"
    U->>AI: "Not that one"

    AI->>U: "No problem - what's steering you away from it?"
    U->>AI: "Not in the mood for deep work"

    Note over AI: Record: mood mismatch for focus work

    AI->>N: Update rejectionNotes
    AI->>N: Increment rejectionCount

    Note over AI: Re-score with mood constraint

    AI->>U: "Got it. How about organizing your inbox? Still productive but lighter work."
```

### Rejection Reason Categories

```mermaid
flowchart TD
    Reject([User rejects]) --> Why["What's steering you away?"]

    Why --> TooLong["Takes too long"]
    Why --> WrongMood["Not in the mood"]
    Why --> Blocked["Waiting on something"]
    Why --> AlreadyDone["Already done"]
    Why --> NotFeeling["Just not feeling it"]

    TooLong --> ActionTime[Suggest shorter task<br/>Consider adjusting estimate]
    WrongMood --> ActionMood[Suggest different work type<br/>Remember mood preference]
    Blocked --> ActionBlock[Mark task as blocked<br/>Don't suggest until unblocked]
    AlreadyDone --> ActionDone[Mark task completed<br/>Celebrate the win!]
    NotFeeling --> ActionGeneral[Log rejection<br/>Try different task]
```

### Rejection Escalation

```mermaid
flowchart TD
    R1[1st Rejection] --> Suggest1[Suggest alternative]
    Suggest1 --> R2[2nd Rejection]
    R2 --> Suggest2[Suggest very different task]
    Suggest2 --> R3[3rd Rejection]
    R3 --> Escalate["Having trouble finding a match.<br/>Want to tell me what you're in the mood for?"]
    Escalate --> UserInput[User describes preference]
    UserInput --> CustomSearch[Search with explicit criteria]
    CustomSearch --> R4{4th+ Rejection?}
    R4 -->|Yes| GracefulExit["Maybe now's not the time.<br/>I'll be here when you're ready."]
    R4 -->|No| Continue[Continue suggesting]
```

## Flow 5: Cannot Finish (Task Breakdown)

When a user indicates they cannot finish a task, it signals the task was too large and needs to be broken down into smaller sub-tasks. **Critically, the AI must first ask what the user accomplished** to understand what remains.

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant N as Notion

    AI->>U: "How about working on the Q4 marketing plan?"
    U->>AI: "I started but this is too big to finish"

    Note over AI: CANNOT_FINISH detected

    AI->>U: "No worries - what did you get into before stopping?"
    U->>AI: "I drafted the outline and did initial research"

    Note over AI: Analyze: Outline + research done<br/>Writing and analysis remain

    AI->>N: Create sub-tasks for remainder (hidden)
    AI->>N: Update parent task with progress notes

    Note over N: Hidden sub-tasks created:<br/>1. Write executive summary<br/>2. Draft competitive analysis<br/>3. Finalize recommendations

    AI->>U: "Good progress! Ready to tackle the executive summary? Should take about 30 min."
```

### Cannot Finish Decision Tree

```mermaid
flowchart TD
    CannotFinish([User: "This is too big"]) --> AskProgress["Ask: What did you accomplish?"]
    AskProgress --> UserDescribes[User describes progress]
    UserDescribes --> Analyze[Analyze remaining work]
    Analyze --> HasSubtasks{Already has sub-tasks?}

    HasSubtasks -->|Yes| UpdateProgress[Update completed sub-tasks]
    HasSubtasks -->|No| CreateSubtasks[Break remainder into sub-tasks]

    UpdateProgress --> RemainingTooLarge{Remaining sub-task too large?}
    RemainingTooLarge -->|Yes| BreakFurther[Break into smaller chunks]
    RemainingTooLarge -->|No| OfferNext[Offer next sub-task]

    CreateSubtasks --> InferBreakdown[AI infers logical breakdown<br/>based on what remains]
    InferBreakdown --> SaveHidden[Save sub-tasks to Notion<br/>Hidden from user]

    BreakFurther --> SaveHidden
    SaveHidden --> OfferFirst[Offer first remaining sub-task]

    OfferNext --> Present([Present manageable task])
    OfferFirst --> Present
```

### Breakdown Strategy

```mermaid
flowchart LR
    subgraph Input["Original Task"]
        Big["Complete the project"]
    end

    subgraph Analysis["AI Analysis"]
        Scope[Identify scope]
        Phases[Determine phases]
        FirstAction[Find first actionable step]
    end

    subgraph Output["Hidden Sub-tasks"]
        S1["Complete first revision"]
        S2["Get initial feedback"]
        S3["Incorporate revisions"]
        S4["Final review"]
    end

    Input --> Scope
    Scope --> Phases
    Phases --> FirstAction
    FirstAction --> S1
    S1 -.-> S2
    S2 -.-> S3
    S3 -.-> S4
```

**Key Principles:**
- Sub-task breakdown is NEVER shown to the user as a full list
- Each sub-task should be completable in one sitting (typically 15-90 min)
- The AI presents only the current actionable sub-task
- Parent task only completes when all sub-tasks are done

### Cannot Finish Response Templates

| Scenario | Response Template |
|----------|-------------------|
| First time | "Got it - that's a big one. I've broken it into smaller pieces. Ready for the first chunk?" |
| Already broken | "Still too much? Let me find an even smaller piece to start with." |
| Can't break further | "This is pretty atomic. What specific part is blocking you?" |
| Making progress | "Nice - one piece done. Ready for the next bit?" |

---

## Flow 6: Breakdown Assistance (On-Demand Help)

A core principle of hide-my-list: **users interpret vague goals as infinite, and thus avoid them.** The agent must always be ready to help users understand exactly what to do next. This flow handles when users need help starting or continuing a task.

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant

    AI->>U: "How about writing the project proposal?"
    U->>AI: "Sure"
    AI->>U: "Great! Here's the plan:<br/>1. Open a new doc<br/>2. Write the problem statement (2-3 sentences)<br/>3. List 3 proposed solutions<br/>4. Add timeline estimate<br/>Start with step 1 - let me know when you're ready for the next!"

    Note over U,AI: User works on step 1

    U->>AI: "What's next?"
    AI->>U: "Step 2: Write the problem statement. Just 2-3 sentences describing what needs solving. What's the main issue this project addresses?"
```

### Breakdown Assistance Decision Tree

```mermaid
flowchart TD
    Start([User needs help]) --> HasActive{Has active task?}

    HasActive -->|Yes| Analyze[Analyze where user is stuck]
    HasActive -->|No| GetTask[Suggest getting a task first]

    Analyze --> StuckType{What kind of stuck?}

    StuckType -->|"How do I start?"| FirstStep[Provide exact first action]
    StuckType -->|"What are all the steps?"| ShowPlan[Show numbered steps]
    StuckType -->|"This step is too big"| BreakStep[Break current step smaller]
    StuckType -->|"I don't understand"| Clarify[Explain the step differently]

    FirstStep --> Specific["Open your browser and go to [specific URL]"]
    ShowPlan --> Numbered["1. Do X (5 min)<br/>2. Do Y (10 min)<br/>3. Do Z (5 min)"]
    BreakStep --> Smaller["Let's make that smaller:<br/>First, just [micro-action]"]
    Clarify --> Rephrase["Here's another way to think about it..."]
```

### Proactive Assistance Triggers

The agent should proactively offer breakdown assistance when it detects:

| Signal | Agent Response |
|--------|----------------|
| Long pause after accepting task | "Need help getting started? I can break this down further." |
| "Hmm" / "Uh" / hesitation words | "Want me to walk you through the steps?" |
| "I guess" / uncertain acceptance | "Let me give you a clear starting point..." |
| User returns without completing | "Picking this back up? Here's where you left off and what's next." |

### Assistance Response Levels

```mermaid
flowchart LR
    subgraph Level1["Level 1: Overview"]
        L1["Here are the 4 steps..."]
    end

    subgraph Level2["Level 2: Current Step"]
        L2["Focus on step 2: Do X"]
    end

    subgraph Level3["Level 3: Micro-Action"]
        L3["Right now, just open the file"]
    end

    subgraph Level4["Level 4: Hand-Holding"]
        L4["Click File > New > Document"]
    end

    Level1 --> Level2 --> Level3 --> Level4
```

The agent adjusts detail level based on user signals:
- Confident user → Level 1-2
- Uncertain user → Level 2-3
- Stuck user → Level 3-4
- Very stuck user → Level 4 with encouragement

### Assistance Conversation Examples

**User needs starting help:**
```
User: "Sure, I'll do the proposal"
AI: "Perfect! Let's make this concrete:
     1. Open Google Docs and create a new file
     2. Title it 'Project Proposal - [Date]'
     3. Write one sentence: What problem are you solving?

     Just do step 1 right now. Ready when you are!"
```

**User stuck mid-task:**
```
User: "I'm stuck on this"
AI: "No problem - where did you get to? Tell me the last thing you finished and I'll point you to the exact next action."
```

**User wants the full picture:**
```
User: "What are all the steps?"
AI: "Here's the full breakdown:
     1. Problem statement (5 min) ← you are here
     2. Solution options (10 min)
     3. Timeline (5 min)
     4. Quick review (5 min)

     Total: ~25 min. Want to start with the problem statement?"
```

---

## Flow 7: Check-In Follow-Up

When a user accepts a task, the system sets a timer for 1.25x the estimated completion time. If the timer expires before the user marks the task complete, the system proactively follows up.

```mermaid
sequenceDiagram
    participant U as User
    participant UI as Chat UI
    participant T as Timer
    participant AI as AI Assistant
    participant N as Notion

    U->>AI: "Sure, I'll do that"
    AI->>N: Update status → in_progress
    AI->>U: "Great, it's yours. Let me know when done!"
    AI->>UI: Return time_estimate (30 min)
    UI->>T: Set timer for 37.5 min

    Note over T: 37.5 minutes later...

    T->>UI: Timer expired
    UI->>AI: CHECK_IN request
    AI->>N: Verify task still in_progress
    N-->>AI: Yes, still in_progress
    AI->>U: "How's the quarterly report going? Still at it?"

    alt User completed
        U->>AI: "Oh yeah, finished that!"
        AI->>N: Update status → completed
        AI->>U: "Nice! Marking that off."
        UI->>T: Clear timer
    else User still working
        U->>AI: "Still working on it"
        AI->>U: "No rush - keep at it!"
        UI->>T: Reset timer (15 min)
    else User got distracted
        U->>AI: "Ugh, got distracted"
        AI->>U: "Happens to everyone. Want to jump back in?"
    else User needs more time
        U->>AI: "Need another 20 minutes"
        AI->>U: "No problem, I'll check back then."
        UI->>T: Set timer (25 min)
    else User wants to stop
        U->>AI: "Let me do something else"
        AI->>N: Update status → pending
        AI->>U: "Got it - back in the queue. What next?"
        UI->>T: Clear timer
    end
```

### Check-In Decision Tree

```mermaid
flowchart TD
    Timer([Timer expires]) --> StillActive{Task still in_progress?}

    StillActive -->|No| Skip[Skip - already handled]
    StillActive -->|Yes| CheckInCount{Check-in count?}

    CheckInCount -->|1st| Friendly["How's [task] going?"]
    CheckInCount -->|2nd| Brief["Still working on [task]?"]
    CheckInCount -->|3rd| Gentle["Want to take a break from [task]?"]
    CheckInCount -->|4th+| Stop[Stop checking in]

    Friendly --> UserResponse
    Brief --> UserResponse
    Gentle --> UserResponse

    UserResponse{User response}
    UserResponse --> Done["Done!"]
    UserResponse --> Working["Still working"]
    UserResponse --> Distracted["Got distracted"]
    UserResponse --> MoreTime["Need more time"]
    UserResponse --> Stop2["Want to stop"]

    Done --> Complete[Mark completed, clear timer]
    Working --> Reset1[Reset timer 0.5x]
    Distracted --> Nudge["Jump back in?"]
    MoreTime --> AskDuration["How much longer?"]
    Stop2 --> ReturnQueue[Return to pending]

    Nudge --> YesNo{User choice}
    YesNo -->|Yes| Reset2[Reset timer 0.5x]
    YesNo -->|No| ReturnQueue

    AskDuration --> NewTime[User provides time]
    NewTime --> Reset3[Set timer to time × 1.25]
```

### Check-In Response Templates

| Scenario | AI Response |
|----------|-------------|
| 1st check-in | "How's [task] going? Still at it?" |
| 2nd check-in | "Still working on [task]?" |
| 3rd check-in | "Want to take a break from [task]? I'll be here when you're ready." |
| User says done | "Nice! Marking that off. Ready for another?" |
| User still working | "No rush - keep at it! I'll check back in a bit." |
| User got distracted | "Happens to everyone. Want to jump back in, or try something else?" |
| User needs more time | "No problem. About how much longer do you think?" |
| User wants to stop | "Got it - I'll put that back in the queue. What would you like to do instead?" |

### Client-Side Timer Implementation

The check-in mechanism uses client-side timers to work with the stateless HTTP architecture:

```mermaid
flowchart LR
    subgraph Client["Frontend (app.js)"]
        Accept[Task accepted] --> Store[Store active task]
        Store --> SetTimer[Set setTimeout]
        SetTimer --> Timer[Timer running]
        Timer --> Fire[Timer fires]
        Fire --> Send[Send CHECK_IN]
    end

    subgraph Cleanup["Timer Cleanup"]
        Complete[User completes] --> Clear1[clearTimeout]
        Abandon[User abandons] --> Clear2[clearTimeout]
        NewTask[New task accepted] --> Clear3[clearTimeout]
    end
```

**Stored State:**
- `activeTaskId`: Current task ID
- `activeTaskTitle`: Task name for display
- `checkInTimer`: setTimeout reference
- `checkInCount`: Number of check-ins for this session

---

## Flow 8: Special Cases

### Empty Queue

```mermaid
flowchart TD
    Request([User: "What should I do?"]) --> Check[Check Notion]
    Check --> Empty{Any pending tasks?}

    Empty -->|Yes| Normal[Normal selection flow]
    Empty -->|No| Celebrate["Your slate is clear!"]

    Celebrate --> Prompt["Nothing's waiting for you.<br/>Enjoy it, or add something new?"]

    Prompt --> Add["Add a task"]
    Prompt --> Leave["Take a break"]
```

### No Good Match

```mermaid
flowchart TD
    Request([User: "15 min, feeling focused"]) --> Check[Check tasks]
    Check --> NoMatch{Any tasks match?}

    NoMatch -->|All too long| TimeIssue["Nothing fits 15 minutes.<br/>Your shortest task is 30 min."]
    NoMatch -->|Wrong mood| MoodIssue["No focus tasks available.<br/>Want to try something else?"]
    NoMatch -->|Both| BothIssue["Tough to find a match right now."]

    TimeIssue --> Options1["Got more time?<br/>Or add a quick task?"]
    MoodIssue --> Options2["How about [other type]<br/>Or change your mood?"]
    BothIssue --> Options3["Want to add something<br/>or come back later?"]
```

### User Asks About Their List

```mermaid
flowchart TD
    Ask([User: "What's in my list?"]) --> Philosophy[Explain philosophy]

    Philosophy --> Response["I keep track so you don't have to!<br/>You've got 8 tasks waiting.<br/>Ready to knock one out?"]

    Response --> Follow{User follows up?}
    Follow -->|Insists| Gentle["The point is not having to look.<br/>Trust me - I'll surface the right one."]
    Follow -->|Accepts| Selection[Normal selection flow]
```

## Conversation Tone Guidelines

```mermaid
mindmap
  root((Tone))
    Casual
      "Got it"
      "Nice work"
      "No problem"
    Brief
      One question at a time
      Short confirmations
      Minimal small talk
    Collaborative
      "Help me learn"
      "What's steering you away?"
      Never apologize excessively
    Confident
      Present suggestions directly
      Trust the algorithm
      Don't second-guess
```

## Example Full Session

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI
    participant HA as Home Audio
    participant SMS as SMS

    U->>AI: Hey
    AI->>U: Hey! Ready to add a task or work on something?

    U->>AI: I need to email the team about the offsite
    AI->>U: Got it - is there a deadline for this?
    U->>AI: Should go out today
    AI->>U: Added - social task, ~15 min, high urgency. What else?

    U->>AI: Also need to book travel for the offsite
    AI->>U: Added - independent task, ~30 min. Ready to work or keep adding?

    U->>AI: I've got 20 minutes before a meeting
    AI->>U: Perfect - how about sending that team email? It's short and urgent.
    U->>AI: Yeah let's do it
    AI->>U: Great, it's yours. Let me know when you're done!

    Note over U,AI: 12 minutes later

    U->>AI: Done

    par Celebration
        AI->>U: FIRST ONE DOWN! 🎯✨💪 [GIF: office high five]
        HA->>HA: Plays victory jingle (15 sec)
        SMS->>SMS: "[Partner], your person just knocked out their first task! 🙌"
    end

    AI->>U: Got time for another quick one?
    U->>AI: No, meeting's starting
    AI->>U: No worries - you're off to a great start! ✨
```

## Example Session with Check-In

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI
    participant T as Timer

    U->>AI: I have an hour, feeling focused
    AI->>U: How about working on the quarterly report? ~45 min of focus work.
    U->>AI: Sure
    AI->>U: Great, it's yours. Let me know when done!
    AI->>T: Set timer: 56 min (45 × 1.25)

    Note over U,T: 56 minutes later...

    T->>AI: Timer expired
    AI->>U: How's the quarterly report going? Still at it?

    U->>AI: Ugh, I got distracted reading emails
    AI->>U: Happens to everyone. Want to jump back in, or try something else?

    U->>AI: Yeah let me finish it
    AI->>U: You got this! I'll check back in a bit.
    AI->>T: Reset timer: 22 min (45 × 0.5)

    Note over U,T: 22 minutes later...

    T->>AI: Timer expired
    AI->>U: Still working on the report?

    U->>AI: Just finished!
    AI->>U: Nice work! That's a big one done. Ready for a break or another task?
    T->>T: Timer cleared
```
