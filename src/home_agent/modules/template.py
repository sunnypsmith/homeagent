from __future__ import annotations

from home_agent.core.logging import get_logger
from home_agent.modules.base import Module, ModuleContext


class TemplateModule(Module):
    """
    Copy this file to start a new behavior module.
    Register it in `modules/registry.py`.
    """

    name = "template"

    async def start(self, ctx: ModuleContext) -> None:
        log = get_logger(module=self.name)

        async def job() -> None:
            log.info("template_job_ran")
            await ctx.events.publish("template.ran", {})

        ctx.scheduler.every_seconds(300, job, name="template_every_5m")
        log.info("scheduled", every_seconds=300)

