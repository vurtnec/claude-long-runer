"""
Card builders for Feishu interactive UI.

Centralizes JSON construction for Feishu cards that contain a
``select_static`` (single-select dropdown) component.  The bot uses
these cards for ``/project``, ``/mode``, ``/resume``, and ``/list``
so users can pick from a dropdown instead of typing the alias.

Cards use schema 2.0, the same as ``_build_interactive_card`` in
``feishu_bot.py``.

Card action callbacks fire ``card.action.trigger`` with::

    event.event.action.name   → routing key (component "name" attribute)
    event.event.action.option → user's selected value (single-select)
    event.event.action.value  → extra dict from behaviors[].value
    event.event.context.open_chat_id    → chat_id where the card lives
    event.event.context.open_message_id → the card's message_id
"""

from __future__ import annotations

import json
from typing import Optional


# Routing keys used as the ``name`` attribute on ``select_static``.
# The card-action handler in feishu_bot.py routes by these.
ACTION_PROJECT = "project"
ACTION_MODE = "mode"
ACTION_RESUME = "resume"
ACTION_SCHEDULE = "schedule"
ACTION_BACKEND = "backend"
ACTION_MODEL = "model"
ACTION_EFFORT = "effort"


#: Key under which the routing identifier is stored in ``behaviors[].value``.
#: Card schema 2.0 does NOT echo back the ``name`` attribute on
#: ``select_static`` unless the component is nested in a form container,
#: so we encode the routing key inside ``behaviors[].value`` (which is
#: always echoed back as ``event.event.action.value``).  The bot's
#: ``_on_card_action`` reads this key to decide which handler to invoke.
ACTION_KEY = "_action"


def build_select_card(
    intro_markdown: str,
    placeholder: str,
    options: list[dict],
    action_name: str,
    action_value: Optional[dict] = None,
    initial_value: Optional[str] = None,
) -> str:
    """Build a Feishu card with a markdown intro and a single-select dropdown.

    Args:
        intro_markdown: Markdown text shown above the dropdown.
        placeholder: Placeholder text inside the dropdown when empty.
        options: List of ``{"text": str, "value": str}``.  Each ``value``
            MUST be unique — Feishu uses it as the option identifier.
        action_name: Routing identifier embedded in ``behaviors[].value``
            under the ``ACTION_KEY`` key.  The card-action handler reads
            this to decide which handler to invoke.  (We do NOT rely on
            the ``name`` attribute alone because schema 2.0 only echoes
            it back when the component sits inside a form container.)
        action_value: Optional extra dict merged into ``behaviors[].value``
            alongside the routing key.
        initial_value: If set and matches an option's value, that option
            is pre-selected when the card renders.

    Returns:
        JSON string ready to use as the card content.
    """
    select_options = [
        {
            "text": {"tag": "plain_text", "content": opt["text"]},
            "value": opt["value"],
        }
        for opt in options
    ]

    behaviors_value = {ACTION_KEY: action_name}
    if action_value:
        behaviors_value.update(action_value)

    select_element: dict = {
        "tag": "select_static",
        "type": "default",
        "name": action_name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "width": "fill",
        "options": select_options,
        "behaviors": [{"type": "callback", "value": behaviors_value}],
    }
    if initial_value:
        select_element["initial_option"] = initial_value

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": intro_markdown},
                select_element,
            ],
        },
    }
    return json.dumps(card, ensure_ascii=False)


def truncate(text: str, limit: int = 30) -> str:
    """Truncate ``text`` to ``limit`` chars, appending an ellipsis if cut."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
