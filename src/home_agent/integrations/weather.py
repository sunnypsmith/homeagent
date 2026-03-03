"""Weather client factory — returns NWS or Open-Meteo based on config."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CurrentWeather:
    temperature: Optional[float]
    wind_speed: Optional[float]
    wind_gusts: Optional[float]
    temperature_unit: str = ""
    wind_unit: str = ""
    description: str = ""


@dataclass(frozen=True)
class TodayForecast:
    temp_max: Optional[float]
    temp_min: Optional[float]
    precip_probability_max: Optional[float]
    precip_sum: Optional[float] = None
    wind_speed_max: Optional[float] = None
    temp_unit: str = ""
    precip_unit: str = ""
    wind_unit: str = ""


def create_weather_client(*, provider: str, latitude: float, longitude: float,
                          units: str, timeout_seconds: float):
    """Factory: returns a weather client with current() and forecast_today() methods."""
    if provider == "nws":
        from home_agent.integrations.weather_nws import NWSClient
        return _NWSAdapter(NWSClient(
            latitude=latitude, longitude=longitude,
            units=units, timeout_seconds=timeout_seconds,
        ))
    else:
        from home_agent.integrations.weather_open_meteo import OpenMeteoClient
        return _OpenMeteoAdapter(OpenMeteoClient(
            latitude=latitude, longitude=longitude,
            units=units, timeout_seconds=timeout_seconds,
        ))


class _NWSAdapter:
    def __init__(self, client):
        self._c = client

    async def current(self) -> CurrentWeather:
        r = await self._c.current()
        return CurrentWeather(
            temperature=r.temperature, wind_speed=r.wind_speed,
            wind_gusts=r.wind_gusts, temperature_unit=r.temperature_unit,
            wind_unit=r.wind_unit, description=r.description,
        )

    async def forecast_today(self) -> TodayForecast:
        r = await self._c.forecast_today()
        return TodayForecast(
            temp_max=r.temp_max, temp_min=r.temp_min,
            precip_probability_max=r.precip_probability_max,
            wind_speed_max=r.wind_speed_max,
            temp_unit=r.temp_unit, wind_unit=r.wind_unit,
        )

    async def sun_times_today(self):
        from home_agent.integrations.weather_open_meteo import OpenMeteoClient
        # NWS doesn't provide sun times; fall back to Open-Meteo for this
        om = OpenMeteoClient(
            latitude=self._c._lat, longitude=self._c._lon,
            units="imperial", timeout_seconds=10.0,
        )
        return await om.sun_times_today()


class _OpenMeteoAdapter:
    def __init__(self, client):
        self._c = client

    async def current(self) -> CurrentWeather:
        r = await self._c.current()
        return CurrentWeather(
            temperature=r.temperature, wind_speed=r.wind_speed,
            wind_gusts=r.wind_gusts, temperature_unit=r.temperature_unit,
            wind_unit=r.wind_unit,
        )

    async def forecast_today(self) -> TodayForecast:
        r = await self._c.forecast_today()
        return TodayForecast(
            temp_max=r.temp_max, temp_min=r.temp_min,
            precip_probability_max=r.precip_probability_max,
            precip_sum=r.precip_sum, wind_speed_max=r.wind_speed_max,
            temp_unit=r.temp_unit, precip_unit=r.precip_unit,
            wind_unit=r.wind_unit,
        )

    async def sun_times_today(self):
        return await self._c.sun_times_today()
