from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

JobFunc = Callable[[], Awaitable[None]]


class Scheduler:
    def __init__(self, timezone: str) -> None:
        self._scheduler = AsyncIOScheduler(timezone=timezone)

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def every_seconds(self, seconds: int, func: JobFunc, *, name: Optional[str] = None) -> None:
        self._scheduler.add_job(_wrap_async(func), IntervalTrigger(seconds=seconds), name=name)

    def cron(self, cron: str, func: JobFunc, *, name: Optional[str] = None) -> None:
        """
        cron string: "min hour day month day_of_week"
        Example: "0 8 * * *" (8:00 daily)
        """
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError("cron must have 5 fields: 'min hour day month day_of_week'")
        minute, hour, day, month, dow = parts
        trigger = CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow)
        self._scheduler.add_job(_wrap_async(func), trigger, name=name)

    def at_startup(self, func: JobFunc) -> None:
        self._scheduler.add_job(_wrap_async(func), trigger="date", run_date=datetime.now())


def _wrap_async(func: JobFunc) -> Callable[[], Any]:
    def runner() -> Any:
        return asyncio.create_task(func())

    return runner
