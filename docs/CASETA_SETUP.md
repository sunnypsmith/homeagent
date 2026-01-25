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
CASETA_CA_CERT_PATH=/home/ml/.config/pylutron_caseta/10.1.2.116-bridge.crt
CASETA_CERT_PATH=/home/ml/.config/pylutron_caseta/10.1.2.116.crt
CASETA_KEY_PATH=/home/ml/.config/pylutron_caseta/10.1.2.116.key
```

## Run

```bash
home-agent caseta-agent
```

On startup, the agent publishes a retained snapshot event:
- topic: `homeagent/lutron/event`
- type: `lutron.devices`
- data: `{ count, devices: [...] }`

## Find device IDs

Subscribe and look for the device name you want:

```bash
mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/event' -v
```

Example names look like:
- `Office_Main Lights`
- `Front Porch_Pendants`

The corresponding `device_id` is what you use for commands/automations.

## Test control via MQTT

Publish a command event to:
- topic: `homeagent/lutron/command`
- type: `lutron.command`

Turn on:

```bash
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/command' -m '{
  "id":"test-on-1",
  "ts":"2026-01-01T00:00:00Z",
  "source":"manual",
  "type":"lutron.command",
  "trace_id":"test-on-1",
  "data":{"action":"on","device_id":"29"}
}'
```

Turn off:

```bash
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/command' -m '{
  "id":"test-off-1",
  "ts":"2026-01-01T00:00:00Z",
  "source":"manual",
  "type":"lutron.command",
  "trace_id":"test-off-1",
  "data":{"action":"off","device_id":"29"}
}'
```

Set dim level (0-100):

```bash
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/command' -m '{
  "id":"test-level-1",
  "ts":"2026-01-01T00:00:00Z",
  "source":"manual",
  "type":"lutron.command",
  "trace_id":"test-level-1",
  "data":{"action":"level","device_id":"29","level":50}
}'
```

The agent will publish:
- `lutron.command.ack` on success
- `lutron.command.error` on failure

