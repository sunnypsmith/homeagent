# Sonos setup

## Install Sonos support

```bash
pip install -e ".[sonos]"
```

## Discover speakers (recommended)

This uses SSDP discovery (multicast):

```bash
python3 scripts/sonos_discover.py
```

Then write your selection into `.env`:

```bash
python3 scripts/sonos_discover.py --write
```

## Discover speakers by scanning a subnet

Use this when multicast/SSDP is blocked on your network:

```bash
python3 scripts/sonos_discover.py --subnet 192.168.1.0/24
python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --write
```

Tuning:

```bash
python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --timeout 2 --max-workers 256
```

## What it writes

The script updates **only**:
- `SONOS_ANNOUNCE_TARGETS=<comma-delimited IPs>`

It does **not** print or modify other `.env` keys.

## Recommended volume

In your `.env`, you can set a default announcement volume:

```bash
SONOS_DEFAULT_VOLUME=50
```

## Test TTS -> Sonos end-to-end

Once you’ve set:
- `SONOS_ANNOUNCE_TARGETS=...`
- `ELEVENLABS_API_KEY=...`

You can run:

```bash
home-agent tts-test "Hello from the home agent"
```

## Quiet hours (hard-enforced)

The Sonos gateway (`home-agent sonos-gateway`) can **suppress** announcements during quiet hours.

Configure in `.env`:

```bash
QUIET_HOURS_ENABLED=true
QUIET_HOURS_WEEKDAY_START=21:00
QUIET_HOURS_WEEKDAY_END=05:50
QUIET_HOURS_WEEKEND_START=21:00
QUIET_HOURS_WEEKEND_END=06:50
```

When suppressed, the gateway publishes a visibility event:
- topic: `homeagent/announce/suppressed`
- type: `announce.suppressed`

## MQTT announce event format (strict envelope)

The Sonos gateway (`home-agent sonos-gateway`) consumes:

- topic: `homeagent/announce/request`
- payload: JSON envelope:

```json
{
  "id": "a_unique_id",
  "ts": "2026-01-24T12:00:00Z",
  "source": "manual",
  "type": "announce.request",
  "trace_id": "trace_1",
  "data": {
    "text": "Hello from MQTT",
    "concurrency": 8
  }
}
```

## Troubleshooting

- **Nothing found (SSDP)**:
  - Ensure you’re on the same LAN/VLAN as Sonos
  - Ensure multicast/UPnP is allowed
  - Try subnet mode

- **Nothing found (subnet mode)**:
  - Verify the CIDR is correct (most home networks are `/24`)
  - Some networks block device-description requests; try increasing `--timeout`
  - Reduce `--max-workers` if your router struggles
