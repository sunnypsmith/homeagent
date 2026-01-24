from __future__ import annotations

from home_agent.core.logging import get_logger
from home_agent.modules.base import Module, ModuleContext


class Heartbeat(Module):
    name = "heartbeat"

    async def start(self, ctx: ModuleContext) -> None:
        log = get_logger(module=self.name)

        async def tick() -> None:
            log.info("alive")
            await ctx.events.publish("agent.heartbeat", {})

        ctx.scheduler.every_seconds(60, tick, name="heartbeat_60s")
        log.info("scheduled", every_seconds=60)

