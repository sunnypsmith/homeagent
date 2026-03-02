"""Watchdog service: monitors all other services via MQTT heartbeats and errors.

- Subscribes to service.error and service.heartbeat topics
- Logs errors to DB (via event-recorder, which records all MQTT events)
- Announces critical errors via announce.request
- Detects missing heartbeats (service down)
- Attempts a single restart for crashed/erroring services
- Escalates to email if restart doesn't fix the problem
- Publishes aggregate health status for the UI
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger

_ANNOUNCE_THROTTLE_SECONDS = 600  # 10 min between repeated announcements per service
_HEARTBEAT_STALE_SECONDS = 90  # declare service down after missing heartbeats for this long
_RESTART_COOLDOWN_SECONDS = 300  # don't restart same service more than once per 5 min


@dataclass
class _ServiceState:
    last_heartbeat_ts: float = 0.0
    last_heartbeat_pid: int = 0
    last_error_ts: float = 0.0
    last_error_context: str = ""
    last_announce_ts: float = 0.0
    last_restart_ts: float = 0.0
    restart_attempted: bool = False
    error_count: int = 0
    recovered: bool = False


async def run_watchdog() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="watchdog")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-watchdog",
    )
    await mqttc.connect()
    log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    base = settings.mqtt.base_topic
    error_topic = "%s/service/error" % base
    heartbeat_topic = "%s/service/heartbeat" % base
    announce_topic = "%s/announce/request" % base
    health_topic = "%s/watchdog/health" % base

    mqttc.subscribe("%s/service/#" % base)
    log.info("subscribed", pattern="%s/service/#" % base)

    states: Dict[str, _ServiceState] = {}
    tmux_map = _parse_tmux_map(settings.watchdog_tmux_map)
    log.info("tmux_map", entries=len(tmux_map), services=list(tmux_map.keys()) if tmux_map else [])

    def _get(service: str) -> _ServiceState:
        if service not in states:
            states[service] = _ServiceState()
        return states[service]

    def _announce(text: str) -> None:
        evt = make_event(
            source="watchdog",
            typ="announce.request",
            data={"text": text},
        )
        mqttc.publish_json(announce_topic, evt)

    def _should_announce(st: _ServiceState) -> bool:
        return (time.monotonic() - st.last_announce_ts) > _ANNOUNCE_THROTTLE_SECONDS

    def _try_restart(service: str, st: _ServiceState) -> bool:
        """Attempt to restart a service via tmux. Returns True if attempted."""
        now = time.monotonic()
        if (now - st.last_restart_ts) < _RESTART_COOLDOWN_SECONDS:
            return False
        if service not in tmux_map:
            log.warning("no_tmux_target", service=service, hint="Set WATCHDOG_TMUX_MAP to enable restarts")
            return False

        target = tmux_map[service]
        cmd = _build_restart_cmd(service)
        log.warning("restart_attempt", service=service, tmux_target=target)
        st.last_restart_ts = now
        st.restart_attempted = True
        st.recovered = False

        try:
            os.system('tmux send-keys -t %s C-c 2>/dev/null' % target)
            time.sleep(2)
            os.system('tmux send-keys -t %s \'%s\' Enter 2>/dev/null' % (target, cmd))
            log.info("restart_sent", service=service, tmux_target=target)
            return True
        except Exception as e:
            log.exception("restart_failed", service=service, error=str(e))
            return False

    async def _health_loop() -> None:
        """Periodically publish aggregate health and check for stale heartbeats."""
        while True:
            await asyncio.sleep(30.0)
            now = time.monotonic()
            health: Dict[str, Any] = {}

            for svc, st in dict(states).items():
                hb_age = round(now - st.last_heartbeat_ts, 1) if st.last_heartbeat_ts > 0 else None
                is_stale = (hb_age is not None and hb_age > _HEARTBEAT_STALE_SECONDS)
                status = "ok"
                if is_stale:
                    status = "down"
                elif st.error_count > 0 and (now - st.last_error_ts) < 120:
                    status = "error"

                health[svc] = {
                    "status": status,
                    "heartbeat_age_seconds": hb_age,
                    "error_count": st.error_count,
                    "restart_attempted": st.restart_attempted,
                    "pid": st.last_heartbeat_pid,
                }

                # Stale heartbeat detection: service may have crashed
                if is_stale and st.last_heartbeat_ts > 0:
                    if not st.restart_attempted:
                        log.error("service_down", service=svc, heartbeat_age=hb_age)
                        if _should_announce(st):
                            _announce(
                                "Your attention please. The %s service appears to be down. "
                                "Attempting restart." % _spoken_name(svc)
                            )
                            st.last_announce_ts = now
                        _try_restart(svc, st)
                    elif not st.recovered:
                        # Already tried restart — check if it's still down
                        if (now - st.last_restart_ts) > _RESTART_COOLDOWN_SECONDS:
                            log.error("service_still_down", service=svc, heartbeat_age=hb_age)
                            if _should_announce(st):
                                _announce(
                                    "Warning. The %s service is still down after restart attempt. "
                                    "Manual intervention required." % _spoken_name(svc)
                                )
                                st.last_announce_ts = now

            # Publish aggregate health
            evt = make_event(
                source="watchdog",
                typ="watchdog.health",
                data={"services": health},
            )
            mqttc.publish_json(health_topic, evt, retain=True)
            log.info(
                "health_check",
                services=len(states),
                down=[s for s, h in health.items() if h["status"] == "down"],
                errors=[s for s, h in health.items() if h["status"] == "error"],
            )

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(60.0)
            log.info(
                "status",
                mqtt_connected=mqttc.is_connected,
                tracked_services=len(states),
                total_errors=sum(s.error_count for s in states.values()),
                restarts_attempted=sum(1 for s in states.values() if s.restart_attempted),
            )

    health_task = asyncio.create_task(_health_loop())
    status_task = asyncio.create_task(_status_loop())

    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload: Dict[str, Any] = msg.json()
            except Exception:
                continue

            typ = payload.get("type", "")
            data = payload.get("data") or {}
            source = payload.get("source", "")
            service = data.get("service") or source

            if not service or service == "watchdog":
                continue

            st = _get(service)
            now = time.monotonic()

            if typ == "service.heartbeat":
                was_stale = (
                    st.last_heartbeat_ts > 0
                    and (now - st.last_heartbeat_ts) > _HEARTBEAT_STALE_SECONDS
                )
                st.last_heartbeat_ts = now
                st.last_heartbeat_pid = data.get("pid", 0)

                # Recovery detection
                if was_stale or (st.restart_attempted and not st.recovered):
                    st.recovered = True
                    log.info("service_recovered", service=service)
                    _announce(
                        "The %s service has recovered and is running normally." % _spoken_name(service)
                    )

            elif typ == "service.error":
                st.last_error_ts = now
                st.last_error_context = data.get("context", "")
                st.error_count += 1
                error_type = data.get("error_type", "Unknown")
                error_msg = data.get("error", "")[:200]
                log.error(
                    "service_error",
                    service=service,
                    context=st.last_error_context,
                    error_type=error_type,
                    error=error_msg,
                    total_errors=st.error_count,
                )

                if _should_announce(st):
                    _announce(
                        "Your attention please. There is an error in the %s service. "
                        "%s: %s." % (_spoken_name(service), error_type, error_msg[:100])
                    )
                    st.last_announce_ts = now

                # Try restart on first error (if not recently restarted)
                if not st.restart_attempted:
                    _try_restart(service, st)

    finally:
        health_task.cancel()
        status_task.cancel()
        await mqttc.close()


def _spoken_name(service: str) -> str:
    """Make service names more announcement-friendly."""
    return service.replace("-", " ").replace("_", " ")


def _parse_tmux_map(raw: str) -> Dict[str, str]:
    """Parse 'service:window.pane,service:window.pane' into a dict."""
    out: Dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        svc, target = part.split(":", 1)
        svc = svc.strip()
        target = target.strip()
        if svc and target:
            out[svc] = target
    return out


def _build_restart_cmd(service: str) -> str:
    return 'home-agent %s 2>&1 | sed -u "s/^/[%s] /"' % (service, service.split("-")[0])


def main() -> int:
    asyncio.run(run_watchdog())
    return 0
