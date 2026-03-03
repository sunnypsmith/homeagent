"""National Weather Service (weather.gov) weather client.

Free, no API key, reliable US government infrastructure.
Includes response caching since NWS data only updates every few minutes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "home-agent/1.0 (homeagent@local)", "Accept": "application/geo+json"}


@dataclass(frozen=True)
class NWSCurrentWeather:
    temperature: Optional[float]
    wind_speed: Optional[float]
    wind_gusts: Optional[float]
    humidity: Optional[float]
    description: str
    temperature_unit: str
    wind_unit: str


@dataclass(frozen=True)
class NWSTodayForecast:
    temp_max: Optional[float]
    temp_min: Optional[float]
    precip_probability_max: Optional[float]
    wind_speed_max: Optional[float]
    daytime_description: str
    nighttime_description: str
    temp_unit: str
    wind_unit: str


@dataclass(frozen=True)
class NWSSunTimes:
    sunrise: Optional[datetime]
    sunset: Optional[datetime]


class NWSClient:
    """Weather client using the National Weather Service API."""

    def __init__(self, *, latitude: float, longitude: float, units: str = "imperial",
                 timeout_seconds: float = 15.0) -> None:
        self._lat = round(float(latitude), 4)
        self._lon = round(float(longitude), 4)
        self._units = units
        self._timeout = float(timeout_seconds)
        self._grid_url: Optional[str] = None
        self._forecast_url: Optional[str] = None
        self._forecast_hourly_url: Optional[str] = None
        self._station_url: Optional[str] = None
        self._cache: Dict[str, Any] = {}

    def _get_cached(self, key: str, max_age: float) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry["ts"]) < max_age:
            return entry["data"]
        return None

    def _set_cached(self, key: str, data: Any) -> None:
        self._cache[key] = {"data": data, "ts": time.monotonic()}

    async def _ensure_grid(self) -> None:
        if self._forecast_url and self._station_url:
            return
        url = "%s/points/%s,%s" % (_BASE, self._lat, self._lon)
        async with httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            props = resp.json().get("properties", {})
        self._forecast_url = props.get("forecast")
        self._forecast_hourly_url = props.get("forecastHourly")
        station_url = props.get("observationStations")
        if station_url:
            async with httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS) as client:
                resp = await client.get(station_url)
                resp.raise_for_status()
                stations = resp.json().get("features", [])
            if stations:
                self._station_url = stations[0].get("id")

    async def current(self) -> NWSCurrentWeather:
        cached = self._get_cached("current", 300)
        if cached:
            return cached

        await self._ensure_grid()
        if not self._station_url:
            raise RuntimeError("No NWS observation station found")

        url = "%s/observations/latest" % self._station_url
        async with httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            props = resp.json().get("properties", {})

        temp_c = _val(props, "temperature")
        wind_ms = _val(props, "windSpeed")
        gust_ms = _val(props, "windGust")
        humidity = _val(props, "relativeHumidity")
        desc = str(props.get("textDescription") or "")

        if self._units == "imperial":
            temp = _c_to_f(temp_c) if temp_c is not None else None
            wind = _ms_to_mph(wind_ms) if wind_ms is not None else None
            gusts = _ms_to_mph(gust_ms) if gust_ms is not None else None
            t_unit, w_unit = "F", "mph"
        else:
            temp, wind, gusts = temp_c, wind_ms, gust_ms
            t_unit, w_unit = "C", "m/s"

        result = NWSCurrentWeather(
            temperature=round(temp, 1) if temp is not None else None,
            wind_speed=round(wind, 1) if wind is not None else None,
            wind_gusts=round(gusts, 1) if gusts is not None else None,
            humidity=round(humidity, 1) if humidity is not None else None,
            description=desc,
            temperature_unit=t_unit,
            wind_unit=w_unit,
        )
        self._set_cached("current", result)
        return result

    async def forecast_today(self) -> NWSTodayForecast:
        cached = self._get_cached("forecast", 600)
        if cached:
            return cached

        await self._ensure_grid()
        if not self._forecast_url:
            raise RuntimeError("No NWS forecast URL found")

        async with httpx.AsyncClient(timeout=self._timeout, headers=_HEADERS) as client:
            resp = await client.get(self._forecast_url)
            resp.raise_for_status()
            props = resp.json().get("properties", {})

        periods = props.get("periods", [])
        day_period = None
        night_period = None
        for p in periods[:4]:
            if p.get("isDaytime") and day_period is None:
                day_period = p
            elif not p.get("isDaytime") and night_period is None:
                night_period = p

        temp_max = day_period.get("temperature") if day_period else None
        temp_min = night_period.get("temperature") if night_period else None
        day_desc = day_period.get("detailedForecast", "") if day_period else ""
        night_desc = night_period.get("detailedForecast", "") if night_period else ""

        precip_max = None
        wind_max = None
        for p in ([day_period, night_period] if day_period else periods[:2]):
            if not p:
                continue
            prob = p.get("probabilityOfPrecipitation", {})
            if isinstance(prob, dict):
                pv = prob.get("value")
                if pv is not None and (precip_max is None or pv > precip_max):
                    precip_max = pv
            ws = p.get("windSpeed", "")
            if isinstance(ws, str) and ws:
                nums = [int(x) for x in ws.split() if x.isdigit()]
                if nums:
                    mx = max(nums)
                    if wind_max is None or mx > wind_max:
                        wind_max = mx

        t_unit = day_period.get("temperatureUnit", "F") if day_period else "F"

        result = NWSTodayForecast(
            temp_max=float(temp_max) if temp_max is not None else None,
            temp_min=float(temp_min) if temp_min is not None else None,
            precip_probability_max=float(precip_max) if precip_max is not None else None,
            wind_speed_max=float(wind_max) if wind_max is not None else None,
            daytime_description=day_desc,
            nighttime_description=night_desc,
            temp_unit=t_unit,
            wind_unit="mph",
        )
        self._set_cached("forecast", result)
        return result


def _val(props: Dict, key: str) -> Optional[float]:
    entry = props.get(key)
    if isinstance(entry, dict):
        v = entry.get("value")
    else:
        v = entry
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _ms_to_mph(ms: float) -> float:
    return ms * 2.237
