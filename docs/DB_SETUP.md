# Database setup (TimescaleDB)

This project stores all MQTT events in a Timescale hypertable for querying/trends.

## One-time container init (already done on your server)

- Create database (example `homeagent`)
- Enable Timescale extension in that database

## Migrations

SQL migrations live in `db/migrations/` and should be applied in filename order.

### Apply migrations (example using your running container)

Assuming:
- DB container name: `home-db`
- user: `homeagent`
- db: `homeagent`

Run:

```bash
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0001_timescaledb.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0002_events.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0003_schedules.sql
```

## Verify

```bash
docker exec -it home-db psql -U homeagent -d homeagent -c "\dt"
docker exec -it home-db psql -U homeagent -d homeagent -c "SELECT extname, extversion FROM pg_extension;"
```

## Schedules

Schedules are stored in the `schedules` table and consumed by `home-agent time-trigger`.

Useful commands:

```bash
home-agent seed-schedules
home-agent add-fixed-announcement --name kids_bedtime_2000 --at 20:00 --days "*" "Time for bed."
home-agent list-fixed-announcements
```

