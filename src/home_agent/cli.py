from __future__ import annotations

import typer

from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging
from home_agent.integrations.audio_host import AudioHost
from home_agent.integrations.sonos_playback import SonosPlayback
from home_agent.integrations.tts_elevenlabs import ElevenLabsTTSClient
from home_agent.main import main
from home_agent.services.event_recorder import main as event_recorder_main
from home_agent.services.camect_agent import main as camect_agent_main
from home_agent.services.camera_lighting_agent import main as camera_lighting_agent_main
from home_agent.services.caseta_agent import main as caseta_agent_main
from home_agent.services.fixed_announcement_agent import main as fixed_announcement_agent_main
from home_agent.services.hourly_chime_agent import main as hourly_chime_agent_main
from home_agent.services.hourly_house_check_agent import main as hourly_house_check_agent_main
from home_agent.services.morning_briefing_agent import main as morning_briefing_agent_main
from home_agent.services.seed_schedules import SeedSchedule, seed_default_schedules, upsert_schedules
from home_agent.services.sonos_gateway import main as sonos_gateway_main
from home_agent.services.time_trigger import main as time_trigger_main
from home_agent.services.wakeup_agent import main as wakeup_agent_main

app = typer.Typer(no_args_is_help=True)


@app.command()
def run() -> None:
    """Run the home agent process."""
    raise SystemExit(main())


@app.command("sonos-discover")
def sonos_discover(
    env_file: str = typer.Option(".env", "--env-file", help="Env file to update (default: .env)"),
    timeout: int = typer.Option(6, "--timeout", help="Discovery timeout seconds"),
    subnet: str = typer.Option(None, "--subnet", help="Optional CIDR to scan by IP (e.g. 192.168.1.0/24)"),
    max_workers: int = typer.Option(128, "--max-workers", help="Concurrency for subnet scanning"),
    write: bool = typer.Option(False, "--write", help="Actually update the env file"),
) -> None:
    """
    Discover Sonos devices on your LAN and (optionally) write SONOS_SPEAKER_IP to the env file.
    """
    # Keep the logic in a standalone script too (scripts/sonos_discover.py).
    from subprocess import run  # nosec
    import sys

    cmd = [
        sys.executable,
        "scripts/sonos_discover.py",
        "--env-file",
        env_file,
        "--timeout",
        str(timeout),
    ]
    if subnet:
        cmd.extend(["--subnet", subnet, "--max-workers", str(max_workers)])
    if write:
        cmd.append("--write")
    raise SystemExit(run(cmd).returncode)


@app.command("tts-test")
def tts_test(
    text: str = typer.Argument(..., help="Text to speak"),
    voice_id: str = typer.Option(None, "--voice-id", help="Override ElevenLabs voice id"),
    volume: int = typer.Option(None, "--volume", help="Override Sonos volume"),
    concurrency: int = typer.Option(None, "--concurrency", help="Parallel Sonos playback concurrency"),
) -> None:
    """
    End-to-end test: ElevenLabs TTS -> host audio -> play on SONOS_ANNOUNCE_TARGETS.
    """
    settings = AppSettings()
    configure_logging(settings.log_level)

    targets = settings.sonos.announce_target_ips
    if not targets:
        raise typer.BadParameter("Set SONOS_ANNOUNCE_TARGETS in .env first")

    tts = ElevenLabsTTSClient(
        api_key=settings.elevenlabs.api_key,
        voice_id=settings.elevenlabs.voice_id,
        base_url=settings.elevenlabs.base_url,
        timeout_seconds=settings.elevenlabs.timeout_seconds,
    )
    host = AudioHost()
    player = SonosPlayback(speaker_ips=targets, default_volume=settings.sonos.default_volume)

    import asyncio

    async def run_once() -> None:
        audio = await tts.synthesize(text=text, voice_id=voice_id)
        hosted = host.host_bytes(
            data=audio.data,
            filename="tts_test.%s" % audio.suggested_ext,
            content_type=audio.content_type,
            route_to_ip=targets[0],
        )
        await player.play_url(
            url=hosted.url,
            volume=volume,
            title="Home Agent TTS Test",
            concurrency=concurrency if concurrency is not None else settings.sonos.announce_concurrency,
        )

    asyncio.run(run_once())

@app.command("sonos-gateway")
def sonos_gateway() -> None:
    """Run Sonos/TTS gateway (MQTT -> play announcements)."""
    raise SystemExit(sonos_gateway_main())

@app.command("event-recorder")
def event_recorder() -> None:
    """Run event recorder (MQTT -> TimescaleDB events table)."""
    raise SystemExit(event_recorder_main())

@app.command("time-trigger")
def time_trigger() -> None:
    """Run time trigger service (DB schedules -> MQTT time events)."""
    raise SystemExit(time_trigger_main())

@app.command("wakeup-agent")
def wakeup_agent() -> None:
    """Run wakeup call agent (time event -> announce.request)."""
    raise SystemExit(wakeup_agent_main())

@app.command("morning-briefing-agent")
def morning_briefing_agent() -> None:
    """Run morning briefing agent (time event -> LLM -> announce.request)."""
    raise SystemExit(morning_briefing_agent_main())

@app.command("hourly-chime-agent")
def hourly_chime_agent() -> None:
    """Run hourly chime agent (time event -> announce.request)."""
    raise SystemExit(hourly_chime_agent_main())

@app.command("hourly-house-check-agent")
def hourly_house_check_agent() -> None:
    """Run hourly house check agent (stub) (time event -> house.check.request)."""
    raise SystemExit(hourly_house_check_agent_main())

@app.command("camect-agent")
def camect_agent() -> None:
    """Run Camect camera events agent (Camect -> MQTT -> announce.request)."""
    raise SystemExit(camect_agent_main())

@app.command("caseta-agent")
def caseta_agent() -> None:
    """Run Lutron Caséta agent (LEAP bridge -> MQTT commands/events)."""
    raise SystemExit(caseta_agent_main())

@app.command("camera-lighting-agent")
def camera_lighting_agent() -> None:
    """Run camera->lighting automation (Camect camera events -> Caséta commands)."""
    raise SystemExit(camera_lighting_agent_main())

@app.command("fixed-announcement-agent")
def fixed_announcement_agent() -> None:
    """Run fixed announcement agent (scheduled time event -> announce.request)."""
    raise SystemExit(fixed_announcement_agent_main())

@app.command("seed-schedules")
def seed_schedules(
    timezone: str = typer.Option(
        None,
        "--timezone",
        help="Timezone for seeded schedules (default: HOME_AGENT_TIMEZONE from .env)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be seeded, but do not write to DB"
    ),
) -> None:
    """Seed default schedules into Postgres (idempotent/upsert)."""
    settings = AppSettings()
    configure_logging(settings.log_level)

    schedules = seed_default_schedules(timezone=timezone, dry_run=dry_run)

    if dry_run:
        for s in schedules:
            typer.echo(
                "%s | %s | %s | %s | %s | %s | %s"
                % (
                    s.name,
                    "enabled" if s.enabled else "disabled",
                    s.kind,
                    s.timezone,
                    s.spec,
                    s.mqtt_topic,
                    s.event_type,
                )
            )
        raise SystemExit(0)

    typer.echo("Seeded %d schedules." % len(schedules))

@app.command("add-fixed-announcement")
def add_fixed_announcement(
    name: str = typer.Option(..., "--name", help="Unique schedule name (used for upsert)"),
    at: str = typer.Option(..., "--at", help="Local time HH:MM (in HOME_AGENT_TIMEZONE)"),
    days: str = typer.Option(
        "*",
        "--days",
        help="Cron day-of-week field (e.g. '*', 'mon-fri', 'sat,sun')",
    ),
    text: str = typer.Argument(..., help="Announcement text to speak"),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="Enable/disable the schedule"),
    volume: int = typer.Option(None, "--volume", help="Optional Sonos volume override"),
    targets: str = typer.Option(
        None, "--targets", help="Optional comma-delimited Sonos IPs override (otherwise default targets)"
    ),
    concurrency: int = typer.Option(None, "--concurrency", help="Optional Sonos playback concurrency override"),
) -> None:
    """Create/update a scheduled fixed announcement in Postgres."""
    settings = AppSettings()
    configure_logging(settings.log_level)

    parts = (at or "").strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise typer.BadParameter("--at must be HH:MM", param_hint="--at")
    hh = int(parts[0])
    mm = int(parts[1])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise typer.BadParameter("--at must be valid HH:MM", param_hint="--at")

    dow = (days or "").strip() or "*"
    spec = "%d %d * * %s" % (mm, hh, dow)

    data = {"text": text}
    if volume is not None:
        data["volume"] = int(volume)
    if concurrency is not None:
        data["concurrency"] = int(concurrency)
    if targets is not None:
        t = [p.strip() for p in str(targets).split(",")]
        t = [p for p in t if p]
        if t:
            data["targets"] = t

    s = SeedSchedule(
        name=name,
        enabled=bool(enabled),
        kind="cron",
        timezone=settings.timezone,
        spec=spec,
        mqtt_topic="%s/time/cron/fixed_announcement" % settings.mqtt.base_topic,
        event_type="time.cron.fixed_announcement",
        data=data,
    )

    import psycopg

    conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    try:
        upsert_schedules(conn, [s])
    finally:
        try:
            conn.close()
        except Exception:
            pass

    typer.echo("Upserted fixed announcement schedule: %s" % name)

@app.command("list-fixed-announcements")
def list_fixed_announcements(
    show_disabled: bool = typer.Option(
        True,
        "--show-disabled/--enabled-only",
        help="Include disabled schedules (default: show all)",
    ),
) -> None:
    """List scheduled fixed announcements from Postgres."""
    settings = AppSettings()
    configure_logging(settings.log_level)

    import psycopg

    conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, enabled, timezone, spec, data
                FROM schedules
                WHERE event_type = 'time.cron.fixed_announcement'
                ORDER BY name ASC
                """
            )
            rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        typer.echo("No fixed announcements found.")
        raise SystemExit(0)

    for r in rows:
        name = str(r[0])
        enabled = bool(r[1])
        if (not show_disabled) and (not enabled):
            continue
        tz = str(r[2])
        spec = str(r[3])
        data = r[4] if isinstance(r[4], dict) else {}
        text = str((data or {}).get("text") or "").strip()
        typer.echo(
            "%s | %s | %s | %s | %s"
            % (
                name,
                "enabled" if enabled else "disabled",
                tz,
                spec,
                text,
            )
        )


if __name__ == "__main__":
    app()

