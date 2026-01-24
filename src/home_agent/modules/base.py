from __future__ import annotations

from dataclasses import dataclass

from home_agent.core.events import EventBus
from home_agent.core.scheduler import Scheduler
from home_agent.integrations.llm import LLMClient
from home_agent.integrations.audio_host import AudioHost
from home_agent.integrations.sonos_playback import SonosPlayback
from home_agent.integrations.tts import TTSClient


@dataclass(frozen=True)
class ModuleContext:
    scheduler: Scheduler
    events: EventBus
    llm: LLMClient
    tts: TTSClient
    audio_host: AudioHost
    sonos_player: SonosPlayback
    sonos_targets: list[str]
    sonos_concurrency: int


class Module:
    """
    Plug-in interface. Modules should register schedules/subscriptions in start().
    """

    name: str = "unnamed"

    async def start(self, ctx: ModuleContext) -> None:  # pragma: no cover
        raise NotImplementedError

