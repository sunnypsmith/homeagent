from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Tuple

import structlog
from rich.logging import RichHandler


def configure_logging(level: str) -> None:
    """
    Human-friendly logs for long-running debugging.
    """
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                rich_tracebacks=True,
                markup=True,  # allow rich markup in messages
                show_time=True,
                show_level=True,
                show_path=False,
            )
        ],
    )

    # Quiet noisy libraries by default; tune as desired.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _pretty_rich_renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(**kwargs: Any) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(**kwargs)


def _pretty_rich_renderer(
    logger: Any, method_name: str, event_dict: Dict[str, Any]
) -> str:  # pragma: no cover
    """
    Final renderer for structlog -> RichHandler.
    Produces compact, emoji-aided, scan-friendly lines.
    """
    event = str(event_dict.pop("event", method_name))
    level = str(event_dict.pop("level", "")).lower()

    icon = _icon_for(event, level)
    title = _style_for(level, "%s %s" % (icon, event)).strip()

    # Prefer a few well-known keys first, then the rest sorted.
    preferred = ("app", "module", "component", "name", "status", "details", "cron", "every_seconds")
    parts: list[str] = []
    for k in preferred:
        if k in event_dict:
            parts.append("%s=%r" % (k, event_dict.pop(k)))

    for k in sorted(event_dict.keys()):
        parts.append("%s=%r" % (k, event_dict[k]))

    if parts:
        return "%s  %s" % (title, " ".join(parts))
    return title


def _style_for(level: str, text: str) -> str:
    if level in ("error", "critical"):
        return "[bold red]%s[/bold red]" % text
    if level == "warning":
        return "[bold yellow]%s[/bold yellow]" % text
    return "[bold cyan]%s[/bold cyan]" % text


def _icon_for(event: str, level: str) -> str:
    # Event-specific icons (fallback to level icons).
    if event in ("starting",):
        return "🚀"
    if event in ("running",):
        return "🟢"
    if event in ("stopping",):
        return "🛑"
    if event in ("startup_checks_complete", "startup_check"):
        return "🧪"
    if event in ("module_starting",):
        return "🧩"
    if event in ("scheduled",):
        return "⏱️"
    if event in ("alive",):
        return "💓"

    if level in ("error", "critical"):
        return "❌"
    if level == "warning":
        return "⚠️"
    return "✅"

