#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.tts_elevenlabs import ElevenLabsTTSClient
from home_agent.offline_audio import OFFLINE_AUDIO_ITEMS


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return _repo_root() / p


async def _generate_all(*, output_dir: Path, settings: AppSettings) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    tts = ElevenLabsTTSClient(
        api_key=settings.elevenlabs.api_key,
        voice_id=settings.elevenlabs.voice_id,
        base_url=settings.elevenlabs.base_url,
        timeout_seconds=settings.elevenlabs.timeout_seconds,
    )

    for item in OFFLINE_AUDIO_ITEMS:
        audio = await tts.synthesize(text=item["text"], output_format="wav_44100")
        path = output_dir / item["filename"]
        path.write_bytes(audio.data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate offline announcement audio files.")
    parser.add_argument("--output-dir", default="", help="Output directory (overwrites files)")
    args = parser.parse_args()

    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="offline_audio_gen")

    raw_dir = args.output_dir or settings.offline_audio.dir
    output_dir = _resolve_path(raw_dir)

    if not settings.elevenlabs.api_key:
        log.error("missing_elevenlabs_api_key", hint="Set ELEVENLABS_API_KEY in .env")
        return 2

    log.info("generating_offline_audio", dir=str(output_dir), count=len(OFFLINE_AUDIO_ITEMS))
    try:
        asyncio.run(_generate_all(output_dir=output_dir, settings=settings))
    except Exception as e:
        log.error("generation_failed", error=type(e).__name__, detail=str(e))
        return 1

    log.info("generation_complete", dir=str(output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
