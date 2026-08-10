"""Shared `{task}` token rendering for user-visible message bodies.

Prompts that reference a specific Notion task write the literal token `{task}`
rather than authoring the title themselves. The application owns the
substitution so the user-visible body always names the exact selected task.

Why this design: models treat a bracketed placeholder (`[task]`) as an
instruction to paraphrase, producing bodies like "how about this focus task?"
that attach a valid page id but give the user nothing actionable. Code-side
substitution removes the model's opportunity to drift.

`send_node` applies this to every draft carrying `notion_page_title`, so the
invariant holds for any node — including ones added later.
"""
from __future__ import annotations

TASK_TOKEN = "{task}"


def render_task_token(user_message: str, *, title: str | None) -> str:
    """Return `user_message` with the task named.

    Replaces `{task}` with `title`. If the message references a task but omits
    the token, appends a deterministic sentence naming it. A falsy `title`
    leaves the message untouched.
    """
    if not title:
        return user_message

    if TASK_TOKEN in user_message:
        return user_message.replace(TASK_TOKEN, title)

    if title in user_message:
        return user_message

    stripped = user_message.rstrip()
    suffix = f" The task is: {title}."
    return f"{stripped}{suffix}" if stripped else f"The task is: {title}."
