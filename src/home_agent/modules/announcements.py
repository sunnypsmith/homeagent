from __future__ import annotations

from home_agent.core.logging import get_logger
from home_agent.modules.base import Module, ModuleContext


class Announcements(Module):
    name = "announcements"

    async def start(self, ctx: ModuleContext) -> None:
        log = get_logger(module=self.name)

        async def on_request(evt) -> None:
            text = (evt.payload or {}).get("text") or ""
            if not str(text).strip():
                return
            voice_id = (evt.payload or {}).get("voice_id") or None
            volume = (evt.payload or {}).get("volume") or None

            try:
                audio = await ctx.tts.synthesize(text=str(text), voice_id=voice_id)
                hosted = ctx.audio_host.host_bytes(
                    data=audio.data,
                    filename="announce.%s" % audio.suggested_ext,
                    content_type=audio.content_type,
                    route_to_ip=ctx.sonos_targets[0],
                )
                await ctx.sonos_player.play_url(
                    url=hosted.url,
                    volume=volume,
                    title="Home Agent",
                    concurrency=ctx.sonos_concurrency,
                )
                log.info("announcement_played")
            except Exception:
                log.exception("announcement_failed")

        await ctx.events.subscribe("announce.request", on_request)
        log.info("subscribed", topic="announce.request")

