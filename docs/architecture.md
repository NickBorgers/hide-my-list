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
        end
        subgraph models["models/"]
            Task[task.go]
        end
    end

    subgraph Entry["cmd/server/"]
        Main[main.go]
    end

    Main --> Handlers
    Handlers --> AIClient
    Handlers --> NotionClient
    AIClient --> Prompts
    NotionClient --> Tasks
    Tasks --> Task
    AIClient --> Task
```

## Request Flow

```mermaid
sequenceDiagram
    participant User
    participant UI as Chat UI
    participant Server as Go Server
    participant Claude as Claude API
    participant Notion as Notion API

    User->>UI: Types message
    UI->>Server: POST /api/chat
    Server->>Claude: Determine intent
    Claude-->>Server: Intent + parameters

    alt Task Intake
        Server->>Claude: Extract task details
        Claude-->>Server: Labels + estimates
        Server->>Notion: Create task
        Notion-->>Server: Task ID
        Server-->>UI: Confirmation message
    else Task Selection
        Server->>Notion: Fetch pending tasks
        Notion-->>Server: Task list
        Server->>Claude: Select best match
        Claude-->>Server: Selected task + reasoning
        Server-->>UI: Task suggestion
    else Task Completion
        Server->>Notion: Update status
        Notion-->>Server: Success
        Server-->>UI: Celebration message
    else Task Rejection
        Server->>Notion: Update rejection notes
        Server->>Claude: Select alternative
        Claude-->>Server: New suggestion
        Server-->>UI: Alternative task
    end

    UI-->>User: Display response
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
        Select[Task Selection]
        Complete[Completion Handler]
        Reject[Rejection Handler]
    end

    subgraph Storage["Notion Database"]
        DB[(Tasks Table)]
    end

    subgraph Output["User Output"]
        Response[Chat Response]
    end

    Msg --> Intent
    Intent -->|"add task"| Intake
    Intent -->|"get task"| Select
    Intent -->|"done"| Complete
    Intent -->|"reject"| Reject

    Intake -->|Create| DB
    Select -->|Read| DB
    Complete -->|Update status| DB
    Reject -->|Update notes| DB

    Intake --> Response
    Select --> Response
    Complete --> Response
    Reject --> Response
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

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API authentication |
| `NOTION_API_KEY` | Notion integration token |
| `NOTION_DATABASE_ID` | Tasks database identifier |
| `PORT` | HTTP server port (default: 8080) |

## Security Considerations

- API keys stored in environment variables, never in code
- Single-user MVP (no authentication initially)
- CORS configured for local development
- Notion integration has minimal required permissions
- No sensitive data stored in conversation state
