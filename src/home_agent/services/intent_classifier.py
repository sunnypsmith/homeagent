"""Lightweight intent classifier for voice commands.

Uses a simple LLM chat call (no tools) to classify user intent into a
category. This is fast and reliable -- Groq never 400s on plain chat.
"""
from __future__ import annotations

from dataclasses import dataclass

from home_agent.core.logging import get_logger
from home_agent.integrations.llm_router import LLMRouter

_log = get_logger(service="intent_classifier")

CATEGORIES = (
    "query",
    "lighting",
    "scene",
    "household",
    "announcement",
    "mute",
    "briefing",
    "conversation",
    "custom",
)

_CLASSIFY_PROMPT = """Classify this home assistant voice command into exactly one category.

Categories:
- query: asking for information (temperature, weather, time, calendar, UPS, internet, sensors, cameras, finances, system health)
- lighting: turning lights on/off or setting brightness
- scene: activating a lighting scene
- household: household announcements (dinner, bedtime, dogs, trash, kids, answer door)
- announcement: making a custom spoken announcement on speakers
- mute: muting or unmuting announcements
- briefing: requesting a morning briefing, executive briefing, or time check
- conversation: general question, chitchat, or anything not covered above
- custom: a smart home automation command not covered by the categories above

Reply with ONLY the category word, nothing else."""


@dataclass
class IntentResult:
    category: str
    raw: str


async def classify(text: str, room_name: str, llm: LLMRouter) -> IntentResult:
    """Classify a voice command into a category using a fast LLM call."""
    try:
        result = await llm.chat(
            system=_CLASSIFY_PROMPT,
            user="[Room: %s] %s" % (room_name, text),
            max_tokens=10,
            temperature=0.0,
        )
        raw = result.text.strip().lower()
        category = raw if raw in CATEGORIES else "conversation"
        _log.info("classified", text=text[:60], category=category, raw=raw)
        return IntentResult(category=category, raw=raw)
    except Exception:
        _log.exception("classify_failed", text=text[:60])
        return IntentResult(category="conversation", raw="error")
