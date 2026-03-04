# Watchdog Service

## Overview

The watchdog monitors all home-agent services via MQTT heartbeats and error events. When a service goes down or reports errors, the watchdog:

1. Announces the failure on Sonos
2. Attempts a single automatic restart via tmux
3. Publishes aggregate health status for the UI dashboard

```bash
home-agent watchdog
```

## How It Works

### ErrorReporter Integration

Every service uses `ErrorReporter` from `home_agent.bus.error_reporter` to:
- Publish **heartbeats** every 30 seconds on `homeagent/service/heartbeat`
- Publish **error events** on `homeagent/service/error` when exceptions occur

The watchdog subscribes to `homeagent/service/#` to receive all heartbeat and error messages.

### What It Monitors

| Condition | Threshold | Action |
|-----------|-----------|--------|
| Heartbeat stale | 90 seconds since last heartbeat | Declare service "down" |
| Error event | Any `service.error` message | Log, announce, attempt restart |

### Actions on Failure

1. **Announce on Sonos**: publishes an `announce.request` describing which service failed (throttled to once per 10 minutes per service to avoid spam)
2. **Attempt tmux restart**: if the service is in `WATCHDOG_TMUX_MAP`, sends Ctrl-C to the tmux pane and re-runs the service command. Only attempts one restart per 5-minute cooldown period per service.
3. **Recovery detection**: when a heartbeat arrives from a previously-down service, the watchdog announces recovery

## Configuration

### `WATCHDOG_TMUX_MAP`

Maps service names to tmux `session:window.pane` targets so the watchdog can restart crashed services.

Format: `service_name:session:window.pane,service_name:session:window.pane,...`

Example:

```bash
WATCHDOG_TMUX_MAP=sonos-gateway:homeagent:0.1,camect-agent:homeagent:2.0,time-trigger:homeagent:0.0
```

This means:
- `sonos-gateway` runs in tmux session `homeagent`, window 0, pane 1
- `camect-agent` runs in tmux session `homeagent`, window 2, pane 0
- `time-trigger` runs in tmux session `homeagent`, window 0, pane 0

The tmux layout is defined in `scripts/tmux_homeagent.sh`.

If `WATCHDOG_TMUX_MAP` is not set, the watchdog will still monitor and announce failures but cannot restart services.

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `homeagent/service/heartbeat` | All services → watchdog | Heartbeat with service name and PID (every 30s) |
| `homeagent/service/error` | All services → watchdog | Error event with context and traceback |
| `homeagent/watchdog/health` | watchdog → UI (retained) | Aggregate health status of all monitored services |

### Health payload

The watchdog publishes `watchdog.health` every 30 seconds on `homeagent/watchdog/health` (retained). The payload contains per-service status:

- **`ok`**: heartbeat received within the last 90 seconds, no recent errors
- **`error`**: service reported an error recently
- **`down`**: no heartbeat for more than 90 seconds

The UI gateway's `/status` dashboard consumes this to display real-time service health.

## Constants

| Name | Value | Description |
|------|-------|-------------|
| Announce throttle | 600s (10 min) | Minimum time between repeated Sonos announcements for the same service |
| Heartbeat stale | 90s | Time after which a missing heartbeat means the service is down |
| Restart cooldown | 300s (5 min) | Minimum time between restart attempts for the same service |
