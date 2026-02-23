# Voice Assistant Setup (Atom Echo + Home Assistant)

Home Assistant Core runs as a Docker container alongside the home-agent stack.
It acts as a **voice pipeline bridge only** — Atom Echo devices connect to it,
HA runs STT (via OpenAI Whisper API), and an automation forwards the transcribed
text to the home-agent MQTT bus.

## Architecture

```
Atom Echo (wake word: "Hey Jarvis")
    ↓ audio stream (WiFi)
Home Assistant (Docker)
    ↓ OpenAI Whisper API (STT)
    ↓ automation
MQTT: homeagent/voice/command
    ↓
Voice Agent (your Python service — future)
```

## Step 1: Start the HA container

```bash
docker compose -f deploy/docker-compose.yml up -d homeassistant
```

## Step 2: Complete HA onboarding

1. Open `http://<server-ip>:8123` in a browser
2. Create an admin account (this is local-only, not cloud)
3. Skip any suggested integrations

## Step 3: Configure MQTT in HA

HA should auto-discover your MQTT broker (same host network). If not:

1. Go to Settings → Devices & Services → Add Integration → MQTT
2. Broker: `127.0.0.1`, Port: `1883`
3. Leave username/password blank (matches your Mosquitto config)

## Step 4: Install ESPHome add-on

1. Go to Settings → Add-ons → Add-on Store
2. Search "ESPHome" → Install
3. Start the add-on

## Step 5: Flash Atom Echo with ESPHome

1. Plug Atom Echo into your Mac/PC via USB
2. Go to https://web.esphome.io/
3. Click Connect → select the USB device
4. Install the M5Stack Atom Echo voice assistant firmware
5. Configure WiFi when prompted
6. After boot, the device will appear in HA's ESPHome dashboard

## Step 6: Set up Voice Pipeline in HA

1. Go to Settings → Voice Assistants
2. Add a new assistant
3. STT engine: choose "OpenAI Whisper" (cloud) or install the local Whisper add-on
4. For OpenAI Whisper cloud:
   - Go to Settings → Devices & Services → Add Integration → OpenAI Conversation
   - Enter your OpenAI API key
5. Set the wake word to "Hey Jarvis" (or your preferred option)
6. Assign the Atom Echo device to this pipeline

## Step 7: Verify

1. Say "Hey Jarvis" near the Atom Echo
2. Say a command (e.g., "call the kids to dinner")
3. Check MQTT for the forwarded text:

```bash
mosquitto_sub -h 127.0.0.1 -t 'homeagent/voice/command' -v
```

## Step 8: Build the Voice Agent (future)

A `voice-agent` service will subscribe to `homeagent/voice/command` and:
- Send the text to your LLM to determine intent
- Execute actions (announce, control lights, etc.)
- Optionally respond via TTS on Sonos

## Custom wake word ("Higgins")

microWakeWord includes pre-trained models for:
- "OK Nabu", "Hey Jarvis", "Hey Mycroft", "Alexa"

For a custom wake word like "Higgins", you would need to train a model:
- https://github.com/kahrendt/microWakeWord
- Requires ~50+ audio samples of the wake word

## Notes

- HA Core uses ~200-300MB RAM
- The HA container shares host networking (same as your other services)
- HA's config lives in `ha-config/` — not in your `.env`
- The automation in `ha-config/automations.yaml` forwards voice text to MQTT
