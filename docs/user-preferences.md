# User Preferences & Comfort System

## Overview

The user preferences system tracks individual user comforts, rituals, and preferences that help create an environment for success when approaching different types of tasks. Rather than generic task breakdowns, the system generates personalized steps that acknowledge what helps each user perform their best.

## Core Philosophy

```mermaid
mindmap
  root((Environment<br/>for Success))
    Physical Comfort
      Hot beverages
      Comfortable seating
      Ambient lighting
      Temperature
    Mental Preparation
      Rituals before tasks
      Calming activities
      Focus techniques
      Transition routines
    Sensory Preferences
      Background music
      Quiet spaces
      Natural light
      Aromatherapy
    Social Comforts
      Check-in with partner
      Brief walk
      Pet interaction
      Deep breaths
```

**Key Insight:** Users are more likely to start (and complete) tasks when the environment feels supportive. A personalized "prep step" like "Make a cup of tea" can reduce the activation energy needed to begin.

---

## Preference Architecture

```mermaid
flowchart TB
    subgraph Storage["Preference Storage"]
        General[General Preferences]
        WorkType[Work Type Specific]
        TaskPattern[Task Pattern Specific]
    end

    subgraph Context["Context Factors"]
        TimeOfDay[Time of Day]
        Energy[Energy Level]
        Mood[Current Mood]
    end

    subgraph Application["Applied To"]
        Breakdown[Task Breakdown]
        Prep[Prep Steps]
        Environment[Environment Setup]
    end

    Storage --> Context
    Context --> Application
```

---

## Preference Categories

### 1. General Preferences

Universal comforts that apply across all task types.

| Preference | Example Values | Usage |
|------------|----------------|-------|
| preferred_beverage | tea, coffee, water, none | Suggested before longer tasks |
| comfort_spot | cozy chair, standing desk, patio | Suggested for focus/social tasks |
| transition_ritual | stretch, deep breaths, brief walk | Suggested between tasks |
| focus_music | lo-fi, classical, silence | Suggested during focus work |
| break_activity | walk, snack, pet time | Suggested after completions |

### 2. Work Type Preferences

Specific preferences tied to the four work types.

```mermaid
flowchart TD
    subgraph Focus["Focus Work Preferences"]
        F1[phone_away: true]
        F2[beverage: coffee]
        F3[environment: quiet office]
        F4[prep_ritual: 2 min meditation]
    end

    subgraph Creative["Creative Work Preferences"]
        C1[music: ambient]
        C2[tools: notebook first]
        C3[environment: natural light]
        C4[prep_ritual: brief walk]
    end

    subgraph Social["Social Work Preferences"]
        S1[beverage: tea]
        S2[environment: comfortable seat]
        S3[prep_ritual: review notes]
        S4[follow_up: note key points]
    end

    subgraph Independent["Independent Work Preferences"]
        I1[music: podcast]
        I2[environment: anywhere]
        I3[batching: group similar tasks]
        I4[reward: treat after completion]
    end
```

### 3. Task Pattern Preferences

Preferences for specific recurring task patterns.

| Pattern | Preference | Example |
|---------|------------|---------|
| phone_calls | pre_call_ritual | "Review contact notes, prepare talking points" |
| phone_calls | environment | "Quiet room, pacing allowed" |
| writing | warmup | "Free-write for 2 min first" |
| email_batch | setup | "Close other tabs, set timer" |
| meetings | prep | "Review agenda, prepare 1 question" |
| exercise | motivation | "Put on workout playlist" |

---

## Preference Schema

### User Preferences Table (Notion or Config)

```mermaid
erDiagram
    USER_PREFERENCES {
        string user_id PK "User identifier"
        string preferred_beverage "tea|coffee|water|none"
        string comfort_spot "Description of favorite spot"
        string transition_ritual "Between-task ritual"
        string focus_music_preference "lo-fi|classical|silence|none"
        string break_activity "Preferred break activity"
        json work_type_prefs "Work type specific JSON"
        json task_pattern_prefs "Pattern specific JSON"
        json time_of_day_prefs "Morning/afternoon/evening"
        json energy_level_prefs "High/medium/low energy"
    }
```

### Work Type Preferences JSON Structure

```json
{
  "focus": {
    "beverage": "coffee",
    "environment": "quiet office with door closed",
    "prep_steps": ["put phone in another room", "close email tabs"],
    "music": "lo-fi beats",
    "ideal_duration": "45-90 min blocks"
  },
  "creative": {
    "beverage": "tea",
    "environment": "natural light, open space",
    "prep_steps": ["take a brief walk", "grab notebook"],
    "music": "ambient or silence",
    "tools": "start with paper before digital"
  },
  "social": {
    "beverage": "tea",
    "environment": "comfortable, quiet spot",
    "prep_steps": ["review context/notes", "set intention"],
    "follow_up": "note key takeaways after"
  },
  "independent": {
    "beverage": "water",
    "environment": "anywhere comfortable",
    "batching": true,
    "music": "podcast or music",
    "reward": "small treat after batch complete"
  }
}
```

### Task Pattern Preferences JSON Structure

```json
{
  "phone_calls": {
    "prep_steps": ["find quiet room", "review last interaction", "prepare 2-3 topics"],
    "environment": "comfortable seat, room to pace",
    "beverage": "tea",
    "follow_up": "send follow-up text/email if needed"
  },
  "writing": {
    "warmup": "2 min free-write",
    "environment": "quiet, minimal distractions",
    "tools": "outline first, then draft",
    "breaks": "every 25 min"
  },
  "email_batch": {
    "setup": ["close other tabs", "set 20 min timer"],
    "approach": "quick responses first, then complex",
    "environment": "desk, focused"
  }
}
```

---

## Preference Learning

The system learns preferences through explicit input and implicit observation.

### Explicit Learning

```mermaid
flowchart TD
    subgraph Prompts["Learning Prompts"]
        Onboard["Onboarding: 'What helps you focus?'"]
        PostTask["Post-task: 'Did that setup work well?'"]
        Periodic["Periodic: 'Any new rituals working for you?'"]
    end

    subgraph Storage["Preference Storage"]
        Save[Save to user preferences]
    end

    Prompts --> Storage
```

**Onboarding Questions:**

| Question | Maps To |
|----------|---------|
| "Do you have a favorite drink while working?" | preferred_beverage |
| "Where do you do your best thinking?" | comfort_spot |
| "What helps you transition between tasks?" | transition_ritual |
| "Music or silence while working?" | focus_music_preference |

### Implicit Learning

```mermaid
flowchart LR
    subgraph Signals["Observed Signals"]
        S1["Task completed quickly after tea mention"]
        S2["Focus tasks always at standing desk"]
        S3["Social tasks completed faster in morning"]
    end

    subgraph Learning["Pattern Detection"]
        L1[Associate beverage with task success]
        L2[Link location to work type]
        L3[Map time-of-day to performance]
    end

    subgraph Apply["Applied Preferences"]
        A1[Suggest tea before similar tasks]
        A2[Recommend standing desk for focus]
        A3[Schedule social tasks AM when possible]
    end

    Signals --> Learning --> Apply
```

---

## Preference Injection

### Task Breakdown Enhancement

When generating task breakdowns, user preferences are injected as context for the LLM.

```mermaid
flowchart TD
    subgraph Input["Breakdown Inputs"]
        Task[Task to break down]
        WorkType[Work Type]
        UserPrefs[User Preferences]
    end

    subgraph Context["Context Assembly"]
        BasePrompt[Base breakdown prompt]
        PrefContext[Preference context block]
        Combined[Combined prompt]
    end

    subgraph Output["Personalized Breakdown"]
        PrepSteps[Personalized prep steps]
        ActionSteps[Core task actions]
        FollowUp[Follow-up steps]
    end

    Task --> BasePrompt
    WorkType --> PrefContext
    UserPrefs --> PrefContext
    BasePrompt --> Combined
    PrefContext --> Combined
    Combined --> Output
```

### Context Block Format

The preference context is added to LLM prompts when generating breakdowns:

```
USER CONTEXT:
This user has the following preferences for {work_type} tasks:
- Environment: {environment_preference}
- Beverage: {beverage_preference}
- Prep ritual: {prep_ritual}
- Additional notes: {any_relevant_patterns}

When generating sub-tasks, include personalized prep steps that align with these preferences.
The first 1-2 steps should focus on environment setup and mental preparation.
```

### Example: Generic vs. Personalized Breakdown

**Generic breakdown for "Call mom":**
```
1. Find quiet spot
2. Make call
3. Note follow-ups
```

**Personalized breakdown for user who drinks tea and likes comfortable spots:**
```
1. Make a cup of tea
2. Settle into the cozy chair in the living room
3. Make the call
4. Note any follow-ups or commitments
```

---

## Time & Energy Context

Preferences can vary by time of day and energy level.

### Time-of-Day Preferences

```mermaid
flowchart LR
    subgraph Morning["Morning (6am-12pm)"]
        M1[Higher energy tolerance]
        M2[Coffee preferred]
        M3[Best for focus work]
    end

    subgraph Afternoon["Afternoon (12pm-5pm)"]
        A1[Post-lunch dip considered]
        A2[Tea or water]
        A3[Good for social/creative]
    end

    subgraph Evening["Evening (5pm-10pm)"]
        E1[Lower energy tolerance]
        E2[Herbal tea or water]
        E3[Best for independent/light]
    end
```

### Energy-Based Adjustments

| Energy Level | Prep Step Adjustments |
|--------------|----------------------|
| High | Minimal prep, dive in quickly |
| Medium | Standard prep rituals |
| Low | Extended prep, comfort-focused, smaller first steps |

---

## Implementation Flow

### Preference Retrieval

```mermaid
sequenceDiagram
    participant AI as AI Assistant
    participant Prefs as Preference Store
    participant LLM as Claude API

    Note over AI: Task breakdown requested

    AI->>Prefs: Get user preferences
    Prefs-->>AI: User preference object

    AI->>AI: Determine work type
    AI->>AI: Get time of day
    AI->>AI: Get user energy level

    AI->>AI: Assemble preference context

    AI->>LLM: Breakdown prompt + preference context
    LLM-->>AI: Personalized breakdown

    AI->>AI: Present to user
```

### Breakdown Generation with Preferences

```mermaid
flowchart TD
    subgraph Request["Breakdown Request"]
        Task["Task: Call mom"]
        Type["Type: social"]
        Time["Time: afternoon"]
        Energy["Energy: medium"]
    end

    subgraph Lookup["Preference Lookup"]
        General["General: tea, cozy chair"]
        Social["Social: quiet spot, prep context"]
        Pattern["Phone calls: review last chat"]
    end

    subgraph Generate["Generate Breakdown"]
        Combine[Combine all relevant prefs]
        Inject[Inject into LLM prompt]
        Create[Create personalized steps]
    end

    subgraph Output["Personalized Steps"]
        Step1["1. Make a cup of tea"]
        Step2["2. Settle into the cozy chair"]
        Step3["3. Glance at last text from mom"]
        Step4["4. Make the call"]
        Step5["5. Note any follow-ups"]
    end

    Request --> Lookup
    Lookup --> Generate
    Generate --> Output
```

---

## User Experience

### Learning Preferences Naturally

The system learns preferences through natural conversation:

```
User: "I need to call the dentist"
AI: "Added - social, ~10 min. Quick question: what helps you gear up for phone calls? Some folks like a drink, others a quiet spot."
User: "I usually make tea first"
AI: "Good to know! I'll remember that. ☕"
```

### Applying Preferences

```
User: "Ready for a task"
AI: "How about calling your dentist? Here's the plan:
     1. Put the kettle on for tea
     2. Find a quiet spot - maybe the living room?
     3. Make the call
     4. Note any appointment times

     Start with step 1 when you're ready!"
```

### Confirming Preference Relevance

```
User: "Done with the call!"
AI: "Nice work! Did having tea help, or should I skip that next time?"
User: "Definitely helps"
AI: "Noted - tea before calls it is! 🍵"
```

---

## Privacy & Control

### User Control

| Control | Description |
|---------|-------------|
| View preferences | User can see all stored preferences |
| Edit preferences | User can modify any preference |
| Delete preferences | User can clear all learned preferences |
| Pause learning | User can disable implicit learning |
| Opt-out of prep steps | User can request "just the task" |

### Data Minimization

- Preferences stored only for personalization
- No sensitive personal data in preferences
- Preferences never shared externally
- Learning limited to task-relevant patterns

---

## Future Enhancements

### Environmental Integration

- Connect to smart home for automatic environment setup
- Dim lights for focus work
- Play preferred music automatically
- Adjust thermostat based on task type

### Partner Preferences

- Learn what helps when working alongside partner
- Coordinate quiet hours
- Shared rituals for joint tasks

### Seasonal Adjustments

- Adjust beverage preferences by season
- Outdoor suggestions in good weather
- Cozy indoor alternatives in winter
