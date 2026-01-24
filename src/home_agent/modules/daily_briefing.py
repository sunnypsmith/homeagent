from __future__ import annotations

from home_agent.core.logging import get_logger
from home_agent.modules.base import Module, ModuleContext


class DailyBriefing(Module):
    name = "daily_briefing"

    async def start(self, ctx: ModuleContext) -> None:
        log = get_logger(module=self.name)

        async def job() -> None:
            # Replace with real inputs: calendar, weather, tasks, commutes, etc.
            prompt = "Give a concise morning briefing for a household in 4 bullet points."
            try:
                text = await ctx.llm.chat(system="You are a helpful home assistant.", user=prompt)
            except Exception:
                log.exception("briefing_llm_failed")
                return

            await ctx.events.publish("announce.request", {"text": text})
            await ctx.events.publish("briefing.sent", {"text": text})
            log.info("briefing_sent")

        # 08:00 daily
        ctx.scheduler.cron("0 8 * * *", job, name="daily_briefing_0800")
        log.info("scheduled", cron="0 8 * * *")

