#!/usr/bin/env python3
"""Test Icecast streaming to Sonos.

Usage:
    1. Start Icecast:
       docker run -d --name icecast --network host \
         -v /workspace/deploy/icecast/icecast.xml:/etc/icecast2/icecast.xml \
         infiniteproject/icecast

    2. Run this test:
       python scripts/test_icecast_sonos.py --speaker-ip 10.1.2.58

    This will:
    - Generate a short TTS clip via ElevenLabs
    - Push it to Icecast as a live source on /voice-test
    - Tell the Sonos speaker to play the stream
    - Wait for playback to finish
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import time
from typing import Optional

import httpx


async def _push_audio_to_icecast(
    audio_data: bytes,
    *,
    icecast_host: str = "127.0.0.1",
    icecast_port: int = 8000,
    mount: str = "/voice-test",
    password: str = "homeagent",
    content_type: str = "audio/mpeg",
    hold_seconds: float = 5.0,
) -> None:
    """Push audio bytes to an Icecast mount point via SOURCE protocol."""
    import base64

    def _blocking_push():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect((icecast_host, icecast_port))

        creds = base64.b64encode(b"source:" + password.encode()).decode()
        header = (
            "SOURCE %s HTTP/1.0\r\n"
            "Authorization: Basic %s\r\n"
            "Content-Type: %s\r\n"
            "ice-name: Home Agent Voice\r\n"
            "ice-public: 0\r\n"
            "\r\n"
        ) % (mount, creds, content_type)
        s.sendall(header.encode())

        resp = s.recv(1024).decode(errors="replace")
        if "200 OK" not in resp:
            print("Icecast rejected source: %s" % resp.strip())
            s.close()
            return

        print("Icecast source connected, pushing %d bytes..." % len(audio_data))
        chunk_size = 4096
        for i in range(0, len(audio_data), chunk_size):
            s.sendall(audio_data[i:i + chunk_size])
            time.sleep(0.01)
        print("Audio pushed. Holding stream open for %.0fs..." % hold_seconds)
        time.sleep(hold_seconds)
        s.close()
        print("Stream closed.")

    await asyncio.get_running_loop().run_in_executor(None, _blocking_push)


async def _test_sonos_plays_stream(
    speaker_ip: str,
    icecast_host: str,
    icecast_port: int,
    mount: str,
) -> None:
    """Tell a Sonos speaker to play an Icecast stream."""
    try:
        from soco import SoCo
    except ImportError:
        print("ERROR: soco not installed")
        return

    stream_url = "http://%s:%d%s" % (icecast_host, icecast_port, mount)
    print("Stream URL: %s" % stream_url)

    spk = SoCo(speaker_ip)
    print("Speaker: %s (%s)" % (spk.player_name, speaker_ip))

    print("Calling play_uri...")
    t0 = time.monotonic()
    meta = '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"><item id="R:0/0/0" parentID="R:0/0" restricted="true"><dc:title>Voice Test</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><res protocolInfo="http-get:*:audio/mpeg:*">%s</res></item></DIDL-Lite>' % stream_url
    spk.play_uri(stream_url, meta=meta, title="Voice Test")
    t1 = time.monotonic()
    print("play_uri took: %dms" % int((t1 - t0) * 1000))

    print("Waiting for playback...")
    await asyncio.sleep(5.0)
    print("Done.")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speaker-ip", required=True, help="Sonos speaker IP to test")
    parser.add_argument("--icecast-host", default="10.1.1.111", help="Icecast server IP")
    parser.add_argument("--icecast-port", type=int, default=8000)
    parser.add_argument("--mount", default="/voice-test")
    parser.add_argument("--password", default="hackme", help="Icecast source password")
    parser.add_argument("--text", default="This is a streaming test from the home agent voice system.")
    args = parser.parse_args()

    # Step 1: Generate TTS audio
    from home_agent.config import AppSettings
    from home_agent.integrations.tts_elevenlabs import ElevenLabsTTSClient

    settings = AppSettings()
    tts = ElevenLabsTTSClient(
        api_key=settings.elevenlabs.api_key,
        voice_id=settings.elevenlabs.voice_id,
        base_url=settings.elevenlabs.base_url,
        timeout_seconds=settings.elevenlabs.timeout_seconds,
    )

    print("Generating TTS...")
    t0 = time.monotonic()
    audio = await tts.synthesize(text=args.text)
    t1 = time.monotonic()
    print("TTS: %dms, %d bytes, %s" % (int((t1 - t0) * 1000), len(audio.data), audio.content_type))

    # Step 2: Push to Icecast (in background)
    print("Pushing to Icecast mount %s..." % args.mount)
    push_task = asyncio.create_task(_push_audio_to_icecast(
        audio.data,
        icecast_host=args.icecast_host,
        icecast_port=args.icecast_port,
        mount=args.mount,
        password=args.password,
        content_type=audio.content_type,
    ))

    # Small delay for Icecast to accept the source
    await asyncio.sleep(0.5)

    # Step 3: Tell Sonos to play the stream
    await _test_sonos_plays_stream(
        speaker_ip=args.speaker_ip,
        icecast_host=args.icecast_host,
        icecast_port=args.icecast_port,
        mount=args.mount,
    )

    await push_task


if __name__ == "__main__":
    asyncio.run(main())
