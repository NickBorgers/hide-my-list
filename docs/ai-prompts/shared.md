---
layout: default
title: AI Prompts
---

# AI Prompts & Interaction Design

## Overview

hide-my-list uses Claude API for all AI features: intent detection, task intake, label inference, task selection, rejection handling. Doc covers prompt architecture and strategies.

## Prompt Architecture

```mermaid
flowchart TB
    subgraph Base["Base System Prompt"]
        BP[Role + Personality + Constraints]
    end

    subgraph Modules["Specialized Modules"]
        Intent[Intent Detection]
        Intake[Task Intake]
        Select[Task Selection]
        Reject[Rejection Handling]
    end

    subgraph Context["Runtime Context"]
        User[User Profile]
        History[Conversation History]
        Tasks[Current Tasks]
    end

    Base --> Intent
    Base --> Intake
    Base --> Select
    Base --> Reject

    Context --> Intent
    Context --> Intake
    Context --> Select
    Context --> Reject
```

## Base System Prompt

```
You are hide-my-list, a friendly task management assistant with a unique philosophy:
users should never need to look at their task list. You handle everything.

PERSONALITY:
- Casual and brief - like texting a helpful friend
- Confident in suggestions - trust your algorithm
- Collaborative on rejections - never defensive
- Celebratory on completions - but not over the top

CONSTRAINTS:
- Never show the user their full task list
- Prefer inference over questions during task intake — ask only when truly needed
- Keep responses under 50 words unless explaining something complex
- Always be ready to add a task or suggest one
- User-visible replies must contain only user-facing content
- Never surface hidden reasoning, self-checks, tool-status narration, or internal implementation details

SHAME PREVENTION (MANDATORY — applies to every response):
Rejection and criticism can feel intensely personal for some people.
A single shame-triggering message can cause permanent disengagement.

- Never imply the user has failed, fallen short, or should have done better
- Never use "you didn't", "you should have", "you forgot", or "you failed"
- Frame ALL difficulties as information, not shortcomings:
  "Too big" → "Now we know this needs smaller pieces"
  "Can't finish" → "You made progress and now know what's left"
  "Rejected 3 tasks" → "Sometimes the brain isn't in task mode — that's useful info"
- Rejection is the user helping you suggest better — say so explicitly
- Celebrate effort and showing up, not just completion
- Always provide safe exit ramps without guilt or pressure
- If the user seems frustrated, offer a graceful exit: "Want to take a break?
  I'll be here when you're ready." Never push.
- Disconnect task performance from self-worth — tasks are external objects,
  not measures of the person

RESPONSE STYLE:
- No emojis unless user uses them first
- No formal greetings ("Hello!", "Thank you for...")
- Use contractions naturally
- Acknowledge briefly, then move forward
- Do not append meta commentary like "Note:" or internal self-assessments after the user-facing answer
```

## Visible Output Boundary

Everything the user sees should read like direct conversation, not system narration.

Rules:
- Never expose internal reasoning, chain-of-thought, hidden compliance checks, or tool-status narration.
- Multi-step flows must stay silent until the user-facing result is ready. Do not send interim messages while running tools, updating state, calculating scores, or preparing attachments.
- Never mention reminder infrastructure like cron jobs, polling, handoff files, Notion writes, tool calls, or whether something will trigger automatically unless the user explicitly asks.
- After a successful reminder create call, send only the reminder confirmation itself. No appended caveats, diagnostics, or self-evaluation.
- The "don't mention infrastructure" rule does NOT mean denying capability. The system does send scheduled reminders and check-ins; never tell the user the assistant cannot send those, is purely passive, or only responds when the user checks in. If a reminder did not arrive, acknowledge the miss without explaining internals.
- When a reminder reply is resolved from `recent_outbound` and becomes a reschedule, the user should still see only the new reminder confirmation. Do not mention prior reminder context, cleanup of old state, replaced reminder records, or cron replacement logic.
- During COMPLETE/reward handling, the only visible reward-phase content is the final celebration copy and optional image attachment described in `docs/reward-system.md`. A user turn that completes multiple tasks still gets one turn-scoped reward reply with at most one image. Never expose reward score calculations, streak math, Notion status updates, image-generation calls, or fallback diagnostics.
- If an internal distinction matters operationally, keep it internal unless the user explicitly asks for technical detail.


---

## Module 1: Intent Detection

```mermaid
flowchart TD
    Input([User Message]) --> Classify[Intent Classification]

    Classify --> ADD["ADD_TASK<br/>Adding new task"]
    Classify --> GET["GET_TASK<br/>Ready to work"]
    Classify --> DONE["COMPLETE<br/>Finished task"]
    Classify --> NOPE["REJECT<br/>Doesn't want this"]
    Classify --> CANT["CANNOT_FINISH<br/>Task too large"]
    Classify --> HELP["NEED_HELP<br/>Wants breakdown help"]
    Classify --> CHECKIN["CHECK_IN<br/>System follow-up"]
    Classify --> CHAT["CHAT<br/>General"]
```

### Intent Detection Prompt

```
Classify the user's intent from their message. Return ONLY the intent category.

Categories:
- ADD_TASK: User wants to add a new task (mentions something they need to do)
- GET_TASK: User wants something to work on (mentions time available, asks what to do)
- COMPLETE: User finished a task — the one they are on, or one they name (says done, finished, completed)
- REJECT: User doesn't want the suggested task (says no, not that one, something else)
- CANNOT_FINISH: User indicates current task is too large or overwhelming (too big, can't finish, overwhelming)
- NEED_HELP: User wants help breaking down or starting their current task (how do I start, what's next, I'm stuck, break this down)
- CHECK_IN: System-initiated follow-up (triggered by APScheduler `check_in_dispatcher`, not by a user message)
- CHAT: General conversation or questions

Prior conversation (last ~5 turns from conversation history):
{prior_conversation}

If the prior conversation shows an in-progress task discussion, treat short
follow-ups (deadlines, clarifications, pronouns like "it") as continuations of
that intent. For example, if the previous turn was the user describing a task
and the current message is "I need to do it by Friday", classify as ADD_TASK.

Message: "{user_message}"

Intent:
```

**Note:** CHECK_IN never inferred from user messages. Reserved system intent for scheduler-driven follow-up via APScheduler `check_in_dispatcher`. Normal user replies like "I'm back" still go through standard intent flow. Short replies like "I did it" after a just-sent reminder classify from the windowed `state["messages"]` history that `send_node` writes — the same `Prior conversation` block surfaced above.

### Cross-Session Reply Resolution

Intent classification uses the checkpointed conversation window in
`state["messages"]`; it does not query `recent_outbound` during routing.
After a message routes to COMPLETE, `complete_node` resolves the completion
target from three sources, in this order of authority:

1. **A task named in the message.** When the message carries words beyond the
   completion phrase itself, they are ranked against every open non-reminder
   task and a model call confirms which task the message reports as finished.
   Word overlap ranks that list; it does not decide who is on it. When nothing
   clears the ranking threshold, the whole open list goes to the model instead
   (capped at 40, ranked), so a message that paraphrases a task rather than
   quoting its title still reaches the model. A match at or above 0.90
   confidence outranks both context sources below, including an active task
   pointing at a different page.
2. **The newest unresolved `recent_outbound` row** for the peer where
   `awaiting_reply = true` and `expires_at > now()`.
3. **The checkpointed `active_task`**, when it has a parseable, unexpired
   `selected_at`.

Between the two context sources, the more recent wins. If the reminder row
wins, the node rewards the matched `notion_page_id`, skips the Notion status
write because reminder delivery already completes the reminder page, and marks
every live `recent_outbound` row for that peer and `notion_page_id`
`awaiting_reply = false` (`signal_timestamp` is the fallback when no page id is
available). If no confident target exists, the node asks which task the user
means instead of completing a checkpointed task by default.

A null match matters differently depending on which list produced it. Over the
ranked shortlist the model is rejecting tasks the message actually overlaps —
"done, now I need to call mom" against "Call mom" — so the node asks rather
than falling through to context. Over the widened whole-list fallback it means
only "could not tell", and context still resolves.

### Pending Clarification

The question is recorded in `state["pending_clarification"]`: its kind, when it
was asked, how many times it has been asked, and the options it named.
`classify_intent` owns that key's lifecycle, since it is the only node that
runs on every turn.

| Rule | Value |
|------|-------|
| Time-to-live from `asked_at` | 30 minutes |
| Intents treated as an answer | CHAT, COMPLETE (steered to COMPLETE) |
| Intents that drop the question | every other intent |
| Options named when the message reached candidates | up to 3 |
| Asks before the agent stops | 2 |

An expired timestamp, a malformed record, or a classified intent outside the
answer set clears the key rather than steering. Past the ask limit the node
sends a closing message, leaves the tasks open, and clears the key.

Options are named only when the message's own words put those candidates on
the list. A widened whole-list scan is ranked by scores that are all
effectively zero, so its top three are the first three open tasks rather than
a shortlist; naming them would present noise as a suggestion, and because a
named option can be answered by position, the user could accept one and
complete a task chosen at random. In that case the question stays open and
nothing is stored as an option — an option the user was never shown must never
become the referent of "the first one". Each ask is worded differently from the
one before it either way, because a question repeated verbatim is the failure
this path exists to prevent.

`complete_node` reads the same record for two things. The options it named lead
the candidate list on the answering turn, in the order they were named, so a
positional reply — "the first one", "the second" — resolves to the option it
points at; an option no longer open is dropped rather than resurrected from the
checkpoint. And the matching prompt switches framing: a standalone completion
must assert that a task is finished, while an answer to a clarification only
has to identify one, because the assertion was made on the previous turn.

Steering an answer back to the node that asked relaxes the framing but not the
threshold: when the answer names a task, the 0.90 confidence threshold and the
instruction to return no match when uncertain still apply. When the answer's
words do not identify a task, context sources (`recent_outbound`, `active_task`)
resolve as they would on a first-turn completion.

Other shorthand follow-up paths thread matched context as follows:

- ADD_TASK (reschedule): matched `recent_outbound.title` seeds the new reminder title in `docs/ai-prompts/intake.md` (see RESCHEDULE FROM RECENT OUTBOUND CONTEXT section); the user's time phrase is the only new input needed.
- REJECT (prior suggestion declined): matched `recent_outbound.title` populates REJECTED TASK in `docs/ai-prompts/rejection.md`; user message text (e.g. "not that one") is USER'S REASON. Clear or mark `awaiting_reply: false` on the matched entry after routing.

### Intent Detection Examples

| Message | Intent |
|---------|--------|
| "I need to call the dentist" | ADD_TASK |
| "Remind me to buy groceries" | ADD_TASK |
| "Remind me at 6pm to call Sarah" | ADD_TASK (with reminder) |
| "Ping me at 3pm CT to email Melanie" | ADD_TASK (with reminder) |
| "What should I do?" | GET_TASK |
| "I have 30 minutes" | GET_TASK |
| "Done!" | COMPLETE |
| "Finished that one" | COMPLETE |
| "Not that one" | REJECT |
| "Something else" | REJECT |
| "This is too big" | CANNOT_FINISH |
| "I can't finish this in one go" | CANNOT_FINISH |
| "This is overwhelming" | CANNOT_FINISH |
| "How do I start?" | NEED_HELP |
| "What's the first step?" | NEED_HELP |
| "I'm stuck" | NEED_HELP |
| "Break this down for me" | NEED_HELP |
| "What should I do first?" | NEED_HELP |
| "How does this work?" | CHAT |
| "Hello" | CHAT |
| "I did it" after a just-sent reminder | COMPLETE |
| "Tomorrow at 9am" after a just-sent reminder | ADD_TASK |


---

## User Preferences Context

When generating task breakdowns, user preferences assembled into context block injected into prompt. Enables personalized prep steps for success environment.

### Preference Context Block Format

```
USER_PREFERENCES_CONTEXT:
This user has the following preferences:

General:
- Preferred beverage: {preferred_beverage}
- Comfort spot: {comfort_spot}
- Transition ritual: {transition_ritual}

For {work_type} tasks:
- Environment: {work_type_prefs.environment}
- Prep steps: {work_type_prefs.prep_steps}
- Beverage: {work_type_prefs.beverage}

Task pattern preferences (if applicable):
- {matched_pattern}: {pattern_prefs}

Current context:
- Time of day: {time_of_day} ({time_prefs})
- Energy level: {energy_level} ({energy_prefs})

When generating sub-tasks, include personalized prep steps that align with these preferences.
The first 1-2 steps should focus on environment setup and mental preparation.
```

### Example Context Blocks

**For social task (phone call) in afternoon:**
```
USER_PREFERENCES_CONTEXT:
This user has the following preferences:

General:
- Preferred beverage: tea
- Comfort spot: cozy chair in the living room
- Transition ritual: 3 deep breaths

For social tasks:
- Environment: comfortable, quiet spot
- Prep steps: review context, set intention
- Beverage: tea

Task pattern preferences:
- phone_calls: find quiet room, review last interaction, prepare 2-3 topics

Current context:
- Time of day: afternoon (tea preferred, good for social tasks)
- Energy level: medium (standard rituals)

When generating sub-tasks, include personalized prep steps that align with these preferences.
The first 1-2 steps should focus on environment setup and mental preparation.
```

**For focus task (writing) in morning:**
```
USER_PREFERENCES_CONTEXT:
This user has the following preferences:

General:
- Preferred beverage: coffee
- Comfort spot: standing desk in the office
- Transition ritual: quick stretch

For focus tasks:
- Environment: quiet office, door closed
- Prep steps: put phone in another room, close email
- Beverage: coffee
- Music: lo-fi

Task pattern preferences:
- writing: 2 min free-write warmup, breaks every 25 min

Current context:
- Time of day: morning (coffee preferred, ideal for focus work)
- Energy level: high (minimal prep, dive in quickly)

When generating sub-tasks, include personalized prep steps that align with these preferences.
The first 1-2 steps should focus on environment setup and mental preparation.
```

### Preference Fallbacks

When user preferences not set, system uses sensible defaults:

| Work Type | Default Prep Steps |
|-----------|-------------------|
| focus | Find quiet spot, minimize distractions |
| creative | Find inspiring space, gather materials |
| social | Find quiet spot, review context |
| independent | Gather needed items, set up workspace |

---

## Structured Output Handling

### JSON Extraction Pattern

```mermaid
flowchart LR
    Response["AI Response with JSON"] --> Parse["Extract JSON block"]
    Parse --> Validate["Validate against schema"]
    Validate --> Use["Use structured data"]
    Validate --> Fallback["Fallback to text parsing"]
```

AI outputs JSON blocks that can be parsed:

```
AI Response format:
"Here's a casual message for the user."

```json
{
  "action": "...",
  "data": {...}
}
```
```

### Validation Rules

| Field | Validation |
|-------|------------|
| work_type | Must be: focus, creative, social, independent |
| urgency | Integer 0-100 |
| time_estimate_minutes | Positive integer |
| confidence scores | Float 0.0-1.0 |
| task_id | Must exist in Notion database |

---

## Error Handling

```mermaid
flowchart TD
    subgraph Errors["Error Types"]
        API["API Error<br/>(Claude unavailable)"]
        Parse["Parse Error<br/>(Invalid JSON)"]
        Empty["Empty Response"]
        Hallucination["Hallucinated Data<br/>(Non-existent task ID)"]
    end

    subgraph Recovery["Recovery Actions"]
        Retry["Retry with backoff"]
        Fallback["Use text parsing"]
        Default["Use default values"]
        Ignore["Ignore and re-query"]
    end

    API --> Retry
    Parse --> Fallback
    Empty --> Default
    Hallucination --> Ignore
```

### Fallback Behaviors

| Error | Fallback |
|-------|----------|
| Intent unclear | Ask user to clarify |
| Confidence all low | Use defaults, mention uncertainty |
| No matching task | Explain constraints, offer alternatives |
| API failure | "Having trouble thinking - try again?" |

---

## Prompt Versioning

```mermaid
flowchart LR
    subgraph Versions["Prompt Versions"]
        V1["v1.0<br/>Initial release"]
        V2["v1.1<br/>Improved intent"]
        V3["v2.0<br/>Better scoring"]
    end

    V1 --> V2 --> V3

    subgraph Tracking["Version Tracking"]
        Tasks["Tasks store prompt version"]
        Metrics["Track success by version"]
        Rollback["Enable rollback if needed"]
    end
```

Each task stores prompt version used for creation, enabling:
- A/B testing of prompt changes
- Performance comparison between versions
- Rollback if new prompts perform worse

---

## Conversation State Management

```mermaid
stateDiagram-v2
    [*] --> Idle

    Idle --> Intake: ADD_TASK intent
    Idle --> Selection: GET_TASK intent

    Intake --> Idle: Task saved (after inference or up to 3 questions)

    Selection --> Active: Task accepted + initiation reward
    Selection --> Selection: Task rejected
    Selection --> Idle: No suitable task

    Active --> Active: First sub-step done + reward
    Active --> Idle: Task completed + celebration
    Active --> Selection: Task abandoned
    Active --> CheckingIn: Timer expires
    Idle --> Active: Resume detected (in_progress task + gap ≥ 15 min)

    CheckingIn --> Active: Still working
    CheckingIn --> Idle: Task completed
    CheckingIn --> Selection: Task abandoned
```

### State Data

| State | Data Stored |
|-------|-------------|
| Idle | None |
| Intake | Partial task data, conversation history, clarification_count |
| Selection | Current task context |
| Active | Active task ID, start time, check-in count |
| CheckingIn | Active task ID, elapsed time, check-in count |

---

## Example Complete Flow

```mermaid
sequenceDiagram
    participant U as User
    participant I as Intent Module
    participant T as Task Module
    participant S as Selection Module
    participant R as Rejection Module

    U->>I: "I need to finish the report"
    I->>T: ADD_TASK intent
    T->>U: "Got it — focus work, ~2 hours, moderate priority. First step: outline the key sections."

    Note over U,T: Vague task example (clarifying question)
    U->>I: "Handle that thing"
    I->>T: ADD_TASK intent
    T->>U: "Which thing are you thinking of?"
    U->>T: "The email to the team about the offsite"
    T->>U: "Got it — social, ~15 min, moderate priority. Steps: 1) Draft email, 2) Review, 3) Send."

    U->>I: "I have 30 minutes, feeling tired"
    I->>S: GET_TASK intent
    S->>U: "How about organizing your files? Light work, 20 min."
    U->>R: "Not that one"
    R->>U: "What's steering you away?"
    U->>R: "Need something more engaging"
    R->>S: Re-score with "engaging" preference
    S->>U: "Try replying to that email from Jake? Social, quick."
    U->>S: "Sure"
    S->>U: "It's yours. Let me know when done!"
