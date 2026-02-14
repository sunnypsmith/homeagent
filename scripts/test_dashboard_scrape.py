#!/usr/bin/env python3
"""
Test script: screenshot a web dashboard and send to an LLM for value extraction.

Usage:
    python scripts/test_dashboard_scrape.py [URL]

Defaults to the Massed Compute dashboard URL if none provided.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


DEFAULT_URL = (
    "http://wave.massedcompute.com:10101/"
    "8cdeef14-2ad7-4c1f-bc61-480685f8c4f7"
    "4261b63e-0d7d-497e-93ea-22fda73eaf7b"
    "1f298e2b-4bd3-48c2-9b48-455729984e41"
    "/massed-compute"
)

SCREENSHOT_PATH = Path(__file__).resolve().parent / "dashboard_screenshot.png"


async def take_screenshot(url: str, output: Path, wait_seconds: float = 8.0) -> Path:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto(url, wait_until="networkidle", timeout=30000)
        # Extra wait for JS rendering
        await asyncio.sleep(wait_seconds)
        await page.screenshot(path=str(output), full_page=True)
        await browser.close()
    return output


async def ask_llm_about_image(
    *,
    image_path: Path,
    settings: AppSettings,
    prompt: str,
) -> str:
    image_bytes = image_path.read_bytes()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    headers = {
        "Authorization": "Bearer %s" % settings.llm.api_key,
        "Content-Type": "application/json",
    }

    vision_model = "meta-llama/llama-4-maverick-17b-128e-instruct"

    payload = {
        "model": vision_model,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,%s" % b64,
                        },
                    },
                ],
            }
        ],
    }

    url = "%s/chat/completions" % settings.llm.base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content") or ""
    return ""


async def main_async(url: str) -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="dashboard_scrape_test")

    log.info("taking_screenshot", url=url)
    screenshot = await take_screenshot(url, SCREENSHOT_PATH)
    log.info("screenshot_saved", path=str(screenshot))

    prompt = (
        "Extract these three values from this dashboard screenshot:\n"
        "1. Last 24 Hours\n"
        "2. Last 30 Days Average\n"
        "3. Spot ARR\n\n"
        "Return ONLY valid JSON with these exact keys: "
        '{"last_24h": 3148, "last_30d_avg": 2813, "spot_arr": 27573589}\n'
        "Round all values to whole dollars (no cents). No other text."
    )

    log.info("sending_to_llm", model=settings.llm.model)
    result = await ask_llm_about_image(
        image_path=screenshot,
        settings=settings,
        prompt=prompt,
    )

    print("\n--- LLM extracted values ---")
    print(result)
    print("---")

    # Try to parse as JSON
    import json
    try:
        data = json.loads(result.strip())
        print("\nParsed:")
        print("  Last 24 Hours:      $%s" % "{:,}".format(data.get("last_24h", 0)))
        print("  Last 30 Days Avg:   $%s" % "{:,}".format(data.get("last_30d_avg", 0)))
        print("  Spot ARR:           $%s" % "{:,}".format(data.get("spot_arr", 0)))
    except Exception as e:
        print("(could not parse JSON: %s)" % e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot a dashboard and extract values via LLM.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="Dashboard URL")
    args = parser.parse_args()

    asyncio.run(main_async(args.url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
