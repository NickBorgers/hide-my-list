"""Synthetic prompts for the LLM perf harness.

These prompts exercise different message shapes (tiny, short conversational,
medium structured-output, longer with JSON shape). All content is placeholder
— no real user data, task titles, or personal information.

Prompts are (system_message, human_message) tuples so they can be passed
directly to LangChain message constructors.
"""
from __future__ import annotations

from typing import NamedTuple


class Prompt(NamedTuple):
    """A synthetic perf prompt: label, system content, human content."""
    label: str
    system: str
    human: str


SYNTHETIC_PROMPTS: list[Prompt] = [
    # Tiny — minimal input/output
    Prompt(
        label="tiny_hello",
        system="You are a helpful assistant. Reply in one word.",
        human="Hello.",
    ),
    Prompt(
        label="tiny_ack",
        system="You are a helpful assistant. Reply with 'OK'.",
        human="Acknowledge.",
    ),

    # Short conversational — typical chat workload
    Prompt(
        label="short_chat_how_are_you",
        system=(
            "You are a friendly task management assistant. "
            "Keep replies under 2 sentences."
        ),
        human="How are you doing today?",
    ),
    Prompt(
        label="short_chat_task_update",
        system=(
            "You are a friendly task management assistant. "
            "Keep replies under 2 sentences."
        ),
        human="I just finished a task. What should I do next?",
    ),
    Prompt(
        label="short_intent_classify",
        system=(
            "Classify this message into exactly one of: ADD_TASK, GET_TASK, "
            "COMPLETE, REJECT, CANNOT_FINISH, NEED_HELP, CHAT. "
            "Reply with the label only."
        ),
        human="I need to call the dentist.",
    ),

    # Medium structured-output — typical intake workload
    Prompt(
        label="medium_intake_json",
        system=(
            "You are a task intake assistant. Parse the user's task and respond "
            "with a JSON object containing: "
            '{"action": "save", "title": str, "work_type": str, '
            '"urgency": int (1-100), "time_estimate_minutes": int, '
            '"energy_required": str, "is_reminder": false, "remind_at": null, '
            '"use_hidden_subtasks": false, "sub_tasks": [], '
            '"inline_steps": str, "confirmation_message": str}.'
        ),
        human="I need to write a report for the placeholder project by end of week.",
    ),
    Prompt(
        label="medium_selection_json",
        system=(
            "You are a task selection assistant. Choose the best task from the "
            "list and respond with JSON: "
            '{"selected_task_id": str, "user_message": str, "rationale": str}. '
            "Tasks: "
            '[{"id": "task-001", "title": "Placeholder task A", '
            '"work_type": "focus", "urgency": 70, "time_estimate": 30, '
            '"energy_required": "Medium", "rejection_count": 0}, '
            '{"id": "task-002", "title": "Placeholder task B", '
            '"work_type": "creative", "urgency": 40, "time_estimate": 60, '
            '"energy_required": "Low", "rejection_count": 1}]'
        ),
        human="I have 30 minutes and feel focused. What should I work on?",
    ),
    Prompt(
        label="medium_shame_safe_rejection",
        system=(
            "You are a shame-safe task rejection handler. "
            "The user rejected a placeholder task. "
            "Respond warmly, validate the rejection as useful signal, "
            "and suggest a next step. Keep response under 3 sentences."
        ),
        human="That one's not right for me today.",
    ),

    # Longer with JSON shape — high context load
    Prompt(
        label="long_cannot_finish",
        system=(
            "You are a shame-safe breakdown assistant for an ADHD-informed task manager. "
            "The user says they cannot finish their task. "
            "TASK: Placeholder task requiring sustained attention (~90 minutes). "
            "CONTEXT: User has spent 45 minutes and made partial progress. "
            "RULES: Never imply failure. Frame progress as information. "
            "Lead with what they accomplished. "
            "Respond with JSON: "
            '{"phase": "ask_progress", "progress_question": str}.'
        ),
        human=(
            "I can't finish this. I got started but it's taking way longer "
            "than I thought and I'm running out of energy."
        ),
    ),
    Prompt(
        label="long_need_help_micro_action",
        system=(
            "You are a breakdown assistance coach for an ADHD-informed task manager. "
            "The user needs help starting a task. "
            "TASK: Placeholder complex research task with multiple components. "
            "SUB-STEPS: 1. Gather sources. 2. Outline key points. "
            "3. Draft introduction. 4. Review and edit. "
            "USER SIGNAL: 'stuck' — give a micro-action. "
            "Respond with JSON: "
            '{"detected_confidence": "stuck", "response_level": "micro_action", '
            '"immediate_action": str, "user_message": str, "encouragement": str}.'
        ),
        human=(
            "I don't know where to start. The whole thing feels overwhelming "
            "and I've been staring at it for 20 minutes."
        ),
    ),

    # Additional shapes for tail latency coverage
    Prompt(
        label="short_check_in",
        system=(
            "Generate a shame-safe, friendly check-in message for a user "
            "who accepted a task 35 minutes ago. The task estimate was 30 minutes. "
            "Respond with JSON: {\"check_in_message\": str}. "
            "Keep it warm and curious, not managerial."
        ),
        human="Generate a check-in message.",
    ),
    Prompt(
        label="medium_reward_text",
        system=(
            "Generate a brief, celebratory completion message for a user who "
            "finished a placeholder task. Intensity: high. "
            "Use 1-2 sentences maximum. Include appropriate emoji."
        ),
        human="The user completed their task!",
    ),
    Prompt(
        label="tiny_classify_complete",
        system=(
            "Classify this message into exactly one of: ADD_TASK, GET_TASK, "
            "COMPLETE, REJECT, CANNOT_FINISH, NEED_HELP, CHAT. "
            "Reply with the label only."
        ),
        human="Done!",
    ),
]
