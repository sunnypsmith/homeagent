# Scheduling

This project uses Postgres to store schedules in the `schedules` table. The `home-agent time-trigger` service loads them and publishes time events onto MQTT.

## Cron spec format

The DB field `spec` uses a 5-field cron string:

`min hour day month dow`

Examples:
- `0 7 * * mon-fri` (7:00am Monday–Friday)
- `0 20 * * *` (8:00pm daily)
- `0 9-20 * * sat,sun` (hourly from 9am–8pm on weekends)

## Seed core schedules

```bash
home-agent seed-schedules
```

## Trigger a morning briefing now (for testing)

```bash
home-agent trigger-morning-briefing
```

## Fixed announcements

Fixed announcements are schedules with:
- `event_type = time.cron.fixed_announcement`
- `data.text` containing the message to speak

### Add/update (upsert by name)

```bash
home-agent add-fixed-announcement --name kids_bedtime_2000 --at 20:00 --days "*" "Time for bed."
```

### List

```bash
home-agent list-fixed-announcements
home-agent list-fixed-announcements --enabled-only
```

### Runtime

To execute fixed announcements, run:
- `home-agent time-trigger`
- `home-agent fixed-announcement-agent`
- `home-agent sonos-gateway`

Quiet hours are enforced at `sonos-gateway`.

## Sunset scene (optional)

The `time-trigger` service can publish a Caséta scene command at local sunset each day.

Requirements:
- `WEATHER_PROVIDER=open_meteo`
- `WEATHER_LAT` / `WEATHER_LON` configured
- `home-agent time-trigger` running

Settings:

```bash
SUNSET_SCENE_ENABLED=true
SUNSET_SCENE_NAME=Nighttime
SUNSET_SCENE_OFFSET_MINUTES=0
```

Event published:
- topic: `homeagent/lutron/command`
- type: `lutron.command`
- data: `{"action":"scene","scene_name":"Nighttime"}`
