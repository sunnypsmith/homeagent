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
import collections
import io
import os
import queue
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

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
    # Accumulates UDP payload until we can enqueue full Porcupine frames (512 samples × 16-bit)
    ww_align_buf: bytearray = field(default_factory=bytearray, repr=False)

    # Sonos playback suppression — True while room's speakers are active
    sonos_playing: bool = False
    sonos_playing_since: float = 0.0

    # Last known source address (ip, port) from UDP — for server-side keepalive
    last_addr: Optional[Tuple[str, int]] = None

    # Timing
    last_wake_time: float = 0.0

    # Audio health metrics
    _packet_times: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=500))
    _max_gap_reset_at: float = 0.0
    max_gap_seconds: float = 0.0
    session_ok: int = 0
    session_fail: int = 0
    last_stt_result: str = ""
    last_session_trigger: str = ""
    _queue_drops: int = 0

    @property
    def packets_per_second(self) -> float:
        now = time.monotonic()
        while self._packet_times and (now - self._packet_times[0]) > 10.0:
            self._packet_times.popleft()
        elapsed = (now - self._packet_times[0]) if self._packet_times else 10.0
        return len(self._packet_times) / max(0.1, elapsed)

    def record_packet(self) -> None:
        now = time.monotonic()
        if self._packet_times:
            gap = now - self._packet_times[-1]
            if gap > self.max_gap_seconds:
                self.max_gap_seconds = gap
        if (now - self._max_gap_reset_at) > 60.0:
            self.max_gap_seconds = 0.0
            self._max_gap_reset_at = now
        self._packet_times.append(now)


class UDPReceiverThread(threading.Thread):
    """Dedicated thread for UDP audio packets.

    Runs a tight recvfrom loop that never blocks on anything except the
    socket read, so packets are never dropped due to event loop contention.
    """

    def __init__(
        self,
        port: int,
        rooms: Dict[str, Room],
        log,
        *,
        porcupine_mode: bool = False,
        live_listeners: Optional[Dict[str, set]] = None,
    ) -> None:
        super().__init__(daemon=True, name="udp-recv")
        self._port = port
        self._rooms = rooms
        self._log = log
        self._porcupine_mode = porcupine_mode
        self._live_listeners = live_listeners if live_listeners is not None else {}
        self._unknown: set = set()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import socket as _socket
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, 8 * 1024 * 1024)
        except Exception:
            pass
        sock.bind(("0.0.0.0", self._port))
        sock.settimeout(1.0)
        self._log.info("udp_thread_ready", port=self._port)

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                if self._stop.is_set():
                    break
                continue

            if len(data) <= ROOM_HEADER_SIZE:
                continue
            room_id = data[:ROOM_HEADER_SIZE].decode("ascii", errors="replace").rstrip("\x00")
            audio = data[ROOM_HEADER_SIZE:]
            room = self._rooms.get(room_id)
            if room is None:
                if room_id not in self._unknown:
                    self._unknown.add(room_id)
                    self._log.warning("unknown_room", room_id=room_id, addr=str(addr))
                continue
            room.frames_received += 1
            room.bytes_received += len(audio)
            room.last_audio_at = time.monotonic()
            room.last_addr = addr
            room.record_packet()
            if len(room.raw_buffer) < _MAX_RAW_BUFFER_BYTES:
                room.raw_buffer.extend(audio)
            if self._porcupine_mode:
                room.ww_align_buf.extend(audio)
                while len(room.ww_align_buf) >= WW_FRAME_BYTES:
                    chunk = bytes(room.ww_align_buf[:WW_FRAME_BYTES])
                    del room.ww_align_buf[:WW_FRAME_BYTES]
                    try:
                        room.audio_queue.put_nowait(chunk)
                    except queue.Full:
                        room._queue_drops += 1

            listeners = self._live_listeners.get(room_id)
            if listeners:
                for q in list(listeners):
                    try:
                        q.put_nowait(audio)
                    except asyncio.QueueFull:
                        pass

        sock.close()


def _pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _porcupine_room_thread(
    *,
    room: Room,
    access_key: str,
    keyword_path: str,
    wake_cooldown: float,
    loop: asyncio.AbstractEventLoop,
    log,
    stop_event: threading.Event,
    start_session,
) -> None:
    """One thread per room; pulls 512-sample frames from room.audio_queue for Picovoice."""
    import pvporcupine

    porcupine = None
    try:
        try:
            porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[keyword_path])
        except Exception:
            log.exception("porcupine_init_failed", room=room.friendly_name, path=keyword_path)
            return

        if porcupine.frame_length != WW_FRAME_SAMPLES:
            log.error(
                "porcupine_frame_length_mismatch",
                room=room.friendly_name,
                need=WW_FRAME_SAMPLES,
                got=porcupine.frame_length,
            )
            return

        log.info("porcupine_ready", room=room.friendly_name, path=keyword_path)
        _processed = 0
        _skipped_state = 0
        _skipped_cooldown = 0
        _speech_misses = 0
        _last_log = time.monotonic()
        _SPEECH_RMS = 300
        _LOG_INTERVAL = 60.0

        _frame_samples = porcupine.frame_length

        while not stop_event.is_set():
            try:
                frame_bytes = room.audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if len(frame_bytes) // 2 != _frame_samples:
                continue

            suppressed = (
                room.state != RoomState.LISTENING
                or (time.monotonic() - room.last_wake_time) < wake_cooldown
            )
            if suppressed:
                _skipped_state += 1
                continue

            arr = np.frombuffer(frame_bytes, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.int32) ** 2)))

            try:
                keyword_index = porcupine.process(arr)
            except Exception as e:
                log.warning("porcupine_process_error", room=room.friendly_name, error=type(e).__name__)
                continue

            _processed += 1

            if keyword_index >= 0:
                log.info(
                    "wake_detected",
                    room=room.friendly_name,
                    engine="porcupine",
                    keyword_index=keyword_index,
                    rms=round(rms),
                    processed=_processed,
                    speech_misses=_speech_misses,
                )
                _speech_misses = 0
                loop.call_soon_threadsafe(lambda r=room: start_session(r))
            elif rms > _SPEECH_RMS:
                _speech_misses += 1

            now = time.monotonic()
            if (now - _last_log) >= _LOG_INTERVAL:
                log.info(
                    "porcupine_stats",
                    room=room.friendly_name,
                    processed=_processed,
                    skipped_state=_skipped_state,
                    skipped_cooldown=_skipped_cooldown,
                    speech_misses=_speech_misses,
                )
                _processed = 0
                _skipped_state = 0
                _skipped_cooldown = 0
                _speech_misses = 0
                _last_log = now
    finally:
        if porcupine is not None:
            try:
                porcupine.delete()
            except Exception:
                pass
        log.info("porcupine_thread_stopped", room=room.friendly_name)


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
    energy_threshold = 200.0
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


def _save_debug_wav(pcm: bytes, *, debug_dir: str, room_name: str, label: str,
                    max_files: int, sample_rate: int = SAMPLE_RATE) -> Optional[str]:
    """Save PCM audio as a WAV file for debugging. Returns the file path or None."""
    try:
        room_dir = Path(debug_dir) / room_name.replace(" ", "_").lower()
        room_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_{room_name}_{label}.wav"
        fpath = room_dir / fname
        with wave.open(str(fpath), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        # Auto-prune old files
        wavs = sorted(room_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        while len(wavs) > max_files:
            wavs.pop(0).unlink(missing_ok=True)
        return str(fpath)
    except Exception:
        return None


async def _transcribe_with_fallback(audio_wav: bytes, *, stt_url: str, stt_api_key: str,
                                     stt_model: str, stt_language: str,
                                     fallback_url: str, fallback_api_key: str,
                                     fallback_model: str, log) -> Optional[str]:
    import httpx
    from home_agent.integrations._retry import api_retry

    @api_retry
    async def _call_stt(url: str, api_key: str, model: str) -> Optional[str]:
        headers = {}
        if api_key:
            headers["Authorization"] = "Bearer %s" % api_key
        files = {"file": ("command.wav", audio_wav, "audio/wav")}
        data_fields = {"model": model, "language": stt_language}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, files=files, data=data_fields)
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip() or None

    try:
        return await _call_stt(stt_url, stt_api_key, stt_model)
    except Exception as e:
        log.warning("stt_primary_failed", error=type(e).__name__, url=stt_url)
        if fallback_url:
            try:
                result = await _call_stt(fallback_url, fallback_api_key, fallback_model)
                log.info("stt_fallback_used", url=fallback_url)
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
    # STT URL: use configured URL, or default to Groq
    stt_url = settings.voice_stt_url or "https://api.groq.com/openai/v1/audio/transcriptions"
    stt_fallback_url = settings.voice_stt_fallback_url
    stt_fallback_key = settings.voice_stt_fallback_api_key
    stt_fallback_model = settings.voice_stt_fallback_model
    room_speakers = settings.voice_room_speakers_parsed
    debug_audio = settings.voice_debug_audio
    debug_dir = settings.voice_debug_dir
    debug_max_files = settings.voice_debug_max_files
    if debug_audio:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        log.info("voice_debug_enabled", dir=debug_dir, max_files=debug_max_files)

    wake_engine = (settings.voice_wake_engine or "whisper").strip().lower()
    if wake_engine not in ("porcupine", "whisper"):
        log.warning("unknown_wake_engine", configured=wake_engine, using="whisper")
        wake_engine = "whisper"
    porcupine_mode = wake_engine == "porcupine"

    porcupine_key = (settings.voice_porcupine_key or "").strip()
    porcupine_model_path: Optional[Path] = None
    if porcupine_mode:
        if not porcupine_key:
            log.error("porcupine_requires_key", hint="Set VOICE_PORCUPINE_KEY in .env")
            return
        model_rel = (settings.voice_porcupine_model or "").strip()
        if not model_rel:
            log.error("porcupine_requires_model", hint="Set VOICE_PORCUPINE_MODEL (.ppn path)")
            return
        mp = Path(model_rel)
        if not mp.is_absolute():
            mp = Path.cwd() / mp
        if not mp.is_file():
            log.error("porcupine_model_not_found", path=str(mp))
            return
        porcupine_model_path = mp

    # VAD
    import webrtcvad
    vad = webrtcvad.Vad(2)

    # Whisper-only wake phrase detection (when VOICE_WAKE_ENGINE=whisper)
    _WAKE_PHRASES = {"master higgins", "hey higgins"}
    _WW_CHUNK_S = 2.5  # seconds of audio per wake word check
    _WW_POLL_S = 1.5   # how often to check each room
    _WW_CHUNK_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH * _WW_CHUNK_S)
    ww_frame_bytes = WW_FRAME_BYTES  # kept for capture phase compatibility

    rooms: Dict[str, Room] = {}
    for name, room_id in room_configs.items():
        rooms[room_id] = Room(room_id=room_id, friendly_name=name)
        log.info("room_registered", name=name, room_id=room_id)

    log.info("voice_room_ids", room_ids=list(rooms.keys()),
             wake_engine=wake_engine, stt_url=stt_url)

    # Keep strong references to session tasks so they aren't GC'd
    _active_tasks: Set[asyncio.Task] = set()

    # Live audio listeners: room_id → set of asyncio.Queue (one per WebSocket client)
    _live_listeners: Dict[str, set] = {}

    loop = asyncio.get_running_loop()

    # UDP listener — dedicated thread so packets are never dropped
    udp_thread = UDPReceiverThread(
        port=udp_port, rooms=rooms, log=log,
        porcupine_mode=porcupine_mode,
        live_listeners=_live_listeners,
    )
    udp_thread.start()
    log.info("udp_listening", port=udp_port, rooms=len(rooms))

    # Flush stale startup audio
    await asyncio.sleep(0.5)
    for room in rooms.values():
        room.raw_buffer.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _announce(text: str, speakers: List[str], offline_key: Optional[str] = None) -> None:
        data: Dict[str, Any] = {"text": text, "targets": speakers, "exempt_mute": True, "exempt_quiet_hours": True}
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

    def _start_session(room: Room, trigger: str = "wake") -> None:
        """Common session-start logic for wake word and push-to-talk."""
        room.state = RoomState.BUSY
        room.wake_detections += 1
        room.last_wake_time = time.monotonic()
        room.last_session_trigger = trigger
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

    def _publish_session_debug(room: Room, *, trigger: str, outcome: str,
                               raw_pcm: bytes = b"", processed_pcm: bytes = b"",
                               text: str = "", timings: Optional[Dict[str, Any]] = None,
                               raw_wav_path: str = "", proc_wav_path: str = "") -> None:
        """Publish a voice.session_debug event with full diagnostics."""
        raw_arr = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) if raw_pcm else np.array([])
        proc_arr = np.frombuffer(processed_pcm, dtype=np.int16).astype(np.float32) if processed_pcm else np.array([])
        raw_rms = float(np.sqrt(np.mean(raw_arr ** 2))) if len(raw_arr) > 0 else 0.0
        raw_peak = float(np.max(np.abs(raw_arr))) if len(raw_arr) > 0 else 0.0
        proc_rms = float(np.sqrt(np.mean(proc_arr ** 2))) if len(proc_arr) > 0 else 0.0
        proc_peak = float(np.max(np.abs(proc_arr))) if len(proc_arr) > 0 else 0.0
        raw_dur = len(raw_arr) / SAMPLE_RATE if len(raw_arr) > 0 else 0.0
        proc_dur = len(proc_arr) / SAMPLE_RATE if len(proc_arr) > 0 else 0.0
        snr_est = round(raw_rms / 50.0, 1) if raw_rms > 0 else 0.0

        data: Dict[str, Any] = {
            "room_id": room.room_id, "room_name": room.friendly_name,
            "trigger": trigger, "outcome": outcome,
            "raw_rms": round(raw_rms, 1), "raw_peak": round(raw_peak, 0),
            "raw_duration_s": round(raw_dur, 2),
            "proc_rms": round(proc_rms, 1), "proc_peak": round(proc_peak, 0),
            "proc_duration_s": round(proc_dur, 2),
            "silence_trimmed_s": round(raw_dur - proc_dur, 2) if proc_dur > 0 else 0,
            "snr_estimate": snr_est,
            "stt_text": text[:120],
            "pps": round(room.packets_per_second, 1),
            "queue_drops": room._queue_drops,
        }
        if raw_wav_path:
            data["raw_wav"] = raw_wav_path
        if proc_wav_path:
            data["proc_wav"] = proc_wav_path
        if timings:
            data.update(timings)
        mqttc.publish_json("%s/voice/session_debug" % base, make_event(
            source="voice-service", typ="voice.session_debug", data=data))

    async def _run_session(room: Room) -> None:
        """Complete voice interaction session.

        Owns the room from wake word through response playback.
        Room is BUSY for the entire duration.
        """
        speakers = room_speakers.get(room.room_id, [])
        has_speakers = speakers and speakers != ["none"]
        trigger = room.last_session_trigger or "wake"
        raw_capture = b""
        processed_capture = b""
        outcome = "unknown"
        stt_text = ""
        raw_wav_path = ""
        proc_wav_path = ""
        timings: Dict[str, Any] = {}

        try:
            t0 = time.monotonic()
            _set_led(room.room_id, "wake")

            if has_speakers:
                mqttc.publish_json("%s/sonos/hold" % base, make_event(
                    source="voice-service", typ="sonos.hold",
                    data={"action": "start", "room_id": room.room_id}))

            if has_speakers:
                _announce("How may I assist you?", speakers, offline_key="voice_prompt")
                log.info("session_prompt", room=room.friendly_name)

            room.raw_buffer.clear()
            _set_led(room.room_id, "capturing")
            if has_speakers:
                await asyncio.sleep(3.0)
            pre_capture_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * 1.0)
            if len(room.raw_buffer) > pre_capture_bytes:
                del room.raw_buffer[:len(room.raw_buffer) - pre_capture_bytes]

            t1 = time.monotonic()
            timings["prompt_ms"] = round((t1 - t0) * 1000)

            log.info("session_capturing", room=room.friendly_name)
            audio_pcm = await _capture_audio(room, max_command_duration)
            raw_capture = audio_pcm

            t2 = time.monotonic()
            timings["capture_ms"] = round((t2 - t1) * 1000)
            log.info("session_timing", room=room.friendly_name,
                     step="capture", elapsed_ms=timings["capture_ms"],
                     audio_bytes=len(audio_pcm))

            if debug_audio:
                raw_wav_path = _save_debug_wav(
                    audio_pcm, debug_dir=debug_dir, room_name=room.friendly_name,
                    label="raw", max_files=debug_max_files) or ""

            if len(audio_pcm) < ww_frame_bytes * 2:
                log.info("session_too_short", room=room.friendly_name, bytes=len(audio_pcm))
                outcome = "too_short"
                return

            _set_led(room.room_id, "processing")

            arr = np.frombuffer(audio_pcm, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            if rms < 50:
                log.info("session_quiet", room=room.friendly_name, rms=round(rms, 1))
                outcome = "too_quiet"
                return

            audio_pcm = _process_audio_for_stt(audio_pcm, log=log)
            processed_capture = audio_pcm

            t3 = time.monotonic()
            timings["audio_process_ms"] = round((t3 - t2) * 1000)

            if debug_audio:
                proc_wav_path = _save_debug_wav(
                    audio_pcm, debug_dir=debug_dir, room_name=room.friendly_name,
                    label="processed", max_files=debug_max_files) or ""

            room.stt_requests += 1
            log.info("session_stt", room=room.friendly_name,
                     audio_seconds=round(len(audio_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH), 1))
            wav = _pcm_to_wav(audio_pcm)
            text = await _transcribe_with_fallback(
                wav, stt_url=stt_url, stt_api_key=stt_api_key, stt_model=stt_model,
                stt_language=stt_language, fallback_url=stt_fallback_url,
                fallback_api_key=stt_fallback_key, fallback_model=stt_fallback_model,
                log=log)

            t4 = time.monotonic()
            timings["stt_ms"] = round((t4 - t3) * 1000)
            stt_text = text or ""

            if not text:
                log.info("session_stt_empty", room=room.friendly_name)
                outcome = "stt_empty"
                return

            if text.lower().rstrip(".!?,") in _WHISPER_HALLUCINATIONS:
                log.info("session_hallucination", room=room.friendly_name, text=text)
                outcome = "hallucination"
                return

            topic = "%s/voice/command" % base
            log.info("session_command", room=room.friendly_name, text=text, topic=topic)
            _publish_command(room, text)
            speakers = room_speakers.get(room.room_id, [])
            if speakers and speakers != ["none"]:
                mqttc.publish_json("%s/sonos/hold" % base,
                    make_event(source="voice-service", typ="sonos.hold", data={"action": "start"}))
                _announce("", speakers, offline_key="voice_typing")
            outcome = "ok"

            t_total = time.monotonic()
            timings["total_ms"] = round((t_total - t0) * 1000)
            mqttc.publish_json("%s/voice/session_timing" % base, make_event(
                source="voice-service", typ="voice.session_timing", data={
                    "room_id": room.room_id, "room_name": room.friendly_name,
                    "prompt_ms": timings.get("prompt_ms", 0),
                    "capture_ms": timings.get("capture_ms", 0),
                    "audio_process_ms": timings.get("audio_process_ms", 0),
                    "stt_ms": timings.get("stt_ms", 0),
                    "total_ms": timings["total_ms"],
                    "text": text[:80],
                }))

        except Exception as e:
            log.exception("session_failed", room=room.friendly_name)
            reporter.report_error("voice_session_failed", e)
            outcome = "error"
        finally:
            if outcome == "ok":
                room.session_ok += 1
            else:
                room.session_fail += 1
            room.last_stt_result = stt_text[:80] if stt_text else outcome

            _publish_session_debug(
                room, trigger=trigger, outcome=outcome,
                raw_pcm=raw_capture, processed_pcm=processed_capture,
                text=stt_text, timings=timings,
                raw_wav_path=raw_wav_path, proc_wav_path=proc_wav_path)

            _set_led(room.room_id, "listening")

            room.raw_buffer.clear()
            room.state = RoomState.LISTENING
            room.last_wake_time = time.monotonic()
            _publish_room_status(room)
            log.info("session_done", room=room.friendly_name, outcome=outcome)

    # ------------------------------------------------------------------
    # Whisper-based wake word detection — one async task per room
    # ------------------------------------------------------------------

    async def _wake_word_loop(room: Room) -> None:
        """Continuously send audio chunks to Whisper and look for wake phrase."""
        import httpx

        log.info("ww_loop_started", room=room.friendly_name, engine="whisper")
        _poll_count = 0
        while True:
            try:
                await asyncio.sleep(_WW_POLL_S)
                _poll_count += 1

                if room.state != RoomState.LISTENING:
                    continue
                if (time.monotonic() - room.last_wake_time) < wake_cooldown:
                    continue

                buf_len = len(room.raw_buffer)
                if buf_len < _WW_CHUNK_BYTES:
                    if _poll_count % 20 == 0:
                        log.info("ww_buf_low", room=room.friendly_name,
                                 buf_bytes=buf_len, need=_WW_CHUNK_BYTES)
                    continue

                chunk = bytes(room.raw_buffer[-_WW_CHUNK_BYTES:])

                arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(arr ** 2)))

                # Log every 10th poll for the office to see what's happening
                if _poll_count % 10 == 0 and room.room_id == "offi":
                    log.info("ww_poll", room=room.friendly_name, rms=round(rms, 0),
                             buf=buf_len, polls=_poll_count)

                if rms < 300:
                    continue

                log.info("ww_sending", room=room.friendly_name, rms=round(rms, 0))
                wav_data = _pcm_to_wav(chunk)
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.post(
                            stt_url,
                            files={"file": ("ww.wav", wav_data, "audio/wav")},
                            data={"model": stt_model, "language": stt_language},
                        )
                        resp.raise_for_status()
                        text = (resp.json().get("text") or "").strip().lower()
                except Exception as e:
                    log.warning("ww_stt_error", room=room.friendly_name,
                                error=type(e).__name__)
                    continue

                log.info("ww_transcript", room=room.friendly_name,
                         text=text[:60], rms=round(rms, 0))

                if not text:
                    continue

                text_clean = text.lower().rstrip(".!?,")
                for phrase in _WAKE_PHRASES:
                    if phrase in text_clean:
                        if room.state != RoomState.LISTENING:
                            break
                        log.info("wake_detected", room=room.friendly_name,
                                 phrase=phrase, transcript=text[:60])
                        _start_session(room)
                        break

            except asyncio.CancelledError:
                return
            except Exception as e:
                log.exception("ww_loop_error", room=room.friendly_name)
                await asyncio.sleep(5.0)

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
                        _start_session(room, trigger="button")

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
                                    room.sonos_playing_since = time.monotonic()
                                    _publish_room_status(room)
                                    log.info("sonos_playback_active", room=room.friendly_name)

                        elif evt_type == "sonos.playback_done":
                            pb_targets = evt_data.get("targets", [])
                            for room in rooms.values():
                                if not room.sonos_playing:
                                    continue
                                speakers = room_speakers.get(room.room_id, [])
                                if pb_targets and not any(s in pb_targets for s in speakers):
                                    continue
                                room.sonos_playing = False
                                _publish_room_status(room)
                                log.info("sonos_playback_ended", room=room.friendly_name)
                    except Exception as e:
                        log.warning("sonos_event_error", error=type(e).__name__)

            except Exception as e:
                log.warning("mqtt_reader_error", error=type(e).__name__)
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # Status loop
    # ------------------------------------------------------------------

    _ww_tasks: Dict[str, asyncio.Task] = {}
    _porcupine_threads: Dict[str, threading.Thread] = {}
    _porcupine_stop = threading.Event()

    def _publish_room_status(r: Room) -> None:
        """Publish room status to MQTT for the dashboard."""
        now = time.monotonic()
        age = round(now - r.last_audio_at, 1) if r.last_audio_at > 0 else None
        active = age is not None and age < 5.0
        if porcupine_mode:
            pt = _porcupine_threads.get(r.room_id)
            ww_task_alive = pt is not None and pt.is_alive()
        else:
            ww_task_alive = r.room_id in _ww_tasks and not _ww_tasks[r.room_id].done()
        mqttc.publish_json("%s/voice/room_status" % base, make_event(
            source="voice-service", typ="voice.room_status", data={
                "room_id": r.room_id, "room_name": r.friendly_name,
                "active": active, "state": r.state,
                "sonos_playing": r.sonos_playing,
                "porcupine_thread": ww_task_alive,
                "queue_size": r.audio_queue.qsize(),
                "frames": r.frames_received, "wakes": r.wake_detections,
                "stt_reqs": r.stt_requests,
                "pps": round(r.packets_per_second, 1),
                "max_gap_s": round(r.max_gap_seconds, 2),
                "queue_drops": r._queue_drops,
                "session_ok": r.session_ok,
                "session_fail": r.session_fail,
                "last_stt": r.last_stt_result[:60],
            }))

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(30.0)
            for _room_id, r in sorted(rooms.items()):
                if porcupine_mode:
                    t = _porcupine_threads.get(r.room_id)
                    if t is None or not t.is_alive():
                        log.warning("porcupine_thread_restart", room=r.friendly_name)
                        nt = threading.Thread(
                            target=_porcupine_room_thread,
                            kwargs={
                                "room": r,
                                "access_key": porcupine_key,
                                "keyword_path": str(porcupine_model_path),
                                "wake_cooldown": wake_cooldown,
                                "loop": loop,
                                "log": log,
                                "stop_event": _porcupine_stop,
                                "start_session": _start_session,
                            },
                            name="porcupine-%s" % r.room_id,
                            daemon=True,
                        )
                        nt.start()
                        _porcupine_threads[r.room_id] = nt
                else:
                    task = _ww_tasks.get(r.room_id)
                    if task is None or task.done():
                        log.warning("ww_task_restart", room=r.friendly_name)
                        _ww_tasks[r.room_id] = asyncio.create_task(_wake_word_loop(r))
                _publish_room_status(r)

    # ------------------------------------------------------------------
    # UDP keepalive — send periodic pings back to each device to keep
    # AP/switch associations active and reduce gaps in the audio stream.
    # ------------------------------------------------------------------

    _KEEPALIVE_INTERVAL = 5.0
    _KEEPALIVE_PAYLOAD = b"\x00"

    async def _keepalive_loop() -> None:
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL)
            for r in rooms.values():
                addr = r.last_addr
                if addr is None:
                    continue
                try:
                    transport.sendto(_KEEPALIVE_PAYLOAD, addr)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    # Reset all LEDs to listening on startup
    for room in rooms.values():
        _set_led(room.room_id, "listening")

    if porcupine_mode:
        assert porcupine_model_path is not None
        for room in rooms.values():
            t = threading.Thread(
                target=_porcupine_room_thread,
                kwargs={
                    "room": room,
                    "access_key": porcupine_key,
                    "keyword_path": str(porcupine_model_path),
                    "wake_cooldown": wake_cooldown,
                    "loop": loop,
                    "log": log,
                    "stop_event": _porcupine_stop,
                    "start_session": _start_session,
                },
                name="porcupine-%s" % room.room_id,
                daemon=True,
            )
            t.start()
            _porcupine_threads[room.room_id] = t
            log.info("porcupine_thread_started", room=room.friendly_name)
    else:
        for room in rooms.values():
            _ww_tasks[room.room_id] = asyncio.create_task(_wake_word_loop(room))
            log.info("ww_task_started", room=room.friendly_name)

    mqtt_task = asyncio.create_task(_mqtt_reader())
    status_task = asyncio.create_task(_status_loop())
    keepalive_task = asyncio.create_task(_keepalive_loop())

    # ------------------------------------------------------------------
    # Live audio WebSocket server (diagnostic)
    # ------------------------------------------------------------------

    _ws_server = None
    live_audio_port = settings.voice_live_audio_port

    if live_audio_port:
        try:
            import websockets
            import websockets.server

            async def _ws_handler(websocket) -> None:
                path = websocket.request.path if hasattr(websocket, 'request') else (websocket.path if hasattr(websocket, 'path') else '')
                parts = path.strip("/").split("/")
                if len(parts) != 2 or parts[0] != "live":
                    await websocket.close(4000, "use /live/{room_id}")
                    return
                room_id = parts[1]
                if room_id not in rooms:
                    await websocket.close(4001, "unknown room")
                    return
                q: asyncio.Queue = asyncio.Queue(maxsize=200)
                _live_listeners.setdefault(room_id, set()).add(q)
                log.info("live_client_connected", room=room_id)
                try:
                    while True:
                        audio = await q.get()
                        await websocket.send(audio)
                except Exception:
                    pass
                finally:
                    _live_listeners.get(room_id, set()).discard(q)
                    if not _live_listeners.get(room_id):
                        _live_listeners.pop(room_id, None)
                    log.info("live_client_disconnected", room=room_id)

            _ws_server = await websockets.serve(
                _ws_handler, "0.0.0.0", live_audio_port,
            )
            log.info("live_audio_ws_started", port=live_audio_port)
        except ImportError:
            log.warning("live_audio_ws_unavailable", hint="pip install websockets")
        except Exception:
            log.exception("live_audio_ws_failed")

    try:
        log.info(
            "voice_service_ready",
            rooms=list(room_configs.keys()),
            udp_port=udp_port,
            stt_url=stt_url,
            wake_engine=wake_engine,
            ww_tasks=len(_ww_tasks),
            porcupine_threads=len(_porcupine_threads),
            live_audio_port=live_audio_port or None,
        )
        await asyncio.Event().wait()
    finally:
        keepalive_task.cancel()
        if _ws_server is not None:
            _ws_server.close()
            await _ws_server.wait_closed()
        udp_thread.stop()
        mqtt_task.cancel()
        status_task.cancel()
        if porcupine_mode:
            _porcupine_stop.set()
            for t in list(_porcupine_threads.values()):
                t.join(timeout=3.0)
        else:
            for task in _ww_tasks.values():
                task.cancel()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_service())
    return 0
