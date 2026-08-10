# hide-my-list

An ADHD-informed task manager where you never see your task list.

## What is this?

hide-my-list is a conversational AI agent that manages your tasks for you. You tell it what you need to do, and it handles the rest — breaking tasks down, tracking them, and surfacing the right one when you're ready to work. You never look at a list. You never feel overwhelmed by a wall of undone items.

The core insight: **for people with ADHD, seeing a long task list isn't motivating — it's paralyzing.** Traditional task managers make this worse. hide-my-list takes a different approach.

## How it works

- **Talk to it** — describe what you need to do in natural language
- **It labels and stores** — work type, urgency, time estimate, energy required (in Notion)
- **It picks for you** — when you have time, it selects a task based on your current energy and mood
- **It breaks things down** — vague tasks get personalized, concrete sub-steps
- **It celebrates wins** — completion triggers immediate positive reinforcement
- **You never see the list** — that's the whole point

## Architecture

This is a **Python + LangGraph application** backed by **Notion** (task storage) and **Postgres** (conversation state, reminder outbox, scheduled jobs). It runs as a Docker Compose stack.

- **App**: Python 3.12, LangGraph, APScheduler
- **Storage**: Postgres (checkpointer, reminder outbox, scheduler) + Notion database
- **Messaging**: Signal via signal-cli bridge (infra-provided)
- **Review Pipeline**: GitHub Actions with multi-agent Codex review

See [docs/architecture.md](docs/architecture.md) for the full system design.

## Quick start

```bash
git clone https://github.com/NickBorgersProbably/hide-my-list.git
cd hide-my-list

# Install the repo-managed git hooks for this worktree
bash .githooks/install-hooks.sh

# Create .env from .env.template and fill in the values you need
cp .env.template .env

# Start the stack
docker compose up -d
docker compose logs -f app
```

## Git hooks

Install the repo hooks in every worktree you plan to commit from:

```bash
bash .githooks/install-hooks.sh
```

`core.hooksPath` is stored per worktree, so re-run that after each `git worktree add`.
`pre-commit` handles fast staged-file checks, and `pre-push` reruns the
deterministic CI-equivalent checks for changed scripts, docs, and workflow-related
paths so those failures are caught locally before GitHub is the first place they fail.

## Research-informed design

Every feature is evaluated against ADHD clinical research:
- Executive function support (Barkley model)
- Emotional regulation (Hallowell-Ratey framework)
- Time perception and time blindness research
- Motivation and reward systems (variable ratio reinforcement)
- Cognitive load management

The CI pipeline includes a **psychological research evidence reviewer** that validates changes against these frameworks.

## Documentation

- [Architecture](docs/architecture.md)
- [AI Prompts](docs/ai-prompts/shared.md) (entry point; per-intent prompts live in `docs/ai-prompts/`)
- [Task Lifecycle](docs/task-lifecycle.md)
- [Notion Schema](docs/notion-schema.md)
- [User Interactions](docs/user-interactions.md)
- [User Preferences](docs/user-preferences.md)
- [Reward System](docs/reward-system.md)
- [ADHD Design Priorities](design/adhd-priorities.md)
- [Security Architecture](SECURITY.md)

## License

MIT — see [LICENSE](LICENSE)
