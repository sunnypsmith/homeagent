"""Voice service — receives UDP audio from Atom Echo devices.

Pipeline per room: UDP audio -> openWakeWord -> VAD -> STT (Groq Whisper)
Stops before MQTT command publishing (to be discussed).
"""
from __future__ import annotations

import asyncio
import io
import struct
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger

# Known Whisper hallucination phrases (returned on silence/noise)
_WHISPER_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thanks for listening",
    "thank you for watching", "thank you for listening",
    "please subscribe", "like and subscribe",
    "see you next time", "bye", "goodbye",
    "you", "the end", "...",
}

ROOM_HEADER_SIZE = 4
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
# Wake word frame sizes (set at runtime based on engine)
WW_FRAME_SAMPLES = 512  # default for Porcupine; 1280 for openWakeWord
WW_FRAME_BYTES = WW_FRAME_SAMPLES * SAMPLE_WIDTH
# webrtcvad needs 30ms frames = 480 samples
VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * SAMPLE_WIDTH


class RoomState:
    LISTENING = "listening"
    CAPTURING = "capturing"
    PROCESSING = "processing"
    DEAF = "deaf"


@dataclass
class Room:
    room_id: str
    friendly_name: str
    state: str = RoomState.LISTENING
    oww_model: Any = None
    porcupine: Any = None

    # Stats
    frames_received: int = 0
    bytes_received: int = 0
    last_audio_at: float = 0.0
    wake_detections: int = 0
    stt_requests: int = 0
    last_button_state: str = ""

    # Audio buffers
    raw_buffer: bytearray = field(default_factory=bytearray)
    command_buffer: bytearray = field(default_factory=bytearray)
    pre_wake_frames: List[bytes] = field(default_factory=list)
    oww_buffer: bytearray = field(default_factory=bytearray)

    # Timing
    last_wake_time: float = 0.0
    last_model_reset: float = 0.0
    last_speech_time: float = 0.0
    command_start_time: float = 0.0


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
        room.raw_buffer.extend(audio)

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


async def _transcribe_groq(audio_wav: bytes, *, api_key: str, model: str, language: str) -> Optional[str]:
    import httpx
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": "Bearer %s" % api_key}
    files = {"file": ("command.wav", audio_wav, "audio/wav")}
    data_fields = {"model": model, "language": language}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, files=files, data=data_fields)
        resp.raise_for_status()
        result = resp.json()
        return (result.get("text") or "").strip() or None


async def run_voice_service() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="voice_service")

    udp_port = settings.voice_udp_port
    room_configs = settings.voice_rooms_parsed
    if not room_configs:
        log.error("no_rooms_configured", hint="Set VOICE_ROOMS in .env")
        return

    # MQTT
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


    # Wake word engine
    global WW_FRAME_SAMPLES, WW_FRAME_BYTES
    wake_engine = settings.voice_wake_engine
    wake_cooldown = settings.voice_wake_cooldown
    wake_threshold = settings.voice_wake_threshold
    porcupine = None

    if wake_engine == "porcupine":
        import pvporcupine
        ppn_path = str(Path(settings.voice_porcupine_model))
        if not Path(ppn_path).is_absolute():
            ppn_path = str(Path(__file__).resolve().parents[3] / ppn_path)
        porcupine = pvporcupine.create(
            access_key=settings.voice_porcupine_key,
            keyword_paths=[ppn_path],
        )
        WW_FRAME_SAMPLES = porcupine.frame_length
        WW_FRAME_BYTES = WW_FRAME_SAMPLES * SAMPLE_WIDTH
        log.info("wake_engine_porcupine", model=ppn_path, frame_length=porcupine.frame_length)
    else:
        from openwakeword.model import Model as OWWModel
        oww_path = str(Path(settings.voice_wake_model))
        if not Path(oww_path).is_absolute():
            oww_path = str(Path(__file__).resolve().parents[3] / oww_path)
        WW_FRAME_SAMPLES = 1280
        WW_FRAME_BYTES = WW_FRAME_SAMPLES * SAMPLE_WIDTH
        log.info("wake_engine_openwakeword", model=oww_path)

    # VAD
    import webrtcvad
    vad = webrtcvad.Vad(2)
    silence_duration = settings.voice_vad_silence_ms / 1000.0
    max_command_duration = settings.voice_vad_max_command_ms / 1000.0

    # STT config
    stt_api_key = settings.voice_stt_api_key
    stt_model = settings.voice_stt_model
    stt_language = settings.voice_stt_language
    if not stt_api_key:
        log.warning("no_stt_api_key", hint="Set VOICE_STT_API_KEY in .env")

    # Rooms — each gets BOTH wake word engines for dual detection
    oww_available = False
    oww_path = None
    try:
        from openwakeword.model import Model as OWWModel
        oww_path_raw = str(Path(settings.voice_wake_model))
        if not Path(oww_path_raw).is_absolute():
            oww_path_raw = str(Path(__file__).resolve().parents[3] / oww_path_raw)
        if Path(oww_path_raw).exists():
            oww_path = oww_path_raw
            oww_available = True
            log.info("oww_available", model=oww_path)
    except Exception:
        pass

    rooms: Dict[str, Room] = {}
    for name, room_id in room_configs.items():
        oww_model = None
        room_porcupine = None
        if wake_engine == "porcupine":
            room_porcupine = pvporcupine.create(
                access_key=settings.voice_porcupine_key,
                keyword_paths=[ppn_path],
            )
        if oww_available:
            oww_model = OWWModel(wakeword_model_paths=[oww_path], enable_speex_noise_suppression=False)
        elif wake_engine != "porcupine":
            oww_model = OWWModel(wakeword_model_paths=[oww_path_raw], enable_speex_noise_suppression=False)
        rooms[room_id] = Room(room_id=room_id, friendly_name=name, oww_model=oww_model, porcupine=room_porcupine)
        engines = []
        if room_porcupine: engines.append("porcupine")
        if oww_model: engines.append("openwakeword")
        log.info("room_registered", name=name, room_id=room_id, engines=engines)

    # UDP listener
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: VoiceUDPProtocol(rooms, log),
        local_addr=("0.0.0.0", udp_port),
    )
    log.info("udp_listening", port=udp_port, rooms=len(rooms))

    # Flush stale audio that accumulated in the UDP buffer while starting up
    await asyncio.sleep(0.5)
    for room in rooms.values():
        flushed = len(room.raw_buffer)
        room.raw_buffer.clear()
        if flushed:
            log.info("buffer_flushed", room=room.friendly_name, bytes=flushed)

    # LED control helper
    def _set_led(room_id: str, color: str) -> None:
        topic = "%s/voice/%s/led" % (settings.mqtt.base_topic, room_id)
        mqttc.publish_json(topic, color)

    # STT processing
    async def _process_command(room: Room, audio_pcm: bytes) -> None:
        room.state = RoomState.PROCESSING
        _set_led(room.room_id, "processing")
        duration_s = len(audio_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
        log.info("stt_start", room=room.friendly_name, audio_seconds=round(duration_s, 1),
                 audio_bytes=len(audio_pcm))
        room.stt_requests += 1
        try:
            if not stt_api_key:
                log.warning("stt_skipped", room=room.friendly_name, reason="no_api_key")
                return
            # Check audio energy — skip if too quiet (likely false wake)
            arr = np.frombuffer(audio_pcm, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32)**2)))
            if rms < 50:
                log.info("stt_skipped_quiet", room=room.friendly_name, rms=round(rms, 1))
                return

            wav = _pcm_to_wav(audio_pcm)
            text = await _transcribe_groq(wav, api_key=stt_api_key, model=stt_model, language=stt_language)
            if text:
                # Filter known Whisper hallucinations
                if text.lower().rstrip(".!?,") in _WHISPER_HALLUCINATIONS:
                    log.info("stt_hallucination_filtered", room=room.friendly_name, text=text)
                    return
                log.info("stt_result", room=room.friendly_name, room_id=room.room_id, text=text)
                from home_agent.bus.envelope import make_event
                evt = make_event(
                    source="voice-service",
                    typ="voice.command",
                    data={"room_id": room.room_id, "room_name": room.friendly_name, "text": text},
                )
                mqttc.publish_json("%s/voice/command" % settings.mqtt.base_topic, evt)
                log.info("voice_command_published", room=room.friendly_name, text=text)
                # Suppress this room to avoid feedback from Sonos response
            else:
                log.info("stt_empty", room=room.friendly_name)
        except Exception as e:
            log.exception("stt_failed", room=room.friendly_name)
            reporter.report_error("stt_failed", e)
            _set_led(room.room_id, "error")
            await asyncio.sleep(2.0)
        finally:
            room.raw_buffer.clear()
            room.pre_wake_frames.clear()
            room.state = RoomState.LISTENING
            _set_led(room.room_id, "listening")

    # Per-room audio processing
    def _process_room_audio(room: Room) -> Optional[bytes]:
        """Process buffered audio for one room. Returns command PCM if complete, else None."""
        while len(room.raw_buffer) >= WW_FRAME_BYTES:
            frame_bytes = bytes(room.raw_buffer[:WW_FRAME_BYTES])
            room.raw_buffer = room.raw_buffer[WW_FRAME_BYTES:]
            frame_np = np.frombuffer(frame_bytes, dtype=np.int16)

            if room.state == RoomState.LISTENING:
                # Maintain pre-wake buffer (last ~500ms)
                room.pre_wake_frames.append(frame_bytes)
                if len(room.pre_wake_frames) > 6:
                    room.pre_wake_frames.pop(0)

                now = time.monotonic()

                # Cooldown check
                if now - room.last_wake_time < wake_cooldown:
                    continue

                # Wake word detection — dual engine (trigger if EITHER detects)
                detected = False
                if room.porcupine is not None:
                    result = room.porcupine.process(frame_np.tolist())
                    if result >= 0:
                        detected = True
                        log.info("wake_detected", room=room.friendly_name, engine="porcupine")
                if not detected and room.oww_model is not None:
                    # OWW needs 1280-sample frames; accumulate from 512-sample Porcupine frames
                    room.oww_buffer.extend(frame_bytes)
                    if len(room.oww_buffer) >= 2560:  # 1280 samples * 2 bytes
                        oww_frame = np.frombuffer(bytes(room.oww_buffer[:2560]), dtype=np.int16)
                        room.oww_buffer = room.oww_buffer[2560:]
                        if now - room.last_model_reset > 900:
                            room.oww_model.reset()
                            room.last_model_reset = now
                        pred = room.oww_model.predict(oww_frame)
                        for model_name, score in pred.items():
                            if score >= wake_threshold:
                                detected = True
                                log.info("wake_detected", room=room.friendly_name,
                                         engine="openwakeword", score=round(score, 3))
                                break

                if detected:
                    room.state = RoomState.CAPTURING
                    room.last_wake_time = now
                    room.command_start_time = now
                    room.last_speech_time = now
                    room.wake_detections += 1
                    room.command_buffer = bytearray()
                    for pf in room.pre_wake_frames:
                        room.command_buffer.extend(pf)
                    room.pre_wake_frames.clear()
                    _set_led(room.room_id, "capturing")

            elif room.state == RoomState.CAPTURING:
                room.command_buffer.extend(frame_bytes)
                now = time.monotonic()

                # VAD: check 30ms sub-frames for speech
                has_speech = False
                for i in range(0, len(frame_bytes) - VAD_FRAME_BYTES + 1, VAD_FRAME_BYTES):
                    sub = frame_bytes[i:i + VAD_FRAME_BYTES]
                    if len(sub) == VAD_FRAME_BYTES:
                        try:
                            if vad.is_speech(sub, SAMPLE_RATE):
                                has_speech = True
                                break
                        except Exception:
                            pass
                if has_speech:
                    room.last_speech_time = now

                silence_elapsed = now - room.last_speech_time
                total_elapsed = now - room.command_start_time

                if silence_elapsed >= silence_duration or total_elapsed >= max_command_duration:
                    reason = "silence" if silence_elapsed >= silence_duration else "max_duration"
                    log.info("command_complete", room=room.friendly_name, reason=reason,
                             duration=round(total_elapsed, 1))
                    audio = bytes(room.command_buffer)
                    room.command_buffer.clear()
                    return audio
        return None

    # Main processing loop
    async def _audio_loop() -> None:
        while True:
            processed_any = False
            for room in rooms.values():
                if room.state in (RoomState.PROCESSING, RoomState.DEAF):
                    continue
                if len(room.raw_buffer) >= WW_FRAME_BYTES:
                    result = _process_room_audio(room)
                    if result is not None:
                        room.state = RoomState.PROCESSING
                        # Play acknowledgment immediately
                        speakers = settings.voice_room_speakers_parsed.get(room.room_id)
                        if speakers:
                            from home_agent.bus.envelope import make_event as _mke
                            ack_evt = _mke(source="voice-service", typ="announce.request",
                                data={"text": "One moment.", "offline_audio_key": "voice_ack", "targets": speakers})
                            mqttc.publish_json("%s/announce/request" % settings.mqtt.base_topic, ack_evt)
                        asyncio.create_task(_process_command(room, result))
                    processed_any = True
            if not processed_any:
                await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.001)

    # MQTT reader (button events, device status)
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
                    if room:
                        room.last_button_state = payload
                        log.info("button_event", room=room.friendly_name, state=payload)
                        if payload == "pressed" and room.state == RoomState.LISTENING:
                            # Push-to-talk: start capturing immediately (skip wake word)
                            room.state = RoomState.CAPTURING
                            room.command_start_time = time.monotonic()
                            room.last_speech_time = time.monotonic()
                            room.last_wake_time = time.monotonic()
                            room.wake_detections += 1
                            room.command_buffer = bytearray()
                            _set_led(room_id, "capturing")
                            log.info("push_to_talk", room=room.friendly_name)
                elif len(parts) >= 4 and parts[-1] == "status":
                    room_id = parts[-2]
                    log.info("device_status", room=room_id, status=payload)
                elif "sonos/playback" in topic:
                    try:
                        evt = msg.json()
                        evt_type = evt.get("type", "")
                        evt_data = evt.get("data", {})
                        if evt_type == "sonos.playback_start":
                            pb_targets = evt_data.get("targets", [])
                            room_speakers = settings.voice_room_speakers_parsed
                            for room in rooms.values():
                                speakers = room_speakers.get(room.room_id, [])
                                if any(s in pb_targets for s in speakers) or not pb_targets:
                                    room.state = RoomState.DEAF
                                    room.raw_buffer.clear()
                                    room.pre_wake_frames.clear()
                                    room.command_buffer.clear()
                                    log.info("room_deaf", room=room.friendly_name)
                                    from home_agent.bus.envelope import make_event as _mk2
                                    mqttc.publish_json("%s/voice/room_status" % settings.mqtt.base_topic, _mk2(source="voice-service", typ="voice.room_status", data={"room_id": room.room_id, "room_name": room.friendly_name, "active": True, "state": "deaf", "frames": room.frames_received, "wakes": room.wake_detections, "stt_reqs": room.stt_requests}))
                        elif evt_type == "sonos.playback_done":
                            for room in rooms.values():
                                if room.state == RoomState.DEAF:
                                    room.raw_buffer.clear()
                                    room.pre_wake_frames.clear()
                                    room.state = RoomState.LISTENING
                                    log.info("room_listening", room=room.friendly_name)
                                    from home_agent.bus.envelope import make_event as _mk3
                                    mqttc.publish_json("%s/voice/room_status" % settings.mqtt.base_topic, _mk3(source="voice-service", typ="voice.room_status", data={"room_id": room.room_id, "room_name": room.friendly_name, "active": True, "state": "listening", "frames": room.frames_received, "wakes": room.wake_detections, "stt_reqs": room.stt_requests}))
                    except Exception:
                        pass

            except Exception:
                await asyncio.sleep(1.0)

    # Status loop
    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(30.0)
            now = time.monotonic()
            for room_id, r in sorted(rooms.items()):
                age = round(now - r.last_audio_at, 1) if r.last_audio_at > 0 else None
                active = age is not None and age < 5.0
                log.info("room_status", room=r.friendly_name, room_id=room_id,
                         state=r.state, active=active, frames=r.frames_received,
                         wakes=r.wake_detections, stt_reqs=r.stt_requests,
                         last_audio_age=age)
                from home_agent.bus.envelope import make_event as _mk
                _evt = _mk(source="voice-service", typ="voice.room_status", data={
                    "room_id": room_id, "room_name": r.friendly_name,
                    "active": active, "state": r.state,
                    "frames": r.frames_received, "wakes": r.wake_detections,
                    "stt_reqs": r.stt_requests,
                })
                mqttc.publish_json("%s/voice/room_status" % settings.mqtt.base_topic, _evt)

    audio_task = asyncio.create_task(_audio_loop())
    mqtt_task = asyncio.create_task(_mqtt_reader())
    status_task = asyncio.create_task(_status_loop())

    try:
        log.info("voice_service_ready", rooms=list(room_configs.keys()), udp_port=udp_port,
                 wake_engine=wake_engine, wake_threshold=wake_threshold,
                 stt_provider=settings.voice_stt_provider)
        await asyncio.Event().wait()
    finally:
        transport.close()
        audio_task.cancel()
        mqtt_task.cancel()
        status_task.cancel()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_service())
    return 0
