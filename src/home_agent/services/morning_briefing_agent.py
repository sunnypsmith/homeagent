from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.llm import LLMClient
from home_agent.integrations.llm_router import LLMRouter
from home_agent.integrations.gcal_ics import GoogleCalendarIcsClient
from home_agent.integrations.weather_open_meteo import OpenMeteoClient


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


def _spoken_wind_unit(unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in ("mph", "mi/h", "mp/h"):
        return "miles per hour"
    if u in ("kmh", "km/h"):
        return "kilometers per hour"
    if u in ("m/s", "ms"):
        return "meters per second"
    if u in ("kn", "kt", "kts", "knots"):
        return "knots"
    return unit or "miles per hour"


def _spoken_precip_unit(unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in ("mm", "millimeter", "millimeters"):
        return "millimeters"
    if u in ("cm", "centimeter", "centimeters"):
        return "centimeters"
    if u in ("in", "inch", "inches"):
        return "inches"
    return unit or "inches"


def _format_inches_fraction(value: float) -> str:
    """
    Format inches as a spoken-friendly nearest quarter.
    Examples:
      0.24 -> "a quarter"
      0.50 -> "a half"
      1.26 -> "1 and a quarter"
      2.74 -> "2 and three quarters"
    """
    v = max(0.0, float(value))
    quarters = int(round(v * 4))
    whole = quarters // 4
    frac_q = quarters % 4

    frac_map = {0: "", 1: "a quarter", 2: "a half", 3: "three quarters"}
    frac = frac_map[frac_q]

    if whole == 0:
        if quarters == 0 and v > 0:
            return "under a quarter inch"
        if frac_q == 1:
            return "a quarter inch"
        if frac_q == 2:
            return "a half inch"
        if frac_q == 3:
            return "three quarters of an inch"
        return "0 inches"

    if not frac:
        return "1 inch" if whole == 1 else "%d inches" % whole

    # Whole number + fraction
    return "%d and %s inches" % (whole, frac)


def _format_precip_phrase(value: float, unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in ("in", "inch", "inches"):
        return _format_inches_fraction(value)
    if u in ("mm", "millimeter", "millimeters"):
        mm = int(round(float(value)))
        if mm == 0 and float(value) > 0:
            mm = 1
        return "%d millimeters" % mm
    if u in ("cm", "centimeter", "centimeters"):
        cm = round(float(value), 1)
        if cm == 0 and float(value) > 0:
            cm = 0.1
        return "%.1f centimeters" % cm
    # fallback
    return "%.2f %s" % (round(float(value), 2), _spoken_precip_unit(unit))


def _spoken_ampm(dt: datetime) -> str:
    h = int(dt.hour)
    return "P M" if h >= 12 else "A M"


def _spoken_hour_minute(dt: datetime) -> str:
    h24 = int(dt.hour)
    h = h24 % 12
    if h == 0:
        h = 12
    m = int(dt.minute)
    if m == 0:
        return "%d %s" % (h, _spoken_ampm(dt))
    return "%d:%02d %s" % (h, m, _spoken_ampm(dt))


def _spoken_time(dt: datetime) -> str:
    """
    Spoken-friendly time like "9 15 A M" or "2 P M".
    """
    h24 = int(dt.hour)
    h = h24 % 12
    if h == 0:
        h = 12
    m = int(dt.minute)
    if m == 0:
        return "%d %s" % (h, _spoken_ampm(dt))
    return "%d %d %s" % (h, m, _spoken_ampm(dt))


def _calendar_payload(events: list[object], *, now_local: datetime) -> Dict[str, Any]:
    """
    Build a compact JSON payload for the LLM so it can narrate reliably.
    """
    out: Dict[str, Any] = {"date": now_local.date().isoformat(), "events": []}

    items: list[Dict[str, Any]] = []
    for e in events[:10]:
        try:
            title = str(getattr(e, "title", "") or "").strip()
            if not title:
                continue
            all_day = bool(getattr(e, "all_day", False))
            start = getattr(e, "start", None)
            item: Dict[str, Any] = {"title": title, "all_day": all_day}
            if isinstance(start, datetime):
                item["start_iso"] = start.isoformat()
                item["start_speech"] = _spoken_time(start)
                item["start_display"] = _spoken_hour_minute(start)
            items.append(item)
        except Exception:
            continue

    out["events"] = items
    out["event_count"] = len(items)
    return out


async def run_morning_briefing_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="morning_briefing_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-morning-briefing-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/morning_briefing" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/announce/request" % settings.mqtt.base_topic

    # LLM router: primary + optional fallback
    providers = []
    providers.append(
        (
            "primary",
            LLMClient(
                base_url=settings.llm.base_url,
                api_key=settings.llm.api_key,
                model=settings.llm.model,
                timeout_seconds=settings.llm.timeout_seconds,
            ),
        )
    )
    if settings.llm_fallback.enabled:
        providers.append(
            (
                "fallback",
                LLMClient(
                    base_url=settings.llm_fallback.base_url,
                    api_key=settings.llm_fallback.api_key,
                    model=settings.llm_fallback.model,
                    timeout_seconds=settings.llm_fallback.timeout_seconds,
                ),
            )
        )
    llm = LLMRouter(providers)

    weather_client: Optional[OpenMeteoClient] = None
    if settings.weather.provider == "open_meteo" and settings.weather.latitude and settings.weather.longitude:
        weather_client = OpenMeteoClient(
            latitude=settings.weather.latitude,
            longitude=settings.weather.longitude,
            units=settings.weather.units,
            timeout_seconds=settings.weather.timeout_seconds,
        )

    tz = ZoneInfo(settings.timezone)
    gcal_client: Optional[GoogleCalendarIcsClient] = None
    if settings.gcal.enabled and settings.gcal.ics_url:
        gcal_client = GoogleCalendarIcsClient(ics_url=settings.gcal.ics_url, timeout_seconds=20.0)

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
                data = _require_dict(payload, "data")
            except Exception as e:
                log.warning("bad_event", topic=msg.topic, error=str(e))
                continue

            if typ != "time.cron.morning_briefing":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            variant = data.get("variant") if isinstance(data.get("variant"), str) else None

            weather_sentence = ""
            if weather_client is not None:
                try:
                    fc = await weather_client.forecast_today()
                    parts = []
                    if fc.temp_max is not None and fc.temp_min is not None:
                        parts.append(
                            "high %d and low %d"
                            % (int(round(fc.temp_max)), int(round(fc.temp_min)))
                        )
                    elif fc.temp_max is not None:
                        parts.append("high %d" % int(round(fc.temp_max)))
                    elif fc.temp_min is not None:
                        parts.append("low %d" % int(round(fc.temp_min)))

                    if fc.precip_probability_max is not None:
                        parts.append("precipitation chance up to %d percent" % int(round(fc.precip_probability_max)))

                    if fc.precip_sum is not None and fc.precip_sum > 0:
                        parts.append(
                            "total precipitation around %s"
                            % (_format_precip_phrase(float(fc.precip_sum), fc.precip_unit),)
                        )

                    if fc.wind_speed_max is not None:
                        parts.append(
                            "wind speeds up to %d %s"
                            % (int(round(fc.wind_speed_max)), _spoken_wind_unit(fc.wind_unit))
                        )

                    if parts:
                        weather_sentence = "Forecast for today: " + ", ".join(parts) + "."
                except Exception:
                    log.warning("weather_failed")

            now_local = datetime.now(tz=tz)
            today = now_local.strftime("%A, %B %d").replace(" 0", " ")
            weekend_note = "It is the weekend." if variant == "weekend" else "It is a weekday."

            # Always provide JSON, even if empty, so the LLM has deterministic input.
            calendar_json = json.dumps({"date": now_local.date().isoformat(), "events": [], "event_count": 0}, ensure_ascii=False)
            if gcal_client is not None:
                try:
                    events = await gcal_client.fetch_events(
                        tz=tz,
                        start_date=now_local.date(),
                        days=max(1, int(settings.gcal.lookahead_days)),
                        max_events=20,
                    )
                    # Only speak events starting today.
                    today_events = [
                        e
                        for e in events
                        if isinstance(getattr(e, "start", None), datetime)
                        and e.start.date() == now_local.date()
                    ]
                    calendar_json = json.dumps(_calendar_payload(today_events, now_local=now_local), ensure_ascii=False)
                except Exception as e:
                    # Do not log the ICS URL; treat it like a bearer secret.
                    log.warning("gcal_failed", error=str(e))

            system = (
                "You are a home morning-briefing generator. "
                "Write for text-to-speech. Be cheerful and uplifting."
            )
            user = (
                f"Today is {today}. {weekend_note}\n\n"
                "Generate a morning briefing for the Smith Family.\n"
                "Requirements:\n"
                "- 4 to 7 sentences total.\n"
                "- Mention today's day.\n"
                "- Do not suggest activities.\n"
                "- If a forecast sentence is provided, include it verbatim as its own sentence.\n"
                "- Use the calendar JSON to narrate today's schedule.\n"
                "- Only mention calendar events that appear in the calendar JSON. Do not invent.\n"
                "- Keep event titles verbatim.\n"
                "- If there are zero events, say there are no calendar events today.\n"
                '- End with exactly: "Mind how you go."\n'
                "- Do not use bullet characters.\n\n"
                f"Forecast sentence (use verbatim, or omit if blank):\n{weather_sentence}\n"
                f"Calendar JSON (do not repeat verbatim; use it to narrate):\n{calendar_json}\n"
            )

            try:
                reply = await llm.chat(system=system, user=user, max_tokens=220, temperature=0.4)
                text = reply.text.strip()
                announce = make_event(
                    source="morning-briefing-agent",
                    typ="announce.request",
                    trace_id=trace_id,
                    data={"text": text, "concurrency": settings.sonos.announce_concurrency},
                )
                mqttc.publish_json(pub_topic, announce)
                log.info("published", to=pub_topic, trace_id=trace_id, llm_provider=reply.provider)
            except Exception:
                log.exception("briefing_failed")
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_morning_briefing_agent())
    return 0

