from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import httpx


@dataclass(frozen=True)
class DashboardMetrics:
    last_24h: Optional[int]
    last_30d_avg: Optional[int]
    spot_arr: Optional[int]


class DashboardScraper:
    def __init__(
        self,
        *,
        url: str,
        llm_base_url: str,
        llm_api_key: str,
        vision_model: str = "meta-llama/llama-4-maverick-17b-128e-instruct",
        wait_seconds: float = 8.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._url = url
        self._llm_base_url = llm_base_url.rstrip("/")
        self._llm_api_key = llm_api_key
        self._vision_model = vision_model
        self._wait_seconds = float(wait_seconds)
        self._timeout = float(timeout_seconds)

    async def fetch_metrics(self) -> DashboardMetrics:
        screenshot_bytes = await self._take_screenshot()
        raw = await self._ask_llm(screenshot_bytes)
        return self._parse(raw)

    async def _take_screenshot(self) -> bytes:
        from playwright.async_api import async_playwright

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = Path(f.name)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                await page.goto(self._url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(self._wait_seconds)
                await page.screenshot(path=str(path), full_page=True)
                await browser.close()
            return path.read_bytes()
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _ask_llm(self, image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt = (
            "Extract these three values from this dashboard screenshot:\n"
            "1. Last 24 Hours\n"
            "2. Last 30 Days Average\n"
            "3. Spot ARR\n\n"
            "Return ONLY valid JSON with these exact keys: "
            '{"last_24h": 3148, "last_30d_avg": 2813, "spot_arr": 27573589}\n'
            "Round all values to whole dollars (no cents). No other text."
        )

        headers = {
            "Authorization": "Bearer %s" % self._llm_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._vision_model,
            "max_tokens": 256,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,%s" % b64},
                        },
                    ],
                }
            ],
        }

        url = "%s/chat/completions" % self._llm_base_url
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content") or ""
        return ""

    def _parse(self, raw: str) -> DashboardMetrics:
        text = raw.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data: Dict[str, Any] = json.loads(text)
        except Exception:
            return DashboardMetrics(last_24h=None, last_30d_avg=None, spot_arr=None)

        return DashboardMetrics(
            last_24h=_to_int(data.get("last_24h")),
            last_30d_avg=_to_int(data.get("last_30d_avg")),
            spot_arr=_to_int(data.get("spot_arr")),
        )


def _to_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except Exception:
        return None
