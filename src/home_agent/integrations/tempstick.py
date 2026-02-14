from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass(frozen=True)
class TempStickSensor:
    sensor_id: str
    name: str
    last_temp_c: Optional[float]
    last_humidity: Optional[float]
    offline: Optional[bool]
    last_checkin: Optional[str]


class TempStickClient:
    def __init__(self, *, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = float(timeout_seconds)

    async def list_sensors(self) -> List[TempStickSensor]:
        url = "https://tempstickapi.com/api/v1/sensors/all"
        headers = {"X-API-KEY": self._api_key}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        items = (((data or {}).get("data") or {}).get("items") or [])
        sensors: List[TempStickSensor] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sensors.append(_parse_sensor(item))
        return sensors

    async def get_sensor(self, sensor_id: str) -> Optional[TempStickSensor]:
        if not sensor_id:
            return None
        url = "https://tempstickapi.com/api/v1/sensor/%s" % sensor_id
        headers = {"X-API-KEY": self._api_key}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        item = (data or {}).get("data")
        if not isinstance(item, dict):
            return None
        return _parse_sensor(item)


def _parse_sensor(item: Dict[str, Any]) -> TempStickSensor:
    sensor_id = str(item.get("sensor_id") or item.get("id") or "").strip()
    name = str(item.get("sensor_name") or item.get("name") or "").strip()

    def fnum(key: str) -> Optional[float]:
        v = item.get(key)
        try:
            return float(v)
        except Exception:
            return None

    last_temp_c = fnum("last_temp")
    last_humidity = fnum("last_humidity")
    offline_raw = item.get("offline")
    offline = None
    if isinstance(offline_raw, bool):
        offline = offline_raw
    elif isinstance(offline_raw, (int, float)):
        offline = bool(int(offline_raw))
    elif isinstance(offline_raw, str) and offline_raw.strip().isdigit():
        offline = bool(int(offline_raw.strip()))

    last_checkin = None
    v = item.get("last_checkin")
    if isinstance(v, str) and v.strip():
        last_checkin = v.strip()

    return TempStickSensor(
        sensor_id=sensor_id,
        name=name,
        last_temp_c=last_temp_c,
        last_humidity=last_humidity,
        offline=offline,
        last_checkin=last_checkin,
    )
