# Lutron Caséta setup

This project can control a Lutron Caséta Smart Bridge (LEAP) via the `caseta-agent` service.

## Install Caséta support

```bash
pip install -e ".[caseta]"
```

## Pair with the bridge (one-time)

Use `lap-pair` to generate TLS client certs:

```bash
lap-pair <BRIDGE_IP>
```

This will prompt you to press the small button on the back of the bridge.

By default, certs are written under:
- `~/.config/pylutron_caseta/`

You should get three files:
- `<BRIDGE_IP>-bridge.crt` (CA)
- `<BRIDGE_IP>.crt` (client cert)
- `<BRIDGE_IP>.key` (client key)

## Configure `.env`

```bash
CASETA_ENABLED=true
CASETA_HOST=10.1.2.116
CASETA_PORT=8081
CASETA_CA_CERT_PATH=~/.config/pylutron_caseta/10.1.2.116-bridge.crt
CASETA_CERT_PATH=~/.config/pylutron_caseta/10.1.2.116.crt
CASETA_KEY_PATH=~/.config/pylutron_caseta/10.1.2.116.key
```

Notes:
- Those paths must be valid **from the process that runs** `home-agent caseta-agent`.
- If you’re running via Docker Compose, it’s usually easiest to mount your cert directory into the container (e.g. `/certs`) and set:
  - `CASETA_CA_CERT_PATH=/certs/<BRIDGE_IP>-bridge.crt`
  - `CASETA_CERT_PATH=/certs/<BRIDGE_IP>.crt`
  - `CASETA_KEY_PATH=/certs/<BRIDGE_IP>.key`

## Run

```bash
home-agent caseta-agent
```

On startup, the agent publishes retained snapshot events:
- topic: `homeagent/lutron/event`
- type: `lutron.devices` (devices list)
- type: `lutron.scenes` (scenes list; Caséta scenes are virtual buttons)

## Scenes (virtual buttons)

You can list scenes directly via the LEAP CLI:

```bash
leap "<BRIDGE_IP>/virtualbutton"
```

Example:
- `Bedtime` is `/virtualbutton/1` (scene_id `1`)
- `Daytime` is `/virtualbutton/2` (scene_id `2`)

## Test control via MQTT

Publish a command event to:
- topic: `homeagent/lutron/command`
- type: `lutron.command`

Activate a scene by id:

```bash
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/command' -m '{
  "id":"test-scene-1",
  "ts":"2026-01-01T00:00:00Z",
  "source":"manual",
  "type":"lutron.command",
  "trace_id":"test-scene-1",
  "data":{"action":"scene","scene_id":"2"}
}'
```

Activate a scene by name:

```bash
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/command' -m '{
  "id":"test-scene-2",
  "ts":"2026-01-01T00:00:00Z",
  "source":"manual",
  "type":"lutron.command",
  "trace_id":"test-scene-2",
  "data":{"action":"scene","scene_name":"Daytime"}
}'
```

## Schedule a scene (Daytime example)

Weekdays at 6:00am:

```bash
home-agent add-caseta-scene --name caseta_daytime_weekday_0600 --at 06:00 --days mon-fri --scene-name Daytime
```

Weekends at 7:00am:

```bash
home-agent add-caseta-scene --name caseta_daytime_weekend_0700 --at 07:00 --days sat,sun --scene-name Daytime
```

## Sunset scene (optional)

Trigger a Caséta scene at local sunset using the `time-trigger` service.

Prereqs:
- Weather is configured (`WEATHER_LAT`, `WEATHER_LON`, `WEATHER_PROVIDER=open_meteo`)
- `home-agent time-trigger` is running

Configure in `.env`:

```bash
SUNSET_SCENE_ENABLED=true
SUNSET_SCENE_NAME=Nighttime
SUNSET_SCENE_OFFSET_MINUTES=0
```

Notes:
- Uses Open‑Meteo for sunrise/sunset times.
- `SUNSET_SCENE_OFFSET_MINUTES` can be negative or positive.
