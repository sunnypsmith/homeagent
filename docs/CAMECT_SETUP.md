# Camect setup

This project can consume Camect AI alerts and publish:
- `homeagent/camera/event` (for recording + downstream automations)
- `homeagent/announce/request` (optional, for spoken announcements)

## Install Camect support

```bash
pip install -e ".[camect]"
```

## Prereq (Camect hub)

- Ensure you can log into the hub UI and have accepted the Camect terms:
  - `https://local.home.camect.com/`

## Configure `.env`

Required:

```bash
CAMECT_ENABLED=true
CAMECT_HOST=10.1.2.150:443
CAMECT_USERNAME=admin
CAMECT_PASSWORD=YOUR_PASSWORD
```

### Choose cameras + filters

You have two options:

#### Option A: Global camera list + one filter token

```bash
CAMECT_CAMERA_NAMES=Front_Door,Front_Garage
CAMECT_EVENT_FILTER=vehicle
```

#### Option B (recommended): Per-camera rules (supports token lists)

Rules override `CAMECT_CAMERA_NAMES` + `CAMECT_EVENT_FILTER`.

Use quotes (recommended) so `;` and commas are preserved:

```bash
CAMECT_CAMERA_RULES="Front_Garage:vehicle,car,truck,van,suv;Front_Door:person,people,human"
```

Notes:
- `Front_Garage:` (empty token) means “accept any event from this camera”.
- Tokens are matched against Camect’s `detected_obj` when present (preferred), otherwise against text fields.

### Tuning + debug

```bash
CAMECT_THROTTLE_SECONDS=120
CAMECT_ANNOUNCE_TEMPLATE=Your attention please. A {kind} was detected at {camera}.

# Debug/observability:
CAMECT_DEBUG=false
CAMECT_STATUS_INTERVAL_SECONDS=60
CAMECT_STALE_WARNING_SECONDS=300
```

What the status means:
- `received_total`: events received from Camect (listener callback)
- `matched_total`: events that passed your camera + filter rules
- `announced_total`: `announce.request` events published
- `last_callback_age_seconds`: seconds since Camect last delivered an event callback (ping/pong does not count)

## Run

```bash
home-agent camect-agent
```

## Troubleshooting

- **No events, but ping/pong continues**:
  - Ping/pong only proves the websocket is alive; it does not prove events are flowing.
  - Watch `camect_status`:
    - if `received_total` stays `0`, Camect is not delivering events (hub-side or auth issue).
    - if `received_total` increases but `matched_total` stays `0`, your filter/rules are excluding them.

- **Passwords with `!`**:
  - Shell history expansion can mangle `!` depending on how you launch the process.
  - Prefer passwords without `!`, or ensure the process is launched in a way that doesn’t expand it.

