# Weather Provider

## Overview

The weather system uses a factory pattern (`integrations/weather.py`) that returns either an **NWS** or **Open-Meteo** client based on configuration. Both providers expose the same interface:

- `current()` → `CurrentWeather` (temperature, wind speed, gusts, description)
- `forecast_today()` → `TodayForecast` (high/low temps, precipitation probability, wind)
- `sun_times_today()` → sunrise/sunset times

Weather data is used by multiple agents: wakeup, morning briefing, hourly chime, executive briefing, and the voice intent agent.

## Providers

### NWS (National Weather Service)

Free, US-only, no API key required. Uses the weather.gov API.

- Two-step lookup: lat/lon → grid point → observation station + forecast URLs
- **Response caching**: 5-minute cache for current conditions, 10-minute cache for forecast
- **Retry logic**: built-in for transient API failures
- **Sunrise/sunset**: falls back to Open-Meteo (NWS API does not provide sun times)

```bash
WEATHER_PROVIDER=nws
```

### Open-Meteo

Free, worldwide, no API key required. Default provider.

- Direct lat/lon queries
- Provides current conditions, forecast, and sunrise/sunset
- No built-in caching (queries the API each time)

```bash
WEATHER_PROVIDER=open_meteo
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WEATHER_PROVIDER` | `open_meteo` | Provider: `nws` or `open_meteo` |
| `WEATHER_LAT` | — | Latitude (decimal degrees) |
| `WEATHER_LON` | — | Longitude (decimal degrees) |
| `WEATHER_UNITS` | `imperial` | Unit system: `imperial` or `metric` |
| `WEATHER_TIMEOUT_SECONDS` | `10` | HTTP request timeout |

## Data Models

### `CurrentWeather`

| Field | Type | Description |
|-------|------|-------------|
| `temperature` | `float` | Current temperature |
| `wind_speed` | `float` | Current wind speed |
| `wind_gusts` | `float` | Current wind gusts |
| `temperature_unit` | `str` | e.g. `°F` or `°C` |
| `wind_unit` | `str` | e.g. `mph` or `km/h` |
| `description` | `str` | Text description (NWS only) |

### `TodayForecast`

| Field | Type | Description |
|-------|------|-------------|
| `temp_max` | `float` | Forecast high temperature |
| `temp_min` | `float` | Forecast low temperature |
| `precip_probability_max` | `float` | Max precipitation probability (%) |
| `precip_sum` | `float` | Total precipitation (Open-Meteo only) |
| `wind_speed_max` | `float` | Max wind speed |
| `temp_unit` | `str` | Temperature unit |
| `precip_unit` | `str` | Precipitation unit |
| `wind_unit` | `str` | Wind unit |

## Usage

The factory is called by agents that need weather data:

```python
from home_agent.integrations.weather import create_weather_client

client = create_weather_client(
    provider=settings.weather_provider,
    latitude=settings.weather_lat,
    longitude=settings.weather_lon,
    units=settings.weather_units,
    timeout_seconds=settings.weather_timeout_seconds,
)

current = await client.current()
forecast = await client.forecast_today()
sun = await client.sun_times_today()
```
