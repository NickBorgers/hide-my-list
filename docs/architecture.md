# hide-my-list: System Architecture

## Overview

hide-my-list is an AI-powered task manager where users never directly view their task list. The system uses conversational AI to intake tasks, intelligently label them, and surface the right task at the right time based on user mood, available time, and task urgency.

## High-Level Architecture

```mermaid
flowchart TB
    subgraph Client["Web Browser"]
        UI[Vanilla JS Chat UI]
    end

    subgraph Server["Go Backend"]
        HTTP[HTTP Server<br/>stdlib net/http]
        API[API Handlers]
        Conv[Conversation<br/>State Manager]
    end

    subgraph External["External Services"]
        Claude[Anthropic Claude API]
        Notion[Notion API]
    end

    UI <-->|HTTP POST /api/chat| HTTP
    HTTP --> API
    API <--> Conv
    API <-->|Task labeling<br/>Task selection<br/>Intent parsing| Claude
    API <-->|CRUD operations| Notion
```

## Component Architecture

```mermaid
flowchart LR
    subgraph Frontend["web/"]
        HTML[index.html]
        JS[app.js]
        CSS[style.css]
    end

    subgraph Backend["internal/"]
        subgraph api["api/"]
            Handlers[handlers.go]
        end
        subgraph ai["ai/"]
            AIClient[client.go]
            Prompts[prompts.go]
        end
        subgraph notion["notion/"]
            NotionClient[client.go]
            Tasks[tasks.go]
            Preferences[preferences.go]
        end
        subgraph models["models/"]
            Task[task.go]
            UserPrefs[preferences.go]
        end
        subgraph preferences["preferences/"]
            PrefManager[manager.go]
            PrefContext[context.go]
        end
        subgraph rewards["rewards/"]
            RewardEngine[engine.go]
            RewardDelivery[delivery.go]
        end
    end

    subgraph Entry["cmd/server/"]
        Main[main.go]
    end

    subgraph Integrations["External Integrations"]
        HomeAudio[Home Audio<br/>Sonos/HomePod/Echo]
        SMS[SMS/Messaging<br/>Twilio/iMessage]
        MediaAPIs[Media APIs<br/>Giphy/Sora]
    end

    Main --> Handlers
    Handlers --> AIClient
    Handlers --> NotionClient
    Handlers --> RewardEngine
    Handlers --> PrefManager
    AIClient --> Prompts
    NotionClient --> Tasks
    NotionClient --> Preferences
    Tasks --> Task
    Preferences --> UserPrefs
    AIClient --> Task
    AIClient --> PrefContext
    PrefManager --> PrefContext
    PrefManager --> Preferences
    RewardEngine --> RewardDelivery
    RewardDelivery --> HomeAudio
    RewardDelivery --> SMS
    RewardDelivery --> MediaAPIs
```

## Request Flow

```mermaid
sequenceDiagram
    participant User
    participant UI as Chat UI
    participant Server as Go Server
    participant Prefs as Preference Manager
    participant Claude as Claude API
    participant Notion as Notion API
    participant HomeAudio as Home Audio
    participant SMS as SMS Service

    User->>UI: Types message
    UI->>Server: POST /api/chat
    Server->>Claude: Determine intent
    Claude-->>Server: Intent + parameters

    alt Task Intake
        Server->>Prefs: Get user preferences
        Prefs->>Notion: Fetch preferences
        Notion-->>Prefs: User preference data
        Prefs-->>Server: Preference context block
        Server->>Claude: Extract task details + preferences context
        Claude-->>Server: Labels + personalized breakdown
        alt Simple Task
            Server->>Notion: Create single task
        else Complex Task
            Server->>Notion: Create parent + sub-tasks (hidden)
        end
        Notion-->>Server: Task ID(s)
        Server-->>UI: Confirmation with personalized steps
    else Task Selection
        Server->>Notion: Fetch pending tasks (exclude has_subtasks)
        Notion-->>Server: Task list
        Server->>Claude: Select best match
        Claude-->>Server: Selected task + reasoning
        Server-->>UI: Task suggestion + time_estimate
    else Task Acceptance
        Server->>Notion: Update status to in_progress
        Server-->>UI: Confirmation + check_in_delay
        UI->>UI: Set check-in timer
    else Task Completion
        Server->>Notion: Update status
        alt Is Sub-task
            Server->>Notion: Check if all siblings complete
            Notion-->>Server: Sibling status
            opt All Complete
                Server->>Notion: Update parent to completed
            end
        end
        Notion-->>Server: Success
        Server->>Server: Trigger Reward Engine
        par Reward Delivery
            Server-->>UI: Emoji celebration + GIF
            Server->>HomeAudio: Play victory music
            Server->>SMS: Text significant other
        end
        UI->>UI: Clear check-in timer
        opt High Intensity Achievement
            Server-->>UI: Suggest fun outing
        end
    else Task Rejection
        Server->>Notion: Update rejection notes
        Server->>Claude: Select alternative
        Claude-->>Server: New suggestion
        Server-->>UI: Alternative task
    else Cannot Finish
        Server->>Claude: Ask what was accomplished
        Claude-->>Server: Progress question
        Server-->>UI: Progress question
        User->>UI: Describes progress
        UI->>Server: Progress response
        Server->>Claude: Analyze remaining work
        Claude-->>Server: Sub-task breakdown
        Server->>Notion: Update parent + create sub-tasks
        Notion-->>Server: Success
        Server-->>UI: Offer first remaining sub-task
    else Check-In (timer triggered)
        UI->>Server: POST /api/chat (CHECK_IN)
        Server->>Notion: Verify task still in_progress
        Server->>Claude: Generate follow-up
        Claude-->>Server: Check-in message
        Server-->>UI: "How's [task] going?"
    end

    UI-->>User: Display response
```

## Check-In Timer Flow

```mermaid
sequenceDiagram
    participant UI as Chat UI
    participant Timer as JS Timer
    participant Server as Go Server

    Note over UI,Server: User accepts task (time_estimate: 30 min)

    UI->>Timer: Set timer for 37.5 min (1.25x)
    Timer-->>UI: Timer running

    alt User completes before timer
        UI->>Server: "Done!"
        UI->>Timer: Clear timer
    else Timer fires
        Timer->>UI: Timer expired
        UI->>Server: CHECK_IN message
        Server-->>UI: "How's [task] going?"
    end
```

## Data Flow

```mermaid
flowchart TD
    subgraph Input["User Input"]
        Msg[Chat Message]
    end

    subgraph Processing["Server Processing"]
        Intent[Intent Detection]
        Intake[Task Intake]
        PrefLookup[Preference Lookup]
        Complexity[Complexity Evaluation]
        Breakdown[Personalized Breakdown]
        Select[Task Selection]
        Complete[Completion Handler]
        Reject[Rejection Handler]
        CannotFinish[Cannot Finish Handler]
    end

    subgraph Storage["Notion Database"]
        DB[(Tasks Table)]
        Prefs[(User Preferences)]
    end

    subgraph Output["User Output"]
        Response[Chat Response]
    end

    Msg --> Intent
    Intent -->|"add task"| Intake
    Intent -->|"get task"| Select
    Intent -->|"done"| Complete
    Intent -->|"reject"| Reject
    Intent -->|"cannot finish"| CannotFinish

    Intake --> PrefLookup
    PrefLookup -->|Get preferences| Prefs
    PrefLookup --> Complexity
    Complexity -->|Simple| DB
    Complexity -->|Complex| Breakdown
    Breakdown -->|Create parent + sub-tasks| DB

    Select -->|Read pending| DB
    Complete -->|Update status| DB
    Reject -->|Update notes| DB

    CannotFinish -->|Ask progress| Response
    CannotFinish --> PrefLookup
    PrefLookup --> Breakdown

    Intake --> Response
    Select --> Response
    Complete --> Response
    Reject --> Response
    Breakdown --> Response
```

## Deployment Architecture

```mermaid
flowchart TB
    subgraph Local["Local Development"]
        Dev[Go Binary<br/>localhost:8080]
        DevNotion[Notion Dev Database]
        DevClaude[Claude API<br/>Dev Key]
    end

    subgraph Production["Production"]
        Prod[Go Binary<br/>Docker Container]
        ProdNotion[Notion Prod Database]
        ProdClaude[Claude API<br/>Prod Key]
    end

    Dev <--> DevNotion
    Dev <--> DevClaude
    Prod <--> ProdNotion
    Prod <--> ProdClaude
```

## Technology Choices

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Backend | Go (stdlib) | Single binary, fast, no framework overhead |
| Frontend | Vanilla JS | No build step, fast iteration |
| Database | Notion | Zero setup, visual backup, rich API |
| AI | Claude API | Strong reasoning, structured output |
| Hosting | Docker | Simple deployment, portable |

## Environment Variables

### Core Services

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API authentication |
| `NOTION_API_KEY` | Notion integration token |
| `NOTION_DATABASE_ID` | Tasks database identifier |
| `NOTION_PREFERENCES_DB_ID` | User preferences database identifier |
| `PORT` | HTTP server port (default: 8080) |

### Reward System (Optional)

| Variable | Purpose |
|----------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio account for SMS notifications |
| `TWILIO_AUTH_TOKEN` | Twilio authentication token |
| `TWILIO_PHONE_NUMBER` | Sender phone number for SMS |
| `SONOS_API_KEY` | Sonos home audio integration |
| `HOME_ASSISTANT_URL` | Home Assistant endpoint for audio control |
| `HOME_ASSISTANT_TOKEN` | Home Assistant authentication |
| `GIPHY_API_KEY` | Giphy API for celebration GIFs |
| `SORA_API_KEY` | OpenAI Sora for AI-generated celebration videos |
| `OPENWEATHER_API_KEY` | Weather API for outing suggestions |

## Security Considerations

- API keys stored in environment variables, never in code
- Single-user MVP (no authentication initially)
- CORS configured for local development
- Notion integration has minimal required permissions
- No sensitive data stored in conversation state
