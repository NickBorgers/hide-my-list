# Task Selection

Assumes you've already read `docs/ai-prompts/shared.md` for the base prompt, shame-prevention templates, user preferences context, and output handling.

## Module 3: Task Selection

```mermaid
flowchart TD
    subgraph Input["Selection Inputs"]
        Time[Available time]
        Mood[User mood/energy]
        Tasks[Pending tasks]
    end

    subgraph Scoring["Scoring Algorithm"]
        TimeFit["Time Fit × 0.3"]
        MoodMatch["Mood Match × 0.4"]
        UrgencyScore["Urgency × 0.2"]
        HistoryBonus["History × 0.1"]
    end

    subgraph Output["Selection Output"]
        Best[Best matching task]
        Reason[Selection reasoning]
        Message[User-facing message]
    end

    Input --> Scoring
    Scoring --> Output
```

### Task Selection Prompt

```
Select the best task for the user based on their current context.

USER CONTEXT:
- Available time: {available_minutes} minutes
- Current mood: {mood} (maps to: {preferred_work_type})
- Time of day: {time_of_day}

PENDING TASKS:
{tasks_json}

SCORING RULES:
1. Time Fit (30% weight):
   - Task fits with buffer: 1.0
   - Tight fit (within 10%): 0.5
   - Doesn't fit: 0.0 (EXCLUDE)

2. Mood Match (40% weight):
   - Perfect match: 1.0
   - Related type: 0.5
   - Opposite type: 0.0

3. Urgency (20% weight):
   - Score = urgency / 100

4. History (10% weight):
   - No rejections: 0.1
   - 1-2 rejections: 0.05
   - 3+ rejections: 0.0

MOOD MAPPING:
- "focused/sharp" → prefer focus work
- "creative/inspired" → prefer creative work
- "social/energetic" → prefer social work
- "tired/low energy" → prefer independent work

OUTPUT (JSON):
{
  "selected_task_id": "...",
  "score": 0.0,
  "reasoning": "brief explanation",
  "user_message": "conversational suggestion"
}

If no tasks fit, explain why and suggest alternatives.
```

### Mood to Work Type Affinity

```mermaid
flowchart LR
    subgraph Mood["User Mood"]
        Focused["Focused / Sharp"]
        Creative["Creative / Inspired"]
        Social["Social / Energetic"]
        Tired["Tired / Low Energy"]
    end

    subgraph Affinity["Work Type Affinity"]
        FocusHigh["focus: 1.0<br/>creative: 0.6<br/>social: 0.3<br/>independent: 0.4"]
        CreativeHigh["creative: 1.0<br/>focus: 0.5<br/>social: 0.4<br/>independent: 0.3"]
        SocialHigh["social: 1.0<br/>creative: 0.5<br/>independent: 0.6<br/>focus: 0.4"]
        TiredHigh["independent: 1.0<br/>social: 0.4<br/>creative: 0.3<br/>focus: 0.2"]
    end

    Focused --> FocusHigh
    Creative --> CreativeHigh
    Social --> SocialHigh
    Tired --> TiredHigh
```

### Selection Message Templates

```mermaid
flowchart TD
    subgraph Templates["Message Confidence"]
        High["Score > 0.8<br/>'Perfect timing - how about {task}?<br/>It matches your [time] and [mood].'"]
        Medium["Score 0.5-0.8<br/>'I'd suggest {task}.<br/>It's [urgency level] and fits your time.'"]
        Low["Score < 0.5<br/>'Best I can find is {task}.<br/>Not perfect, but might work?'"]
        None["No match<br/>'Nothing quite fits right now.<br/>Want to add something quick?'"]
    end
```

### Naming the Selected Task

`{task}` is a literal token, not a description to paraphrase. The module writes
it wherever `user_message` refers to the selected task; the application
substitutes the exact stored title before the message is sent.

Why this design: a suggestion that identifies the task only by attribute —
"this focus task", "this 30-minute one" — gives the user nothing to act on. The
attached page id is internal and never rendered to the user, so the title has to
appear in the message text itself. Code owns the substitution so the wording
cannot drift.

The bracketed slots that remain (`[time]`, `[mood]`, `[urgency level]`) are
prose the module writes itself.


---

See also:
- `docs/ai-prompts/shared.md` — base prompt, mood/confidence framing
- `docs/ai-prompts/rejection.md` — what happens when the user says no
