"""OpenALPR / Rekor CarCheck integration.

Sends a JPEG image to the CarCheck API and returns structured vehicle data
(license plate, make, model, color, body type) for use in announcements.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from home_agent.core.logging import get_logger

_log = get_logger(service="alpr")
_API_URL = "https://api.openalpr.com/v3/recognize_bytes"


@dataclass(frozen=True)
class ALPRVehicle:
    plate: Optional[str] = None
    plate_region: Optional[str] = None
    plate_confidence: float = 0.0
    make: Optional[str] = None
    model: Optional[str] = None
    make_model: Optional[str] = None
    color: Optional[str] = None
    body_type: Optional[str] = None
    year: Optional[str] = None
    orientation: Optional[str] = None

    # Confidence thresholds for each field from the raw ALPR response.
    color_confidence: float = 0.0
    make_confidence: float = 0.0
    make_model_confidence: float = 0.0
    body_type_confidence: float = 0.0

    def summary(self) -> str:
        """Human-readable summary for the vision LLM context.

        Only includes license plate data — ALPR vehicle make/model/color
        is unreliable from residential security camera angles so the
        vision LLM handles vehicle identification from the images instead.
        """
        parts: List[str] = []

        if self.plate and self.plate_confidence >= 75:
            region_str = self.plate_region.upper() if self.plate_region else ""
            plate_str = f"license plate: {self.plate} ({self.plate_confidence:.0f}% confident)"
            if region_str:
                plate_str += f" [{region_str}]"
            parts.append(plate_str)

        return ", ".join(parts) if parts else ""


@dataclass(frozen=True)
class ALPRResult:
    vehicles: List[ALPRVehicle] = field(default_factory=list)
    credit_cost: int = 0
    error: Optional[str] = None

    @property
    def best(self) -> Optional[ALPRVehicle]:
        return self.vehicles[0] if self.vehicles else None

    def summary(self) -> str:
        if not self.vehicles:
            return ""
        return "; ".join(v.summary() for v in self.vehicles if v.summary())


def _top_entry(entries: list) -> tuple:
    """Extract the top-confidence (name, confidence) from an ALPR result list."""
    if not entries:
        return (None, 0.0)
    best = max(entries, key=lambda x: x.get("confidence", 0))
    name = best.get("name", "")
    conf = best.get("confidence", 0.0)
    return (name if name else None, float(conf))


def _parse_vehicle(result: Dict) -> ALPRVehicle:
    """Parse a single result entry from the ALPR response."""
    plate = result.get("plate")
    region = result.get("region")
    plate_conf = result.get("confidence", 0.0)

    veh = result.get("vehicle") or {}
    make, make_conf = _top_entry(veh.get("make", []))
    model = None
    make_model_raw, make_model_conf = _top_entry(veh.get("make_model", []))
    if make_model_raw and "_" in make_model_raw:
        parts = make_model_raw.split("_", 1)
        make = parts[0]
        model = parts[1].replace("-", " ") if len(parts) > 1 else None

    color, color_conf = _top_entry(veh.get("color", []))
    body_type, body_type_conf = _top_entry(veh.get("body_type", []))
    year, _ = _top_entry(veh.get("year", []))
    orientation, _ = _top_entry(veh.get("orientation", []))

    return ALPRVehicle(
        plate=plate if plate else None,
        plate_region=region if region else None,
        plate_confidence=float(plate_conf),
        make=make,
        model=model,
        make_model=make_model_raw,
        color=color,
        body_type=body_type,
        year=year,
        orientation=orientation,
        color_confidence=color_conf,
        make_confidence=make_conf,
        make_model_confidence=make_model_conf,
        body_type_confidence=body_type_conf,
    )


async def recognize(
    jpeg_bytes: bytes,
    *,
    secret_key: str,
    region: str = "us",
    timeout_seconds: float = 8.0,
) -> ALPRResult:
    """Send a JPEG image to OpenALPR and return structured vehicle data."""
    if not secret_key:
        return ALPRResult(error="no_secret_key")

    b64 = base64.b64encode(jpeg_bytes)
    url = f"{_API_URL}?recognize_vehicle=1&country={region}&secret_key={secret_key}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(url, content=b64)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        _log.warning("alpr_request_failed", error=type(e).__name__, detail=str(e)[:200])
        return ALPRResult(error=type(e).__name__)

    if data.get("error"):
        _log.warning("alpr_api_error", error=str(data.get("error_code", "")))
        return ALPRResult(error=str(data.get("error_code", "unknown")))

    credit_cost = data.get("credit_cost", 0)
    vehicles: List[ALPRVehicle] = []

    for result in data.get("results", []):
        vehicles.append(_parse_vehicle(result))

    if not vehicles:
        for veh_entry in data.get("vehicles", []):
            details = veh_entry.get("details", {})
            color, color_c = _top_entry(details.get("color", []))
            make, make_c = _top_entry(details.get("make", []))
            mm, mm_c = _top_entry(details.get("make_model", []))
            bt, bt_c = _top_entry(details.get("body_type", []))
            year, _ = _top_entry(details.get("year", []))
            orientation, _ = _top_entry(details.get("orientation", []))
            vehicles.append(ALPRVehicle(
                color=color, make=make, make_model=mm,
                body_type=bt, year=year, orientation=orientation,
                color_confidence=color_c, make_confidence=make_c,
                make_model_confidence=mm_c, body_type_confidence=bt_c,
            ))

    _log.info("alpr_result",
              vehicles=len(vehicles),
              plate=vehicles[0].plate if vehicles else None,
              make_model=vehicles[0].make_model if vehicles else None,
              credit_cost=credit_cost)

    return ALPRResult(vehicles=vehicles, credit_cost=credit_cost)
