"""Shared error reporting and heartbeat publishing for all services.

Usage:
    reporter = ErrorReporter(mqttc=mqttc, service="sonos-gateway")
    reporter.start_heartbeat(interval_seconds=30)

    # In exception handlers:
    reporter.report_error("announce_failed", exception)
"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
from typing import Optional

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient


class ErrorReporter:
    def __init__(
        self,
        *,
        mqttc: MqttClient,
        service: str,
        base_topic: str = "homeagent",
    ) -> None:
        self._mqttc = mqttc
        self._service = service
        self._error_topic = "%s/service/error" % base_topic
        self._heartbeat_topic = "%s/service/heartbeat" % base_topic
        self._start_time = time.monotonic()
        self._heartbeat_task: Optional[asyncio.Task] = None

    def report_error(
        self,
        context: str,
        exc: Optional[BaseException] = None,
        *,
        detail: Optional[str] = None,
    ) -> None:
        """Publish a service.error event to MQTT (fire-and-forget)."""
        error_type = type(exc).__name__ if exc else "Unknown"
        error_msg = str(exc)[:500] if exc else (detail or "unknown error")
        tb = None
        if exc and exc.__traceback__:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            if len(tb) > 2000:
                tb = tb[-2000:]

        evt = make_event(
            source=self._service,
            typ="service.error",
            data={
                "service": self._service,
                "context": context,
                "error_type": error_type,
                "error": error_msg,
                "traceback": tb,
                "pid": os.getpid(),
            },
        )
        try:
            self._mqttc.publish_json(self._error_topic, evt)
        except Exception:
            pass

    def start_heartbeat(self, interval_seconds: float = 30.0) -> None:
        """Start a background task that publishes periodic heartbeats."""
        if self._heartbeat_task is not None:
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_seconds)
                uptime = time.monotonic() - self._start_time
                evt = make_event(
                    source=self._service,
                    typ="service.heartbeat",
                    data={
                        "service": self._service,
                        "uptime_seconds": round(uptime, 1),
                        "pid": os.getpid(),
                    },
                )
                try:
                    self._mqttc.publish_json(self._heartbeat_topic, evt)
                except Exception:
                    pass

        self._heartbeat_task = asyncio.create_task(_loop())

    def stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
