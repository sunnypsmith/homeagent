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
from home_agent.integrations.dashboard_scrape import DashboardScraper
from home_agent.integrations.llm import LLMClient
from home_agent.integrations.llm_router import LLMRouter
from home_agent.integrations.gcal_ics import GoogleCalendarIcsClient
from home_agent.integrations.news_feed import fetch_all_feeds
from home_agent.integrations.simplefin import SimpleFINClient
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


def _format_precip_phrase(value: float, unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in ("in", "inch", "inches"):
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
        return "%d and %s inches" % (whole, frac)
    if u in ("mm", "millimeter", "millimeters"):
        mm = int(round(float(value)))
        if mm == 0 and float(value) > 0:
            mm = 1
        return "%d millimeters" % mm
    return "%.2f %s" % (round(float(value), 2), unit or "")


def _spoken_ampm(dt: datetime) -> str:
    return "P M" if dt.hour >= 12 else "A M"


def _spoken_time(dt: datetime) -> str:
    h = dt.hour % 12
    if h == 0:
        h = 12
    m = dt.minute
    if m == 0:
        return "%d %s" % (h, _spoken_ampm(dt))
    return "%d %d %s" % (h, m, _spoken_ampm(dt))


def _spoken_hour_minute(dt: datetime) -> str:
    h = dt.hour % 12
    if h == 0:
        h = 12
    m = dt.minute
    if m == 0:
        return "%d %s" % (h, _spoken_ampm(dt))
    return "%d:%02d %s" % (h, m, _spoken_ampm(dt))


def _calendar_payload(events: list[object], *, now_local: datetime) -> Dict[str, Any]:
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


def _format_dollars(v: float) -> str:
    """
    Format a dollar amount for slow, clear TTS output.
    Examples:
        12345.67  -> "twelve thousand, three hundred forty five dollars"
        -1234.00  -> "negative one thousand, two hundred thirty four dollars"
        0.00      -> "zero dollars"
    """
    negative = v < 0
    cents = abs(v)
    whole = int(cents)

    text = _number_to_words(whole) + " dollars"
    if negative:
        text = "negative " + text
    return text


def _number_to_words(n: int) -> str:
    if n == 0:
        return "zero"

    ones = [
        "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen",
    ]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

    def _under_thousand(num: int) -> str:
        if num == 0:
            return ""
        if num < 20:
            return ones[num]
        if num < 100:
            t = tens[num // 10]
            o = ones[num % 10]
            return ("%s %s" % (t, o)).strip()
        h = ones[num // 100] + " hundred"
        rem = num % 100
        if rem == 0:
            return h
        return h + " " + _under_thousand(rem)

    parts: list[str] = []
    if n >= 1_000_000_000:
        b = n // 1_000_000_000
        parts.append(_under_thousand(b) + " billion")
        n %= 1_000_000_000
    if n >= 1_000_000:
        m = n // 1_000_000
        parts.append(_under_thousand(m) + " million")
        n %= 1_000_000
    if n >= 1_000:
        t = n // 1_000
        parts.append(_under_thousand(t) + " thousand")
        n %= 1_000
    if n > 0:
        parts.append(_under_thousand(n))

    return ", ".join(parts)


async def run_exec_briefing_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="exec_briefing_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-exec-briefing-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/exec_briefing" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/announce/request" % settings.mqtt.base_topic

    # LLM router
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
    exec_ics = settings.exec_briefing.ics_url
    if exec_ics:
        gcal_client = GoogleCalendarIcsClient(ics_url=exec_ics, timeout_seconds=20.0)
    elif settings.gcal.enabled and settings.gcal.ics_url:
        gcal_client = GoogleCalendarIcsClient(ics_url=settings.gcal.ics_url, timeout_seconds=20.0)

    dashboard_scraper: Optional[DashboardScraper] = None
    if settings.exec_briefing.dashboard_url and settings.llm.api_key:
        dashboard_scraper = DashboardScraper(
            url=settings.exec_briefing.dashboard_url,
            llm_base_url=settings.llm.base_url,
            llm_api_key=settings.llm.api_key,
            vision_model=settings.exec_briefing.dashboard_vision_model,
        )

    simplefin_client: Optional[SimpleFINClient] = None
    if settings.simplefin.enabled and settings.simplefin.access_url:
        simplefin_client = SimpleFINClient(
            access_url=settings.simplefin.access_url,
            timeout_seconds=settings.simplefin.timeout_seconds,
        )
    elif settings.simplefin.enabled:
        log.warning("simplefin_disabled", reason="missing_access_url")

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

            if typ != "time.cron.exec_briefing":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            # --- Weather ---
            weather_sentence = ""
            if weather_client is not None:
                try:
                    fc = await weather_client.forecast_today()
                    parts = []
                    if fc.temp_max is not None and fc.temp_min is not None:
                        parts.append("high %d and low %d" % (int(round(fc.temp_max)), int(round(fc.temp_min))))
                    elif fc.temp_max is not None:
                        parts.append("high %d" % int(round(fc.temp_max)))
                    elif fc.temp_min is not None:
                        parts.append("low %d" % int(round(fc.temp_min)))

                    if fc.precip_probability_max is not None:
                        parts.append("precipitation chance up to %d percent" % int(round(fc.precip_probability_max)))

                    if fc.precip_sum is not None and fc.precip_sum > 0:
                        parts.append("total precipitation around %s" % _format_precip_phrase(float(fc.precip_sum), fc.precip_unit))

                    if fc.wind_speed_max is not None:
                        parts.append("wind speeds up to %d %s" % (int(round(fc.wind_speed_max)), _spoken_wind_unit(fc.wind_unit)))

                    if parts:
                        weather_sentence = "Forecast for today: " + ", ".join(parts) + "."
                except Exception:
                    log.warning("weather_failed")

            # --- Calendar ---
            now_local = datetime.now(tz=tz)
            today = now_local.strftime("%A, %B %d").replace(" 0", " ")
            calendar_json = json.dumps({"date": now_local.date().isoformat(), "events": [], "event_count": 0}, ensure_ascii=False)
            if gcal_client is not None:
                try:
                    events = await gcal_client.fetch_events(
                        tz=tz,
                        start_date=now_local.date(),
                        days=max(1, int(settings.gcal.lookahead_days)),
                        max_events=20,
                    )
                    today_events = [
                        e for e in events
                        if isinstance(getattr(e, "start", None), datetime) and e.start.date() == now_local.date()
                    ]
                    calendar_json = json.dumps(_calendar_payload(today_events, now_local=now_local), ensure_ascii=False)
                except Exception as e:
                    log.warning("gcal_failed", error=str(e))

            # --- Financial ---
            finance_sentence = ""
            if simplefin_client is not None:
                try:
                    summary = await simplefin_client.financial_summary()
                    finance_sentence = (
                        "Financial snapshot: total cash %s, total debt %s, net worth %s."
                        % (
                            _format_dollars(summary.total_cash),
                            _format_dollars(summary.total_debt),
                            _format_dollars(summary.net_worth),
                        )
                    )
                except Exception as e:
                    log.warning("simplefin_failed", error=str(e))

            # --- Dashboard ---
            dashboard_sentence = ""
            if dashboard_scraper is not None:
                try:
                    metrics = await dashboard_scraper.fetch_metrics()
                    parts = []
                    if metrics.last_24h is not None:
                        parts.append("last twenty four hours %s" % _format_dollars(float(metrics.last_24h)))
                    if metrics.last_30d_avg is not None:
                        parts.append("thirty day average %s" % _format_dollars(float(metrics.last_30d_avg)))
                    if metrics.spot_arr is not None:
                        parts.append("spot A R R %s" % _format_dollars(float(metrics.spot_arr)))
                    if parts:
                        dashboard_sentence = "Massed Compute: " + ", ".join(parts) + "."
                except Exception as e:
                    log.warning("dashboard_scrape_failed", error=str(e))

            # --- News feeds ---
            news_section = ""
            news_feeds = settings.exec_briefing.news_feeds
            if news_feeds:
                try:
                    results = await fetch_all_feeds(
                        news_feeds,
                        max_items=settings.exec_briefing.news_headlines,
                    )
                    parts = []
                    for result in results:
                        if result.headlines:
                            titles = [h.title for h in result.headlines]
                            parts.append(
                                "%s headlines: %s." % (result.label, ". ".join(titles))
                            )
                    if parts:
                        news_section = " ".join(parts)
                except Exception as e:
                    log.warning("news_feed_failed", error=str(e))

            # --- LLM ---
            system = (
                "You are a concise executive briefing generator for a busy professional. "
                "Write for text-to-speech. Be direct and professional."
            )
            user = (
                f"Today is {today}.\n\n"
                "Generate a concise executive briefing.\n"
                "Requirements:\n"
                "- Start with the day and date.\n"
                "- If a forecast sentence is provided, include it verbatim as its own sentence.\n"
                "- If a financial snapshot sentence is provided, include it verbatim as its own sentence.\n"
                "- If a dashboard sentence is provided, include it verbatim as its own sentence.\n"
                "- If news headlines are provided, read each headline verbatim.\n"
                "- Use the calendar JSON to narrate today's schedule.\n"
                "- Only mention calendar events that appear in the calendar JSON. Do not invent.\n"
                "- Keep event titles verbatim.\n"
                "- If there are zero events, say the calendar is clear today.\n"
                '- End with exactly: "Mind how you go."\n'
                "- Do not use bullet characters.\n\n"
                f"Forecast sentence (use verbatim, or omit if blank):\n{weather_sentence}\n\n"
                f"Financial snapshot sentence (use verbatim, or omit if blank):\n{finance_sentence}\n\n"
                f"Dashboard sentence (use verbatim, or omit if blank):\n{dashboard_sentence}\n\n"
                f"News headlines (read each headline verbatim, or omit if blank):\n{news_section}\n\n"
                f"Calendar JSON (do not repeat verbatim; use it to narrate):\n{calendar_json}\n"
            )

            try:
                reply = await llm.chat(system=system, user=user, max_tokens=4096, temperature=0.4)
                text = reply.text.strip()
                announce_data: Dict[str, Any] = {
                    "text": text,
                    "concurrency": settings.sonos.announce_concurrency,
                }
                targets = settings.sonos.resolve_targets(settings.exec_briefing.targets)
                if targets:
                    announce_data["targets"] = targets

                announce = make_event(
                    source="exec-briefing-agent",
                    typ="announce.request",
                    trace_id=trace_id,
                    data=announce_data,
                )
                mqttc.publish_json(pub_topic, announce)
                log.info("published", to=pub_topic, trace_id=trace_id, llm_provider=reply.provider)
            except Exception:
                log.exception("briefing_failed")
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_exec_briefing_agent())
    return 0
