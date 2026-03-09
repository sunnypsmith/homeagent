# Voice Assistant

## Overview

The voice assistant provides hands-free voice control via M5Stack Atom Echo devices running custom ESPHome firmware. The pipeline:

```
Atom Echo mics (ESPHome, UDP PCM)
  → voice-service (UDP listener)
    → Wake word detection (Porcupine)
      → Prompt ("How may I assist you?" on Sonos)
        → WebRTC VAD (speech capture with 1s pre-buffer)
          → Audio processing (noise reduction → silence trim → peak normalize)
            → STT (Groq Whisper)
              → MQTT voice.command
                → voice-intent-agent (LLM with tool calling)
                  → TTS text normalization (LLM expands abbreviations)
                    → Sonos spoken response
```

Two services work together:
- **`voice-service`**: audio processing pipeline (UDP → wake word → VAD → STT → MQTT)
- **`voice-intent-agent`**: command interpretation (MQTT → LLM → tool calls → Sonos response)

## ESPHome Firmware

Device configs live in `esphome/`. Each Atom Echo streams 16 kHz 16-bit PCM audio over UDP with a 4-byte room ID header.

### Device configs

| File | Room | Room ID | Listen Port |
|------|------|---------|-------------|
| `atom-echo-voice.yaml` | Office | `offi` | 9200 |
| `atom-echo-voice-office.yaml` | Office (alt) | `offi` | 9200 |
| `atom-echo-bedroom.yaml` | Bedroom | `bedr` | 9203 |
| `atom-echo-kitchen.yaml` | Kitchen | `ktch` | 9201 |
| `atom-echo-dining.yaml` | Dining | `dine` | 9202 |

Shared files:
- `esphome/secrets.yaml` — WiFi, API, and OTA credentials
- `esphome/udp_streamer.h` — C++ UDP audio streamer included by each device config

### UDP packet format

```
[4 bytes room_id][N bytes PCM audio (16-bit LE mono 16 kHz)]
```

All devices stream to the same voice server IP on a single UDP port (default 9100).

### Flashing a device

```bash
# First flash (USB)
esphome run esphome/atom-echo-voice.yaml

# Subsequent flashes (OTA, if on the same network)
esphome run esphome/atom-echo-bedroom.yaml
```

Each YAML config contains:
- WiFi + MQTT connection (birth/will messages for online/offline status)
- I2S PDM microphone on GPIO23
- SK6812 RGB LED on GPIO27 (status feedback)
- Button on GPIO39 (push-to-talk)
- LED color control via MQTT subscription
- Button press/release publishing via MQTT

### LED states

The voice service controls each device's LED via MQTT:

| State | Color | Meaning |
|-------|-------|---------|
| `listening` | Dim green | Idle, waiting for wake word |
| `wake` | Bright blue | Wake word detected |
| `capturing` | Bright green | Recording speech |
| `processing` | Yellow | Transcribing / waiting for LLM |
| `error` | Red | STT or processing failure |
| `off` | Off | LED disabled |

## Voice Service Configuration

All config is set via environment variables in `.env`:

### Core settings

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICE_UDP_PORT` | `9100` | UDP port for incoming audio streams |
| `VOICE_ROOMS` | — | Room name-to-ID mapping. Format: `name=id,name=id` (e.g. `office=offi,kitchen=ktch`) |
| `VOICE_ROOM_SPEAKERS` | — | Room ID to Sonos speaker alias mapping. Format: `room_id:speaker,room_id:speaker` (e.g. `offi:office,ktch:kitchen_dining`). Use `+` for multiple speakers per room. |

### Wake word settings

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICE_PORCUPINE_KEY` | — | Picovoice access key (required) |
| `VOICE_PORCUPINE_MODEL` | — | Path to `.ppn` keyword model file |
| `VOICE_WAKE_COOLDOWN` | `2.0` | Seconds to ignore wake word after a detection (prevents double-triggers) |

### VAD settings

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICE_VAD_SILENCE_MS` | `1000` | Milliseconds of silence before ending capture |
| `VOICE_VAD_MAX_COMMAND_MS` | `10000` | Maximum command duration in milliseconds |

### STT settings

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICE_STT_PROVIDER` | `groq` | Speech-to-text provider |
| `VOICE_STT_API_KEY` | — | API key for the STT provider |
| `VOICE_STT_MODEL` | `whisper-large-v3` | Whisper model to use |
| `VOICE_STT_LANGUAGE` | `en` | Language code for transcription |

## Voice Intent Agent

The voice-intent-agent subscribes to `voice.command` MQTT events and uses an LLM with tool calling to interpret commands. It operates as a persona named "Higgins."

### LLM integration

- Uses the primary LLM provider (with fallback if configured via `LLM_FALLBACK_*`)
- System prompt instructs the LLM to distinguish commands (use tools) from questions (answer conversationally)
- All LLM text output is formatted for spoken TTS (spelled-out numbers, expanded abbreviations, no markdown, no URLs)
- For general knowledge questions, Perplexity is used as a web-search fallback (skipped for queries about weather, lights, sensors, and other local data)
- Responses are targeted to the room's Sonos speaker via `VOICE_ROOM_SPEAKERS` mapping
- Before TTS synthesis, the sonos-gateway runs a fast LLM normalization pass to expand any remaining abbreviations (mph → miles per hour, F → Fahrenheit, etc.) and spell out numbers

### Available tools

| Tool | Description |
|------|-------------|
| `announce` | Broadcast a spoken message on Sonos (optionally targeted to specific speakers) |
| `mute_announcements` | Temporarily mute all Sonos announcements for N minutes |
| `unmute_announcements` | Cancel any active mute |
| `lights_on` | Turn on a Caseta light by device ID |
| `lights_off` | Turn off a Caseta light by device ID |
| `lights_level` | Set a Caseta light to a specific brightness (0–100) |
| `activate_scene` | Activate a Caseta lighting scene (e.g. Bedtime, Daytime, Nighttime) |
| `trigger_briefing` | Trigger a briefing: morning, executive, or chime |
| `get_time_and_weather` | Get current time and outdoor temperature |
| `get_forecast` | Get today's or tomorrow's weather forecast |
| `household_command` | Common household announcements: dinner, bedtime, trash, dogs_out, dogs_in, answer_door, kids_upstairs, kids_kitchen |

### Command flow

1. User says wake word → voice-service captures speech → STT → publishes `voice.command`
2. voice-intent-agent receives command with `[Room: name]` prefix
3. LLM decides: tool call (command) or text response (question)
4. Tool calls dispatch via MQTT (e.g. `announce.request`, `lutron.command`)
5. Text responses are spoken on the room's Sonos speaker

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `homeagent/voice/command` | voice-service → voice-intent-agent | Transcribed voice command (`{room_id, room_name, text}`) |
| `homeagent/sonos/playback` | sonos-gateway → voice-service | Playback start/done events (triggers DEAF state) |
| `homeagent/voice/{room_id}/led` | voice-service → ESPHome device | LED color command |
| `homeagent/voice/{room_id}/button` | ESPHome device → voice-service | Button pressed/released (push-to-talk) |
| `homeagent/voice/{room_id}/status` | ESPHome device (retained) | Device birth (`online`) / will (`offline`) |
| `homeagent/voice/room_status` | voice-service → UI | Periodic room status for the dashboard |

## DEAF State (Feedback Suppression)

When the Sonos gateway plays audio, it publishes `sonos.playback_start` on `homeagent/sonos/playback`. The voice service receives this and transitions all rooms to a **DEAF** state — incoming audio frames are dropped to prevent the microphone from picking up its own announcements.

When the gateway publishes `sonos.playback_done`, the voice service resumes normal listening.

This is critical for avoiding feedback loops where the voice service triggers on its own spoken responses.

## Pre-recorded Acknowledgment

To provide immediate feedback while the LLM processes a command, the system uses pre-recorded offline WAV files:

| Key | Text | Purpose |
|-----|------|---------|
| `voice_prompt` | "How may I assist you?" | Wake word response prompt |
| `voice_ack` | "One moment." | Primary acknowledgment |
| `voice_ack_2` | "Let me check." | Alternate acknowledgment |
| `voice_ack_3` | "Working on it." | Alternate acknowledgment |
| `voice_reasoning` | "Let me think about that." | Reasoning escalation acknowledgment |
| `voice_cancelled` | "Never mind. Cancelled." | Pending action cancelled |
| `voice_error` | "I'm sorry, I had trouble with that request." | Error feedback |

These are generated ahead of time via `python scripts/generate_offline_audio.py` using ElevenLabs TTS settings from `.env`, and stored in `OFFLINE_AUDIO_DIR` (default: `assets/offline`).

## Pre-capture Buffer

After the prompt plays, the voice service keeps the last 1 second of audio in the buffer before starting capture. This prevents the first word of the user's command from being clipped if they start talking during or right after the prompt.

## Audio Processing Pipeline

Before sending captured audio to Whisper STT, the voice service runs three processing steps:

1. **Noise reduction** — spectral gating (`noisereduce` library, stationary mode) strips out background noise (HVAC, fans, hum) while preserving speech. ~46ms for 5 seconds of audio.
2. **Silence trimming** — leading and trailing silence is removed based on frame energy analysis (30ms frames, energy threshold 30, with 150ms padding on each side). Reduces wasted audio sent to Whisper.
3. **Peak normalization** — scales the audio so the loudest sample reaches ~80% of the int16 range. This dramatically improves STT accuracy for rooms with distant microphones or quiet speech.

Diagnostic logging (`audio_processed`) reports raw vs. final RMS, duration, and trimmed seconds for every command.

## Whisper Hallucination Filtering

Groq Whisper sometimes returns hallucinated text on silence or noise. The voice service filters out known hallucination phrases including: "thank you", "thanks for watching", "thanks for listening", "please subscribe", "see you next time", "bye", "how may i assist you", etc.

Additionally, audio with very low RMS energy (< 50) is skipped as a likely false wake.

## Troubleshooting

### Wake word not triggering

- Verify the Atom Echo is online: check `homeagent/voice/{room_id}/status` for `online`
- Confirm UDP audio is arriving: voice-service logs `frames_received` per room
- Check the wake word model path (`VOICE_PORCUPINE_MODEL` or `VOICE_WAKE_MODEL`)
- For Porcupine: verify your `VOICE_PORCUPINE_KEY` is valid
- Ensure `VOICE_WAKE_COOLDOWN` isn't too high (default 2s)

### False triggers

- Increase `VOICE_WAKE_COOLDOWN` to add a longer refractory period after each detection
- Check if Sonos playback is causing triggers — the DEAF state should prevent this, but verify `sonos.playback_start`/`sonos.playback_done` events are flowing
- The RMS energy check (threshold 50) filters out very quiet false wakes

### STT returning garbage

- Verify `VOICE_STT_API_KEY` is set and valid
- Check audio duration: very short captures (< 0.5s) often produce poor results
- The hallucination filter catches common Whisper artifacts, but unusual noise may still produce nonsensical transcriptions

### No Sonos response after command

- Verify `VOICE_ROOM_SPEAKERS` maps the room ID to a valid Sonos speaker alias
- Check that `sonos-gateway` is running and processing `announce.request` events
- Verify quiet hours are not suppressing the response
ocessed` log: if `raw_rms` is very low (under 200), the mic is too far away or the room is too noisy for usable capture
- The noise reduction and normalization pipeline helps significantly, but audio with RMS below 50 is rejected entirely
- The hallucination filter catches common Whisper artifacts, but unusual noise may still produce nonsensical transcriptions

### No Sonos response after command

- Verify `VOICE_ROOM_SPEAKERS` maps the room ID to a valid Sonos speaker alias
- Check that `sonos-gateway` is running and processing `announce.request` events
- Verify quiet hours are not suppressing the response
