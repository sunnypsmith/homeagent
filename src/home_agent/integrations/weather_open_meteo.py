from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import httpx

from home_agent.integrations._retry import api_retry

_USER_AGENT = "HomeAgent/1.0 (github.com/home-agent)"

_cache: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL_S = 600.0  # 10 minutes


def _get_cached(key: str) -> Any:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if (time.monotonic() - ts) > _CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return val


def _set_cached(key: str, val: Any) -> None:
    _cache[key] = (time.monotonic(), val)


@dataclass(frozen=True)
class CurrentWeather:
    temperature: Optional[float]
    wind_speed: Optional[float]
    wind_gusts: Optional[float]
    temperature_unit: str
    wind_unit: str


@dataclass(frozen=True)
class TodayForecast:
    temp_max: Optional[float]
    temp_min: Optional[float]
    precip_probability_max: Optional[float]  # percent (0..100)
    precip_sum: Optional[float]
    wind_speed_max: Optional[float]
    temp_unit: str
    precip_unit: str
    wind_unit: str


@dataclass(frozen=True)
class SunTimes:
    sunrise: Optional[datetime]
    sunset: Optional[datetime]
    timezone: str


class OpenMeteoClient:
    def __init__(self, *, latitude: float, longitude: float, units: str, timeout_seconds: float) -> None:
        self._lat = float(latitude)
        self._lon = float(longitude)
        self._units = units
        self._timeout = float(timeout_seconds)

    def _unit_params(self) -> dict:
        if self._units == "imperial":
            return {
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
            }
        if self._units == "metric":
            return {
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
            }
        return {}

    def _headers(self) -> dict:
        return {"User-Agent": _USER_AGENT}

    @api_retry
    async def current(self) -> CurrentWeather:
        cached = _get_cached("current")
        if cached is not None:
            return cached

        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "current": "temperature_2m,wind_speed_10m,wind_gusts_10m",
            "timezone": "auto",
        }
        params.update(self._unit_params())

        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
            resp = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current") or {}
        units = data.get("current_units") or {}

        def f(key: str) -> Optional[float]:
            v = current.get(key)
            try:
                return float(v)
            except Exception:
                return None

        result = CurrentWeather(
            temperature=f("temperature_2m"),
            wind_speed=f("wind_speed_10m"),
            wind_gusts=f("wind_gusts_10m"),
            temperature_unit=str(units.get("temperature_2m") or ""),
            wind_unit=str(units.get("wind_speed_10m") or ""),
        )
        _set_cached("current", result)
        return result

    @api_retry
    async def forecast_today(self) -> TodayForecast:
        cached = _get_cached("forecast")
        if cached is not None:
            return cached

        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
            "timezone": "auto",
        }
        params.update(self._unit_params())

        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
            resp = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily") or {}
        units = data.get("daily_units") or {}

        def f0(key: str) -> Optional[float]:
            arr = daily.get(key)
            if not isinstance(arr, list) or not arr:
                return None
            try:
                return float(arr[0])
            except Exception:
                return None

        result = TodayForecast(
            temp_max=f0("temperature_2m_max"),
            temp_min=f0("temperature_2m_min"),
            precip_probability_max=f0("precipitation_probability_max"),
            precip_sum=f0("precipitation_sum"),
            wind_speed_max=f0("wind_speed_10m_max"),
            temp_unit=str(units.get("temperature_2m_max") or ""),
            precip_unit=str(units.get("precipitation_sum") or ""),
            wind_unit=str(units.get("wind_speed_10m_max") or ""),
        )
        _set_cached("forecast", result)
        return result

    async def sun_times_today(self, *, tz: Optional[str] = None) -> SunTimes:
        """
        Calculate today's sunrise/sunset locally using the astral library.
        No API call needed — sun position is purely mathematical.

        A local timezone (e.g. "America/New_York") MUST be provided — astral
        fails for many date/location combos when called with bare UTC.
        """
        from astral import LocationInfo
        from astral.sun import sun
        from zoneinfo import ZoneInfo

        tzinfo = ZoneInfo(tz) if tz else ZoneInfo("UTC")
        loc = LocationInfo(latitude=self._lat, longitude=self._lon)
        today = datetime.now(tzinfo).date()
        s = sun(loc.observer, date=today, tzinfo=tzinfo)
        return SunTimes(
            sunrise=s["sunrise"],
            sunset=s["sunset"],
            timezone=tz or "UTC",
        )
