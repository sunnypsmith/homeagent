from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass(frozen=True)
class NewsHeadline:
    title: str
    url: Optional[str]


@dataclass(frozen=True)
class NewsFeedResult:
    label: str
    headlines: List[NewsHeadline]


async def fetch_json_feed(
    *,
    url: str,
    label: str = "News",
    max_items: int = 5,
    timeout_seconds: float = 15.0,
) -> NewsFeedResult:
    """
    Fetch a JSON Feed (https://jsonfeed.org/version/1.1) and return headlines.
    """
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items") or []
    headlines: List[NewsHeadline] = []
    for item in items[:max_items]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        link = item.get("url") or item.get("link") or None
        if isinstance(link, str):
            link = link.strip() or None
        headlines.append(NewsHeadline(title=title, url=link))

    return NewsFeedResult(label=label, headlines=headlines)


async def fetch_all_feeds(
    feeds: List[Dict[str, str]],
    *,
    max_items: int = 5,
    timeout_seconds: float = 15.0,
) -> List[NewsFeedResult]:
    """
    Fetch multiple feeds. Failures are silently skipped.
    """
    results: List[NewsFeedResult] = []
    for feed in feeds:
        url = feed.get("url") or ""
        label = feed.get("label") or "News"
        if not url:
            continue
        try:
            result = await fetch_json_feed(
                url=url,
                label=label,
                max_items=max_items,
                timeout_seconds=timeout_seconds,
            )
            results.append(result)
        except Exception:
            continue
    return results
