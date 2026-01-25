# Camera → lighting automation

The `camera-lighting-agent` listens for camera events and triggers one or more Caséta devices.

## Prereqs

You must have these running:
- `home-agent camect-agent` (publishes `homeagent/camera/event`)
- `home-agent caseta-agent` (consumes `homeagent/lutron/command`)

You also need weather coordinates for sunrise/sunset checks:
- `WEATHER_LAT`, `WEATHER_LON`

## Configure `.env`

```bash
CAMERA_LIGHTING_ENABLED=true
CAMERA_LIGHTING_ONLY_DARK=true

# Match one or more cameras (comma/semicolon list)
CAMERA_LIGHTING_CAMERA_NAME=Front_Door,Front_Garage

# Match one or more object labels (comma/semicolon list)
# Common labels include: vehicle, car, truck, van, suv, person, people, human
CAMERA_LIGHTING_DETECTED_OBJ=vehicle,person

# Trigger one or more Caséta devices (comma/semicolon list)
CAMERA_LIGHTING_CASETA_DEVICE_ID=7,47

# Turn off after N seconds since the most recent matching event
CAMERA_LIGHTING_DURATION_SECONDS=600

# Avoid spamming repeated ON commands (per device); still extends the timer
CAMERA_LIGHTING_MIN_RETRIGGER_SECONDS=30
```

## Behavior

- **Dark-only**:
  - If `CAMERA_LIGHTING_ONLY_DARK=true`, the agent uses Open-Meteo sunrise/sunset and only triggers when it is “dark”.
- **Matching**:
  - Camera names are matched case-insensitively against `data.camera_name`.
  - `detected_obj` is normalized from the Camect event payload (string or list of strings).
- **Timers**:
  - On a matching event, the agent sends `lutron.command` **on** to each device id.
  - It schedules an **off** for each device after `CAMERA_LIGHTING_DURATION_SECONDS`.
  - New matching events cancel/reschedule the off task (keeps the lights on longer).
- **Throttling**:
  - If another event happens within `CAMERA_LIGHTING_MIN_RETRIGGER_SECONDS`, the agent will *not* re-send **on** (per device),
    but it will still extend the off timer.

## Run

```bash
home-agent camera-lighting-agent
```

