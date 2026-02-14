from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.tempstick import TempStickClient, TempStickSensor
from home_agent.integrations.internet_check import run_internet_check
from home_agent.integrations.ups_snmp import UpsSnmpClient
from home_agent.offline_audio import OFFLINE_AUDIO_ITEMS


def _require_str(payload: Dict[str, Any], key: str) -> str:
    v = payload.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError("missing_or_invalid_%s" % key)
    return v


def _require_dict(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = payload.get(key)
    if not isinstance(v, dict):
        raise ValueError("missing_or_invalid_%s" % key)
    return v


def _c_to_f(c: float) -> float:
    return (float(c) * 9.0 / 5.0) + 32.0


def _fmt_temp_f(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    return "%d" % int(round(float(v)))


async def _tempstick_check(settings: AppSettings, *, log, client: TempStickClient) -> dict:
    sensor: TempStickSensor | None = None
    if settings.tempstick.sensor_id:
        sensor = await client.get_sensor(settings.tempstick.sensor_id)
    if sensor is None:
        sensors = await client.list_sensors()
        want = (settings.tempstick.sensor_name or "").strip().lower()
        if want:
            for s in sensors:
                if (s.name or "").strip().lower() == want:
                    sensor = s
                    break
        if sensor is None and sensors:
            sensor = sensors[0]

    data: Dict[str, Any] = {"ok": False, "alerts": []}
    if sensor is None or not sensor.sensor_id:
        data["error"] = "sensor_not_found"
        return data

    temp_c = sensor.last_temp_c
    temp_f = _c_to_f(temp_c) if temp_c is not None else None
    humidity = sensor.last_humidity
    label = (sensor.name or "").strip() or "Temp Stick"
    data.update(
        {
            "ok": True,
            "label": label,
            "sensor_id": sensor.sensor_id,
            "sensor_name": sensor.name or None,
            "temp_c": temp_c,
            "temp_f": temp_f,
            "humidity": humidity,
            "offline": sensor.offline,
            "last_checkin": sensor.last_checkin,
        }
    )

    alerts: List[str] = []
    if sensor.offline:
        alerts.append("%s is offline" % label)

    low_f = settings.tempstick.temp_low_f
    high_f = settings.tempstick.temp_high_f
    if temp_f is not None:
        if low_f is not None and temp_f < float(low_f):
            alerts.append("Temperature is %s, below %s" % (_fmt_temp_f(temp_f), _fmt_temp_f(low_f)))
        if high_f is not None and temp_f > float(high_f):
            alerts.append("Temperature is %s, above %s" % (_fmt_temp_f(temp_f), _fmt_temp_f(high_f)))

    low_h = settings.tempstick.humidity_low
    high_h = settings.tempstick.humidity_high
    if humidity is not None:
        if low_h is not None and float(humidity) < float(low_h):
            alerts.append("Humidity is %d, below %d" % (int(round(humidity)), int(round(float(low_h)))))
        if high_h is not None and float(humidity) > float(high_h):
            alerts.append("Humidity is %d, above %d" % (int(round(humidity)), int(round(float(high_h)))))

    data["alerts"] = alerts
    return data


def _fmt_float(v: Optional[float], *, digits: int = 1) -> Optional[str]:
    if v is None:
        return None
    fmt = "%%.%df" % int(digits)
    return fmt % float(v)


async def _ups_check(settings: AppSettings, *, log, client: UpsSnmpClient) -> dict:
    data: Dict[str, Any] = {"ok": False, "alerts": []}
    label = (settings.ups.name or "").strip() or "UPS"
    data["label"] = label

    try:
        metrics = await client.get_input_metrics(
            voltage_oid=settings.ups.input_voltage_oid,
            frequency_oid=settings.ups.input_frequency_oid,
        )
    except Exception as e:
        data["error"] = type(e).__name__
        return data

    voltage = metrics.voltage
    frequency = metrics.frequency
    if voltage is not None:
        voltage = float(voltage) * float(settings.ups.input_voltage_scale)
    if frequency is not None:
        frequency = float(frequency) * float(settings.ups.input_frequency_scale)

    data.update(
        {
            "ok": True,
            "input_voltage": voltage,
            "input_frequency": frequency,
        }
    )

    alerts: List[str] = []
    if voltage is None and frequency is None:
        alerts.append("UPS input metrics unavailable")
    else:
        low_v = settings.ups.input_voltage_low
        high_v = settings.ups.input_voltage_high
        if voltage is not None:
            if low_v is not None and voltage < float(low_v):
                alerts.append(
                    "UPS line voltage is %s, below %s"
                    % (_fmt_temp_f(voltage), _fmt_temp_f(low_v))
                )
            if high_v is not None and voltage > float(high_v):
                alerts.append(
                    "UPS line voltage is %s, above %s"
                    % (_fmt_temp_f(voltage), _fmt_temp_f(high_v))
                )

        low_f = settings.ups.input_frequency_low
        high_f = settings.ups.input_frequency_high
        if frequency is not None:
            if low_f is not None and frequency < float(low_f):
                alerts.append(
                    "UPS line frequency is %s, below %s"
                    % (_fmt_float(frequency, digits=1), _fmt_float(low_f, digits=1))
                )
            if high_f is not None and frequency > float(high_f):
                alerts.append(
                    "UPS line frequency is %s, above %s"
                    % (_fmt_float(frequency, digits=1), _fmt_float(high_f, digits=1))
                )

    data["alerts"] = alerts
    return data


async def _internet_check(settings: AppSettings) -> dict:
    data: Dict[str, Any] = {"ok": False, "alerts": []}
    label = "Internet egress"
    data["label"] = label

    host = (settings.internet.host or "").strip()
    if not host:
        data["error"] = "missing_host"
        return data

    try:
        result = await asyncio.to_thread(
            run_internet_check,
            host=host,
            duration_seconds=settings.internet.duration_seconds,
            interval_seconds=settings.internet.interval_seconds,
            timeout_seconds=settings.internet.timeout_seconds,
        )
    except Exception as e:
        data["error"] = type(e).__name__
        return data

    data.update(
        {
            "ok": True,
            "host": host,
            "sent": result.sent,
            "received": result.received,
            "loss_percent": result.loss_percent,
            "avg_latency_ms": result.avg_latency_ms,
            "min_latency_ms": result.min_latency_ms,
            "max_latency_ms": result.max_latency_ms,
        }
    )

    alerts: List[str] = []
    offline_key: Optional[str] = None
    if result.received == 0:
        alerts.append(
            "Your attention please. The internet egress is down. Repeating. The internet egress is down."
        )
        offline_key = "internet_down"
    else:
        if result.loss_percent > float(settings.internet.max_loss_percent):
            alerts.append(
                "Your attention please. The internet egress has significant packet loss. "
                "Repeating. The internet egress has significant packet loss."
            )
            offline_key = "internet_packet_loss"
        avg_ms = result.avg_latency_ms
        if avg_ms is not None and avg_ms > float(settings.internet.max_latency_ms):
            alerts.append(
                "Your attention please. The internet egress has high latency. "
                "Repeating. The internet egress has high latency."
            )
            if offline_key is None:
                offline_key = "internet_high_latency"

    data["alerts"] = alerts
    data["offline_audio_key"] = offline_key
    return data


def _resolve_repo_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parents[3] / p


def _ensure_offline_audio(settings: AppSettings, *, log) -> None:
    out_dir = _resolve_repo_path(settings.offline_audio.dir)
    missing: List[str] = []
    for item in OFFLINE_AUDIO_ITEMS:
        path = out_dir / item["filename"]
        if not path.exists():
            missing.append(item["filename"])

    if not missing:
        return

    script = Path(__file__).resolve().parents[3] / "scripts" / "generate_offline_audio.py"
    log.info("offline_audio_missing", count=len(missing), dir=str(out_dir))
    if not script.exists():
        log.warning("offline_audio_script_missing", path=str(script))
        return

    try:
        res = subprocess.run(
            [sys.executable, str(script), "--output-dir", str(out_dir)],
            check=False,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            log.warning(
                "offline_audio_generate_failed",
                code=res.returncode,
                stdout=(res.stdout or "").strip()[:500],
                stderr=(res.stderr or "").strip()[:500],
            )
    except Exception as e:
        log.warning("offline_audio_generate_failed", error=type(e).__name__, detail=str(e))


async def run_hourly_house_check_agent() -> None:
    """
    Hourly home check: run lightweight monitors and optionally announce issues.
    """
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="hourly_house_check_agent")

    _ensure_offline_audio(settings, log=log)

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-hourly-house-check-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/hourly_house_check" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/house/check/report" % settings.mqtt.base_topic
    announce_topic = "%s/announce/request" % settings.mqtt.base_topic

    tempstick_client: TempStickClient | None = None
    if settings.tempstick.enabled and settings.tempstick.api_key:
        tempstick_client = TempStickClient(
            api_key=settings.tempstick.api_key,
            timeout_seconds=settings.tempstick.timeout_seconds,
        )
    elif settings.tempstick.enabled:
        log.warning("tempstick_disabled", reason="missing_api_key")

    ups_client: UpsSnmpClient | None = None
    if settings.ups.enabled and settings.ups.host:
        try:
            ups_client = UpsSnmpClient(
                host=settings.ups.host,
                port=settings.ups.port,
                community=settings.ups.community,
                version=settings.ups.version,
                timeout_seconds=settings.ups.timeout_seconds,
                retries=settings.ups.retries,
            )
        except Exception as e:
            log.warning("ups_disabled", reason=type(e).__name__, detail=str(e))
    elif settings.ups.enabled:
        log.warning("ups_disabled", reason="missing_host")

    if settings.internet.enabled and not settings.internet.host:
        log.warning("internet_check_disabled", reason="missing_host")

    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload: Dict[str, Any] = msg.json()
                event_id = _require_str(payload, "id")
                _require_str(payload, "ts")
                _require_str(payload, "source")
                typ = _require_str(payload, "type")
                trace_id = _require_str(payload, "trace_id")
                _require_dict(payload, "data")
            except Exception as e:
                log.warning("bad_event", topic=msg.topic, error=str(e))
                continue

            if typ != "time.cron.hourly_house_check":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            checks: Dict[str, Any] = {}
            alerts: List[str] = []

            if tempstick_client is not None:
                try:
                    ts = await _tempstick_check(settings, log=log, client=tempstick_client)
                    checks["tempstick"] = ts
                    alerts.extend(ts.get("alerts") or [])
                except Exception as e:
                    checks["tempstick"] = {"ok": False, "error": type(e).__name__}
                    alerts.append("Temp Stick check failed")

            if ups_client is not None:
                try:
                    ups = await _ups_check(settings, log=log, client=ups_client)
                    checks["ups"] = ups
                    alerts.extend(ups.get("alerts") or [])
                except Exception as e:
                    checks["ups"] = {"ok": False, "error": type(e).__name__}
                    alerts.append("UPS check failed")

            if settings.internet.enabled and settings.internet.host:
                try:
                    inet = await _internet_check(settings)
                    checks["internet"] = inet
                    alerts.extend(inet.get("alerts") or [])
                except Exception as e:
                    checks["internet"] = {"ok": False, "error": type(e).__name__}
                    alerts.append("Internet check failed")

            report = make_event(
                source="hourly-house-check-agent",
                typ="house.check.report",
                trace_id=trace_id,
                data={
                    "checks": checks,
                    "alerts": alerts,
                },
            )
            mqttc.publish_json(pub_topic, report)
            log.info("published", to=pub_topic, trace_id=trace_id, from_event=event_id, alerts=len(alerts))

            if alerts:
                offline_key = None
                inet = checks.get("internet")
                if isinstance(inet, dict):
                    offline_key = inet.get("offline_audio_key")

                if any(a.lower().startswith("your attention please") for a in alerts):
                    text = " ".join(alerts)
                    payload_data: Dict[str, Any] = {"text": text}
                    if isinstance(offline_key, str) and offline_key:
                        payload_data["offline_audio_key"] = offline_key
                else:
                    labels: List[str] = []
                    for key in ("tempstick", "ups", "internet"):
                        item = checks.get(key) or {}
                        if isinstance(item, dict) and item.get("alerts"):
                            label = item.get("label")
                            if isinstance(label, str) and label.strip():
                                labels.append(label.strip())
                    labels = list(dict.fromkeys(labels))
                    if len(labels) == 1:
                        prefix = "%s alert" % labels[0]
                    else:
                        prefix = "Home alert"
                    text = prefix + ". " + ". ".join(alerts) + "."
                    payload_data = {"text": text}

                announce = make_event(
                    source="hourly-house-check-agent",
                    typ="announce.request",
                    trace_id=trace_id,
                    data=payload_data,
                )
                mqttc.publish_json(announce_topic, announce)
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_hourly_house_check_agent())
    return 0

