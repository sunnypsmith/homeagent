"""Tests for the weather client factory and adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from home_agent.integrations.weather import (
    CurrentWeather,
    TodayForecast,
    create_weather_client,
    _NWSAdapter,
    _OpenMeteoAdapter,
)


def test_create_weather_client_nws() -> None:
    with patch("home_agent.integrations.weather_nws.NWSClient") as mock_cls:
        client = create_weather_client(
            provider="nws", latitude=40.0, longitude=-74.0,
            units="imperial", timeout_seconds=10.0,
        )
    assert isinstance(client, _NWSAdapter)


def test_create_weather_client_open_meteo() -> None:
    with patch("home_agent.integrations.weather_open_meteo.OpenMeteoClient") as mock_cls:
        client = create_weather_client(
            provider="open_meteo", latitude=40.0, longitude=-74.0,
            units="imperial", timeout_seconds=10.0,
        )
    assert isinstance(client, _OpenMeteoAdapter)


@pytest.mark.asyncio
async def test_nws_adapter_current() -> None:
    @dataclass(frozen=True)
    class FakeNWSCurrent:
        temperature: Optional[float] = 72.0
        wind_speed: Optional[float] = 10.0
        wind_gusts: Optional[float] = 15.0
        temperature_unit: str = "F"
        wind_unit: str = "mph"
        description: str = "Sunny"

    mock_client = MagicMock()
    mock_client.current = AsyncMock(return_value=FakeNWSCurrent())
    adapter = _NWSAdapter(mock_client)
    result = await adapter.current()

    assert isinstance(result, CurrentWeather)
    assert result.temperature == 72.0
    assert result.wind_speed == 10.0
    assert result.description == "Sunny"


@pytest.mark.asyncio
async def test_nws_adapter_forecast_today() -> None:
    @dataclass(frozen=True)
    class FakeNWSForecast:
        temp_max: Optional[float] = 85.0
        temp_min: Optional[float] = 65.0
        precip_probability_max: Optional[float] = 30.0
        wind_speed_max: Optional[float] = 20.0
        temp_unit: str = "F"
        wind_unit: str = "mph"

    mock_client = MagicMock()
    mock_client.forecast_today = AsyncMock(return_value=FakeNWSForecast())
    adapter = _NWSAdapter(mock_client)
    result = await adapter.forecast_today()

    assert isinstance(result, TodayForecast)
    assert result.temp_max == 85.0
    assert result.temp_min == 65.0
    assert result.precip_probability_max == 30.0


@pytest.mark.asyncio
async def test_open_meteo_adapter_current() -> None:
    @dataclass(frozen=True)
    class FakeOMCurrent:
        temperature: Optional[float] = 20.0
        wind_speed: Optional[float] = 5.0
        wind_gusts: Optional[float] = 8.0
        temperature_unit: str = "C"
        wind_unit: str = "km/h"

    mock_client = MagicMock()
    mock_client.current = AsyncMock(return_value=FakeOMCurrent())
    adapter = _OpenMeteoAdapter(mock_client)
    result = await adapter.current()

    assert isinstance(result, CurrentWeather)
    assert result.temperature == 20.0
    assert result.description == ""


@pytest.mark.asyncio
async def test_open_meteo_adapter_forecast_today() -> None:
    @dataclass(frozen=True)
    class FakeOMForecast:
        temp_max: Optional[float] = 30.0
        temp_min: Optional[float] = 18.0
        precip_probability_max: Optional[float] = 50.0
        precip_sum: Optional[float] = 2.5
        wind_speed_max: Optional[float] = 15.0
        temp_unit: str = "C"
        precip_unit: str = "mm"
        wind_unit: str = "km/h"

    mock_client = MagicMock()
    mock_client.forecast_today = AsyncMock(return_value=FakeOMForecast())
    adapter = _OpenMeteoAdapter(mock_client)
    result = await adapter.forecast_today()

    assert isinstance(result, TodayForecast)
    assert result.temp_max == 30.0
    assert result.precip_sum == 2.5
    assert result.precip_unit == "mm"
