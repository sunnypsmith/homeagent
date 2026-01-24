from __future__ import annotations

import asyncio
import signal

from home_agent.config import AppSettings
from home_agent.core.events import EventBus
from home_agent.core.logging import get_logger
from home_agent.core.scheduler import Scheduler
from home_agent.integrations.llm import LLMClient
from home_agent.integrations.audio_host import AudioHost
from home_agent.integrations.sonos_playback import SonosPlayback
from home_agent.integrations.tts_elevenlabs import ElevenLabsTTSClient
from home_agent.modules.base import ModuleContext
from home_agent.modules.registry import default_modules
from home_agent.startup.checks import CheckStatus, run_startup_checks


class HomeAgentApp:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._log = get_logger(app=settings.name)
        # Create asyncio primitives inside the running loop (Python 3.8 is loop-bound).
        self._stop: "asyncio.Event" = None  # type: ignore[assignment]

        self._events = EventBus()
        self._scheduler = Scheduler(timezone=settings.timezone)

        self._llm = LLMClient(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            timeout_seconds=settings.llm.timeout_seconds,
        )

        self._sonos_targets = settings.sonos.announce_target_ips
        self._sonos_concurrency = settings.sonos.announce_concurrency
        self._sonos_player = SonosPlayback(
            speaker_ips=self._sonos_targets,
            default_volume=settings.sonos.default_volume,
        )

        self._audio_host = AudioHost()
        self._tts = ElevenLabsTTSClient(
            api_key=settings.elevenlabs.api_key,
            voice_id=settings.elevenlabs.voice_id,
            base_url=settings.elevenlabs.base_url,
            timeout_seconds=settings.elevenlabs.timeout_seconds,
        )

        self._modules = default_modules()

    async def run(self) -> None:
        self._log.info("starting", timezone=self._settings.timezone)
        self._stop = asyncio.Event()
        self._install_signal_handlers()

        # Preflight checks (fast, side-effect-free). Fail fast on critical failures.
        results = await run_startup_checks(llm=self._llm)
        if any(r.status == CheckStatus.FAIL for r in results):
            self._log.error("startup_checks_failed")
            return

        ctx = ModuleContext(
            scheduler=self._scheduler,
            events=self._events,
            llm=self._llm,
            tts=self._tts,
            audio_host=self._audio_host,
            sonos_player=self._sonos_player,
            sonos_targets=self._sonos_targets,
            sonos_concurrency=self._sonos_concurrency,
        )

        # Start modules (register schedules/subscriptions).
        for m in self._modules:
            self._log.info("module_starting", module=m.name)
            await m.start(ctx)

        self._scheduler.start()
        self._log.info("running")
        await self._stop.wait()
        self._log.info("stopping")
        self._scheduler.shutdown()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            return

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:  # pragma: no cover (Windows)
                pass
