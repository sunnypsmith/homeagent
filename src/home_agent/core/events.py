from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List


@dataclass(frozen=True)
class Event:
    topic: str
    payload: Dict[str, Any]


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """
    Minimal async pub/sub so modules can communicate without tight coupling.
    """

    def __init__(self) -> None:
        self._subs: Dict[str, List[Handler]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, handler: Handler) -> None:
        async with self._lock:
            self._subs.setdefault(topic, []).append(handler)

    async def publish(self, topic: str, payload: Dict[str, Any]) -> None:
        event = Event(topic=topic, payload=payload)
        handlers = list(self._subs.get(topic, []))
        if not handlers:
            return
        await asyncio.gather(*(h(event) for h in handlers), return_exceptions=False)

