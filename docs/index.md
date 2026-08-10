---
layout: default
title: Home
---

# hide-my-list Documentation

An AI-powered task manager where users never directly view their task list. The system uses conversational AI to intake tasks, intelligently label them, and surface the right task at the right time.

## Documentation

- [Architecture](architecture.md) - System architecture: Python + LangGraph container topology, reminder outbox, scheduled jobs
- [Agentic Pipeline Learnings](agentic-pipeline-learnings.md) - Prescriptive lessons from the agentic review and CI pipeline (§1.18 documents the two-lens security reviewer orchestrator: vendored breadth prompt + repo-specific narrow prompt + deterministic merger that caps + dedupes findings)
- Setup/config specs:
  - [Model Tiers](../setup/model-tiers.json) - Repo metadata mapping expensive, medium, and cheap model tiers used by validation and app startup
- AI Prompts (per-intent):
  - [Shared](ai-prompts/shared.md) - Base prompt, intent dispatch, user preferences context, output handling (entry point)
  - [Intake](ai-prompts/intake.md) - Task intake: inference rules, sub-task generation, reminder detection
  - [Selection](ai-prompts/selection.md) - Task selection: scoring weights, mood mapping
  - [Rejection](ai-prompts/rejection.md) - Shame-safe rejection responses, escalation flow
  - [Cannot Finish](ai-prompts/cannot-finish.md) - Progress gathering, sub-task creation
  - [Check-In](ai-prompts/check-in.md) - Timing, shame-safe templates
  - [Breakdown](ai-prompts/breakdown.md) - Confidence detection, response levels
- [Notion Schema](notion-schema.md) - Database schema and data model
- [Task Lifecycle](task-lifecycle.md) - Task states and transitions
- [User Interactions](user-interactions.md) - User interaction patterns
- [User Preferences](user-preferences.md) - Personalization behavior spec
- [Reward System](reward-system.md) - Multi-channel reward and celebration system

## Python Rewrite

- [LangGraph Semantics](python-rewrite/langgraph-semantics.md) - Durability spike findings: per-peer isolation, restart semantics, worker-to-graph state pattern, schema migration behavior
- [Reward Deferred Features](python-rewrite/reward-deferred.md) - Features deferred from v1 reward subsystem (audio rewards, outing suggestions, video compilation)
- [Test Rig Architecture](python-rewrite/test-rig.md) - Authoritative spec for the behavior + LLM-swap test rig: layer architecture, bug-class catalog, fixture format, discipline rules
- [LLM Observability](python-rewrite/llm-observability.md) - Token + latency logging event schema (structlog → Gravwell), perf harness usage, model comparison recipe

## Design

- [ADHD Priorities](../design/adhd-priorities.md) - Core design principles grounded in ADHD research

## CI / Infrastructure

Per-file inventory of all CI and infrastructure files lives in [DEV-AGENTS.md](../DEV-AGENTS.md) (Infrastructure & CI Files section). Key scheduled workflows:

- `update-signal-cli.yml` — Mondays 10:00 UTC + `workflow_dispatch`; resolves `bbernhard/signal-cli-rest-api:latest` digest from the registry and opens a refresh PR when it differs from the digest pinned in `docker/compose.yaml`
- `update-ai-clis.yml` — refreshes Claude Code and Codex CLI pins weekly and re-vendors the upstream security audit prompt
- `nightly-evals.yml` — cron 09:00 UTC; runs LLM behavior evals against current model tiers on the homelab runner
- `model-swap.yml` — `workflow_dispatch` only; side-by-side baseline vs. candidate model comparison before tier changes

Published container images (both push from `main` only, never from PR branches):

- `ghcr.io/nickborgersprobably/hide-my-list` — runtime app image, built from `docker/Dockerfile` by `app-image.yml`
- `ghcr.io/nickborgersprobably/hide-my-list-ci` — CI agent image, built from `.github/ci/Dockerfile` by `ci-image.yml`
