"""Jinja2-based prompt template loader with caching.

Templates live in app/prompts/*.md.j2. They are rendered with State context
(or a subset thereof) to produce the final prompt strings for each node.

Caching: templates are loaded from disk once and cached by filename. The
rendered output is NOT cached — rendering is cheap and context varies per turn.

Security: Templates are loaded from the app/prompts/ directory only. No
user-supplied template paths are accepted.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    select_autoescape,
)

_PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def _get_env() -> Environment:
    """Build and cache the Jinja2 environment."""
    return Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=select_autoescape([]),  # Plain text prompts, no HTML escaping
        undefined=StrictUndefined,         # Raise on undefined variables
        keep_trailing_newline=True,
    )


def render(template_name: str, context: dict[str, Any] | None = None) -> str:
    """Render a prompt template from app/prompts/.

    Args:
        template_name: Filename relative to app/prompts/ (e.g. 'shared.md.j2').
        context: Dict of variables to inject into the template.
                 If None, renders with an empty context (for parity tests).

    Returns:
        Rendered string with all Jinja2 placeholders substituted.

    Raises:
        jinja2.TemplateNotFound: If template_name does not exist.
        jinja2.UndefinedError: If a template variable is not in context
            and StrictUndefined is active.
    """
    env = _get_env()
    template = env.get_template(template_name)
    return template.render(context or {})


def render_with_defaults(
    template_name: str,
    context: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> str:
    """Render a template, filling missing keys from defaults.

    Useful when rendering prompts with partial State (e.g., missing optional fields).
    Defaults are applied before rendering — they do not override explicitly-provided context.

    Args:
        template_name: Filename relative to app/prompts/.
        context: Primary rendering context.
        defaults: Fallback values for keys absent in context.

    Returns:
        Rendered string.
    """
    merged = dict(defaults or {})
    merged.update(context)
    return render(template_name, merged)
