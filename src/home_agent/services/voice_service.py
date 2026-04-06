"""Voice service — receives UDP audio from Atom Echo devices.

Session-based architecture: each voice interaction is a single async task
that owns the room from wake word detection through response playback.

Sonos-aware: subscribes to sonos.playback_start / playback_done events to
suppress wake word detection while speakers are active (prevents the mic
from picking up speaker output and false-triggering Porcupine).

States: LISTENING (processing wake word) or BUSY (session in progress).
"""
from __future__ import annotations

import asyncio
import io
import queue
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np

from home_agent.bus.envelope import make_event
from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger

_WHISPER_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thanks for listening",
    "thank you for watching", "thank you for listening",
    "please subscribe", "like and subscribe",
    "see you next time", "bye", "goodbye",
    "you", "the end", "...", "one moment", "amen",
    "how may i serve you", "how may i assist you",
}

ROOM_HEADER_SIZE = 4
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
WW_FRAME_SAMPLES = 512
WW_FRAME_BYTES = WW_FRAME_SAMPLES * SAMPLE_WIDTH
VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * SAMPLE_WIDTH

_MAX_RAW_BUFFER_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * 30  # 30 seconds max


class RoomState:
    LISTENING = "listening"
    BUSY = "busy"


@dataclass
class Room:
    room_id: str
    friendly_name: str
    state: str = RoomState.LISTENING
    porcupine: Any = None

    # Stats
    frames_received: int = 0
    bytes_received: int = 0
    last_audio_at: float = 0.0
    wake_detections: int = 0
    stt_requests: int = 0

    # Audio buffer (UDP packets append here, capture reads from here)
    raw_buffer: bytearray = field(default_factory=bytearray)

    # Thread-safe queue for wake word detection (UDP handler → Porcupine thread)
    audio_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=500))

    # Sonos playback suppression — True while room's speakers are active
    sonos_playing: bool = False

    # Timing
    last_wake_time: float = 0.0


class VoiceUDPProtocol(asyncio.DatagramProtocol):

    def __init__(self, rooms: Dict[str, Room], log) -> None:
        self._rooms = rooms
        self._log = log
        self._unknown: set = set()

    def connection_made(self, transport) -> None:
        self._log.info("udp_listener_ready")

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) <= ROOM_HEADER_SIZE:
            return
        room_id = data[:ROOM_HEADER_SIZE].decode("ascii", errors="replace").rstrip("\x00")
        audio = data[ROOM_HEADER_SIZE:]
        room = self._rooms.get(room_id)
        if room is None:
            if room_id not in self._unknown:
                self._unknown.add(room_id)
                self._log.warning("unknown_room", room_id=room_id, addr=str(addr))
            return
        room.frames_received += 1
        room.bytes_received += len(audio)
        room.last_audio_at = time.monotonic()
        # Cap buffer to prevent unbounded growth during long Sonos playback
        if len(room.raw_buffer) < _MAX_RAW_BUFFER_BYTES:
            room.raw_buffer.extend(audio)
        # Feed wake word detection thread (non-blocking, drop if full)
        try:
            room.audio_queue.put_nowait(audio)
        except queue.Full:
            pass

    def error_received(self, exc) -> None:
        self._log.warning("udp_error", error=str(exc))


def _pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _process_audio_for_stt(pcm: bytes, *, sample_rate: int = SAMPLE_RATE, log) -> bytes:
    """Noise-reduce, trim silence, and normalize captured PCM before STT.

    Returns processed PCM bytes (16-bit mono) ready for _pcm_to_wav.
    """
    import noisereduce as nr

    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(arr) < sample_rate // 4:
        return pcm

    raw_rms = float(np.sqrt(np.mean(arr ** 2)))

    cleaned = nr.reduce_noise(
        y=arr, sr=sample_rate, stationary=True, prop_decrease=0.75,
    )

    frame_len = int(sample_rate * 0.03)
    energy_threshold = 30.0
    frame_energies = np.array([
        np.sqrt(np.mean(cleaned[i:i + frame_len] ** 2))
        for i in range(0, len(cleaned) - frame_len, frame_len)
    ])
    voiced = np.where(frame_energies > energy_threshold)[0]
    if len(voiced) == 0:
        log.info("audio_process_all_silence", raw_rms=round(raw_rms, 1))
        return pcm

    pad_frames = 5
    start_frame = max(0, voiced[0] - pad_frames)
    end_frame = min(len(frame_energies), voiced[-1] + pad_frames + 1)
    cleaned = cleaned[start_frame * frame_len : end_frame * frame_len]

    peak = np.max(np.abs(cleaned))
    if peak > 0:
        target = 0.8 * 32767
        cleaned = cleaned * (target / peak)

    cleaned = np.clip(cleaned, -32768, 32767).astype(np.int16)
    final_rms = float(np.sqrt(np.mean(cleaned.astype(np.float32) ** 2)))

    raw_dur = len(arr) / sample_rate
    final_dur = len(cleaned) / sample_rate
    log.info("audio_processed",
             raw_rms=round(raw_rms, 1), final_rms=round(final_rms, 1),
             raw_duration=round(raw_dur, 1), final_duration=round(final_dur, 1),
             trimmed_seconds=round(raw_dur - final_dur, 1))

    return cleaned.tobytes()


async def _transcribe_with_fallback(audio_wav: bytes, *, stt_api_key: str, stt_model: str,
                                     stt_language: str, fallback_api_key: str, log) -> Optional[str]:
    import httpx
    from home_agent.integrations._retry import api_retry

    @api_retry
    async def _call_stt(url: str, api_key: str, model: str) -> Optional[str]:
        headers = {"Authorization": "Bearer %s" % api_key}
        files = {"file": ("command.wav", audio_wav, "audio/wav")}
        data_fields = {"model": model, "language": stt_language}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, files=files, data=data_fields)
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip() or None

    try:
        return await _call_stt("https://api.groq.com/openai/v1/audio/transcriptions", stt_api_key, stt_model)
    except Exception as e:
        log.warning("stt_primary_failed", error=type(e).__name__)
        if fallback_api_key:
            try:
                result = await _call_stt("https://api.openai.com/v1/audio/transcriptions", fallback_api_key, "whisper-1")
                log.info("stt_fallback_used", provider="openai")
                return result
            except Exception as e2:
                log.warning("stt_fallback_failed", error=type(e2).__name__)
        raise


async def run_voice_service() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="voice_service")

    udp_port = settings.voice_udp_port
    room_configs = settings.voice_rooms_parsed
    if not room_configs:
        log.error("no_rooms_configured", hint="Set VOICE_ROOMS in .env")
        return

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-voice-service",
    )
    await mqttc.connect()
    reporter = ErrorReporter(mqttc=mqttc, service="voice-service", base_topic=settings.mqtt.base_topic)
    reporter.start_heartbeat(interval_seconds=30.0)
    log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    mqttc.subscribe("%s/voice/+/button" % settings.mqtt.base_topic)
    mqttc.subscribe("%s/voice/+/status" % settings.mqtt.base_topic)
    mqttc.subscribe("%s/sonos/playback" % settings.mqtt.base_topic)

    base = settings.mqtt.base_topic
    wake_cooldown = settings.voice_wake_cooldown
    silence_duration = settings.voice_vad_silence_ms / 1000.0
    max_command_duration = settings.voice_vad_max_command_ms / 1000.0
    stt_api_key = settings.voice_stt_api_key
    stt_model = settings.voice_stt_model
    stt_language = settings.voice_stt_language
    fallback_key = settings.llm_fallback.api_key if settings.llm_fallback.enabled else ""
    room_speakers = settings.voice_room_speakers_parsed

    # Wake word engine
    import pvporcupine
    ppn_path = str(Path(settings.voice_porcupine_model))
    if not Path(ppn_path).is_absolute():
        ppn_path = str(Path(__file__).resolve().parents[3] / ppn_path)

    # VAD
    import webrtcvad
    vad = webrtcvad.Vad(2)

    # Rooms — each gets its own Porcupine instance (they carry internal state)
    rooms: Dict[str, Room] = {}
    for name, room_id in room_configs.items():
        p = pvporcupine.create(
            access_key=settings.voice_porcupine_key,
            keyword_paths=[ppn_path],
        )
        rooms[room_id] = Room(room_id=room_id, friendly_name=name, porcupine=p)
        log.info("room_registered", name=name, room_id=room_id, frame_length=p.frame_length)

    # Frame sizes derived from the engine (all instances share the same model)
    ww_frame_samples = next(iter(rooms.values())).porcupine.frame_length
    ww_frame_bytes = ww_frame_samples * SAMPLE_WIDTH
    log.info("voice_room_ids", room_ids=list(rooms.keys()),
             ww_frame_samples=ww_frame_samples)

    # Keep strong references to session tasks so they aren't GC'd
    _active_tasks: Set[asyncio.Task] = set()

    # UDP listener
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: VoiceUDPProtocol(rooms, log),
        local_addr=("0.0.0.0", udp_port),
    )
    log.info("udp_listening", port=udp_port, rooms=len(rooms))

    # Flush stale startup audio
    await asyncio.sleep(0.5)
    for room in rooms.values():
        room.raw_buffer.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _announce(text: str, speakers: List[str], offline_key: Optional[str] = None) -> None:
        data: Dict[str, Any] = {"text": text, "targets": speakers}
        if offline_key:
            data["offline_audio_key"] = offline_key
        mqttc.publish_json("%s/announce/request" % base, make_event(
            source="voice-service", typ="announce.request", data=data))

    def _publish_command(room: Room, text: str) -> None:
        evt = make_event(source="voice-service", typ="voice.command",
            data={"room_id": room.room_id, "room_name": room.friendly_name, "text": text})
        mqttc.publish_json("%s/voice/command" % base, evt)

    def _set_led(room_id: str, state: str) -> None:
        try:
            topic = "%s/voice/%s/led" % (base, room_id)
            mqttc._client.publish(topic, payload=state.encode("utf-8"), qos=0)
        except Exception:
            pass

    def _start_session(room: Room) -> None:
        """Common session-start logic for wake word and push-to-talk."""
        room.state = RoomState.BUSY
        room.wake_detections += 1
        room.last_wake_time = time.monotonic()
        room.raw_buffer.clear()
        _publish_room_status(room)
        task = asyncio.create_task(_run_session(room))
        _active_tasks.add(task)
        def _on_done(t, _room=room):
            _active_tasks.discard(t)
            try:
                exc = t.exception()
                if exc is not None:
                    log.exception("session_task_failed", room=_room.friendly_name)
                    reporter.report_error("voice_session_failed", exc)
            except asyncio.CancelledError:
                pass
        task.add_done_callback(_on_done)

    async def _capture_audio(room: Room, duration_limit: float) -> bytes:
        """Capture audio from the room's buffer with VAD silence detection.

        Runs on the event loop — both this and datagram_received are
        single-threaded, so raw_buffer access is serialized.
        """
        command_buf = bytearray()
        start = time.monotonic()
        last_speech = time.monotonic()

        while True:
            now = time.monotonic()
            if (now - last_speech) >= silence_duration and len(command_buf) > ww_frame_bytes * 3:
                log.info("capture_done", room=room.friendly_name, reason="silence",
                         duration=round(now - start, 1), bytes=len(command_buf))
                break
            if (now - start) >= duration_limit:
                log.info("capture_done", room=room.friendly_name, reason="max_duration",
                         duration=round(now - start, 1), bytes=len(command_buf))
                break

            if len(room.raw_buffer) >= ww_frame_bytes:
                frame = bytes(room.raw_buffer[:ww_frame_bytes])
                del room.raw_buffer[:ww_frame_bytes]
                command_buf.extend(frame)

                for i in range(0, len(frame) - VAD_FRAME_BYTES + 1, VAD_FRAME_BYTES):
                    sub = frame[i:i + VAD_FRAME_BYTES]
                    if len(sub) == VAD_FRAME_BYTES:
                        try:
                            if vad.is_speech(sub, SAMPLE_RATE):
                                last_speech = time.monotonic()
                                break
                        except Exception:
                            log.warning("vad_error", room=room.friendly_name)
            else:
                await asyncio.sleep(0.005)

        return bytes(command_buf)

    # ------------------------------------------------------------------
    # Voice session — one async task per interaction
    # ------------------------------------------------------------------

    async def _run_session(room: Room) -> None:
        """Complete voice interaction session.

        Owns the room from wake word through response playback.
        Room is BUSY for the entire duration.
        """
        speakers = room_speakers.get(room.room_id, [])
        has_speakers = speakers and speakers != ["none"]

        try:
            t0 = time.monotonic()
            _set_led(room.room_id, "wake")

            # Tell sonos gateway to hold speakers open (skip restore between announcements)
            if has_speakers:
                mqttc.publish_json("%s/sonos/hold" % base, make_event(
                    source="voice-service", typ="sonos.hold",
                    data={"action": "start", "room_id": room.room_id}))

            # Step 1: Play "How may I assist you?"
            if has_speakers:
                _announce("How may I assist you?", speakers, offline_key="voice_prompt")
                log.info("session_prompt", room=room.friendly_name)

            # Step 2: Wait for prompt to play on Sonos (~3s)
            room.raw_buffer.clear()
            _set_led(room.room_id, "capturing")
            if has_speakers:
                await asyncio.sleep(3.0)
            # Keep the last 1s of audio in case the user started talking
            # during or right after the prompt — avoids clipping the first word.
            pre_capture_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * 1.0)
            if len(room.raw_buffer) > pre_capture_bytes:
                del room.raw_buffer[:len(room.raw_buffer) - pre_capture_bytes]

            t1 = time.monotonic()
            log.info("session_timing", room=room.friendly_name,
                     step="prompt_wait", elapsed_ms=round((t1 - t0) * 1000))

            # Step 3: Capture command audio
            log.info("session_capturing", room=room.friendly_name)
            audio_pcm = await _capture_audio(room, max_command_duration)

            t2 = time.monotonic()
            log.info("session_timing", room=room.friendly_name,
                     step="capture", elapsed_ms=round((t2 - t1) * 1000),
                     audio_bytes=len(audio_pcm))

            if len(audio_pcm) < ww_frame_bytes * 2:
                log.info("session_too_short", room=room.friendly_name, bytes=len(audio_pcm))
                return

            _set_led(room.room_id, "processing")

            # Step 4: Check raw audio quality
            arr = np.frombuffer(audio_pcm, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            if rms < 50:
                log.info("session_quiet", room=room.friendly_name, rms=round(rms, 1))
                return

            # Step 5: Noise reduce, trim silence, normalize
            audio_pcm = _process_audio_for_stt(audio_pcm, log=log)

            t3 = time.monotonic()
            log.info("session_timing", room=room.friendly_name,
                     step="audio_process", elapsed_ms=round((t3 - t2) * 1000))

            # Step 6: STT
            room.stt_requests += 1
            log.info("session_stt", room=room.friendly_name,
                     audio_seconds=round(len(audio_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH), 1))
            wav = _pcm_to_wav(audio_pcm)
            text = await _transcribe_with_fallback(
                wav, stt_api_key=stt_api_key, stt_model=stt_model,
                stt_language=stt_language, fallback_api_key=fallback_key, log=log)

            t4 = time.monotonic()
            log.info("session_timing", room=room.friendly_name,
                     step="stt", elapsed_ms=round((t4 - t3) * 1000))

            if not text:
                log.info("session_stt_empty", room=room.friendly_name)
                return

            # Step 7: Filter hallucinations
            if text.lower().rstrip(".!?,") in _WHISPER_HALLUCINATIONS:
                log.info("session_hallucination", room=room.friendly_name, text=text)
                return

            # Step 8: Publish command
            topic = "%s/voice/command" % base
            log.info("session_command", room=room.friendly_name, text=text, topic=topic)
            _publish_command(room, text)

            # Publish session timing for dashboard
            t_total = time.monotonic()
            mqttc.publish_json("%s/voice/session_timing" % base, make_event(
                source="voice-service", typ="voice.session_timing", data={
                    "room_id": room.room_id, "room_name": room.friendly_name,
                    "prompt_ms": round((t1 - t0) * 1000),
                    "capture_ms": round((t2 - t1) * 1000),
                    "audio_process_ms": round((t3 - t2) * 1000),
                    "stt_ms": round((t4 - t3) * 1000),
                    "total_ms": round((t_total - t0) * 1000),
                    "text": text[:80],
                }))

        except Exception as e:
            log.exception("session_failed", room=room.friendly_name)
            reporter.report_error("voice_session_failed", e)
        finally:
            # Reset LED for rooms without speakers (no playback_done will come)
            if not has_speakers:
                _set_led(room.room_id, "listening")

            # Session complete — return to listening
            room.raw_buffer.clear()
            room.state = RoomState.LISTENING
            room.last_wake_time = time.monotonic()
            _publish_room_status(room)
            log.info("session_done", room=room.friendly_name)

    # ------------------------------------------------------------------
    # Per-room Porcupine threads — one thread per room for parallel wake detection
    # ------------------------------------------------------------------

    _ww_threads: List[threading.Thread] = []
    _ww_stop = threading.Event()

    def _wake_word_thread(room: Room) -> None:
        """Dedicated thread for one room's wake word detection.

        Reads audio from room.audio_queue, assembles into Porcupine-sized
        frames, and posts wake events back to the event loop.
        """
        thread_name = "porcupine-%s" % room.room_id
        log.info("ww_thread_started", room=room.friendly_name, thread=thread_name)
        buf = bytearray()
        frames_processed = 0
        try:
            while not _ww_stop.is_set():
                # Skip processing when not listening or Sonos is playing
                if room.state != RoomState.LISTENING or room.sonos_playing:
                    while not room.audio_queue.empty():
                        try:
                            room.audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    buf.clear()
                    time.sleep(0.05)
                    continue

                try:
                    data = room.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                buf.extend(data)

                while len(buf) >= ww_frame_bytes:
                    frame_data = bytes(buf[:ww_frame_bytes])
                    del buf[:ww_frame_bytes]

                    if (time.monotonic() - room.last_wake_time) < wake_cooldown:
                        continue

                    if room.porcupine is not None:
                        try:
                            frame_np = np.frombuffer(frame_data, dtype=np.int16)
                            result = room.porcupine.process(frame_np.tolist())
                            frames_processed += 1
                        except Exception as e:
                            log.warning("porcupine_error", room=room.friendly_name,
                                        error=type(e).__name__, detail=str(e)[:100])
                            continue
                        if result >= 0:
                            loop.call_soon_threadsafe(_on_wake_detected, room)
                            buf.clear()
                            break
        except Exception as e:
            log.exception("ww_thread_crashed", room=room.friendly_name,
                          frames_processed=frames_processed)
        finally:
            log.warning("ww_thread_exited", room=room.friendly_name,
                        frames_processed=frames_processed)

    def _on_wake_detected(room: Room) -> None:
        """Called on the event loop thread when a Porcupine thread detects a wake word."""
        if room.state != RoomState.LISTENING:
            return
        log.info("wake_detected", room=room.friendly_name)
        _start_session(room)

    # ------------------------------------------------------------------
    # MQTT reader (button events + Sonos playback awareness)
    # ------------------------------------------------------------------

    async def _mqtt_reader() -> None:
        while True:
            try:
                msg = await mqttc.next_message()
                topic = msg.topic
                payload = msg.payload.decode("utf-8", errors="replace").strip()
                parts = topic.split("/")

                if len(parts) >= 4 and parts[-1] == "button":
                    room_id = parts[-2]
                    room = rooms.get(room_id)
                    if room and payload == "pressed" and room.state == RoomState.LISTENING:
                        log.info("push_to_talk", room=room.friendly_name)
                        _start_session(room)

                elif len(parts) >= 4 and parts[-1] == "status":
                    room_id = parts[-2]
                    log.info("device_status", room=room_id, status=payload)

                elif len(parts) >= 3 and parts[-2] == "sonos" and parts[-1] == "playback":
                    try:
                        evt = msg.json()
                        evt_type = evt.get("type", "")
                        evt_data = evt.get("data") or {}

                        if evt_type == "sonos.playback_start":
                            pb_targets = evt_data.get("targets", [])
                            for room in rooms.values():
                                speakers = room_speakers.get(room.room_id, [])
                                if any(s in pb_targets for s in speakers) or not pb_targets:
                                    room.sonos_playing = True
                                    if room.state != RoomState.BUSY:
                                        room.raw_buffer.clear()
                                    _publish_room_status(room)
                                    log.info("room_deaf", room=room.friendly_name,
                                             reason="sonos_playback_start")

                        elif evt_type == "sonos.playback_done":
                            pb_targets = evt_data.get("targets", [])
                            for room in rooms.values():
                                if not room.sonos_playing:
                                    continue
                                speakers = room_speakers.get(room.room_id, [])
                                if pb_targets and not any(s in pb_targets for s in speakers):
                                    continue
                                room.sonos_playing = False
                                if room.state != RoomState.BUSY:
                                    room.raw_buffer.clear()
                                if room.state == RoomState.LISTENING:
                                    # Brief bright flash then back to dim green — visible "ready" cue
                                    _set_led(room.room_id, "wake")
                                    async def _flash_ready(_rid=room.room_id):
                                        await asyncio.sleep(0.4)
                                        _set_led(_rid, "listening")
                                    asyncio.create_task(_flash_ready())
                                _publish_room_status(room)
                                log.info("room_undeaf", room=room.friendly_name,
                                         reason="sonos_playback_done")
                    except Exception as e:
                        log.warning("sonos_event_error", error=type(e).__name__)

            except Exception as e:
                log.warning("mqtt_reader_error", error=type(e).__name__)
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # Status loop
    # ------------------------------------------------------------------

    def _publish_room_status(r: Room) -> None:
        """Publish room status to MQTT for the dashboard."""
        now = time.monotonic()
        age = round(now - r.last_audio_at, 1) if r.last_audio_at > 0 else None
        active = age is not None and age < 5.0
        thread_alive = any(
            t.name == "porcupine-%s" % r.room_id and t.is_alive()
            for t in _ww_threads
        )
        mqttc.publish_json("%s/voice/room_status" % base, make_event(
            source="voice-service", typ="voice.room_status", data={
                "room_id": r.room_id, "room_name": r.friendly_name,
                "active": active, "state": r.state,
                "sonos_playing": r.sonos_playing,
                "porcupine_thread": thread_alive,
                "queue_size": r.audio_queue.qsize(),
                "frames": r.frames_received, "wakes": r.wake_detections,
                "stt_reqs": r.stt_requests,
            }))

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(30.0)
            for room_id, r in sorted(rooms.items()):
                _publish_room_status(r)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    # Launch one Porcupine thread per room
    for room in rooms.values():
        t = threading.Thread(
            target=_wake_word_thread, args=(room,),
            name="porcupine-%s" % room.room_id, daemon=True,
        )
        t.start()
        _ww_threads.append(t)
        log.info("porcupine_thread_started", room=room.friendly_name)

    mqtt_task = asyncio.create_task(_mqtt_reader())
    status_task = asyncio.create_task(_status_loop())

    try:
        log.info("voice_service_ready", rooms=list(room_configs.keys()),
                 udp_port=udp_port, stt_provider=settings.voice_stt_provider,
                 porcupine_threads=len(_ww_threads))
        await asyncio.Event().wait()
    finally:
        transport.close()
        # Stop Porcupine threads
        _ww_stop.set()
        for t in _ww_threads:
            t.join(timeout=2.0)
        mqtt_task.cancel()
        status_task.cancel()
        # Free Porcupine native instances
        for room in rooms.values():
            if room.porcupine is not None:
                try:
                    room.porcupine.delete()
                except Exception:
                    pass
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_service())
    return 0
