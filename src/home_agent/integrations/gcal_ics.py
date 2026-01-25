from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: Optional[str] = None


class GoogleCalendarIcsClient:
    """
    Lightweight Google Calendar reader via ICS feed URL (no OAuth).

    Notes:
    - The ICS URL is effectively a bearer secret; do not log it.
    - Requires optional deps if you want recurrence expansion:
        - icalendar
        - recurring-ical-events
    """

    def __init__(self, *, ics_url: str, timeout_seconds: float = 20.0) -> None:
        u = (ics_url or "").strip()
        # iCloud "Public Calendar" often provides a webcal:// URL. Treat it as HTTPS.
        if u.lower().startswith("webcal://"):
            u = "https://" + u[len("webcal://") :]
        self._timeout = float(timeout_seconds)
        self._ics_url = u

    async def fetch_events(
        self,
        *,
        tz: ZoneInfo,
        start_date: date,
        days: int = 1,
        max_events: int = 25,
    ) -> List[CalendarEvent]:
        """
        Fetch calendar events for an inclusive window:
          [start_date 00:00, start_date+days 00:00)
        """
        if not self._ics_url:
            return []

        window_start = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
        window_end = window_start + timedelta(days=max(1, int(days)))

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            try:
                resp = await client.get(self._ics_url)
            except httpx.HTTPError as e:
                # Don't leak the URL (it is effectively a bearer secret).
                raise RuntimeError("Failed to fetch calendar feed (%s)" % type(e).__name__) from e

            if resp.status_code >= 400:
                # Don't leak the URL (it is effectively a bearer secret).
                raise RuntimeError("Calendar feed returned HTTP %d" % resp.status_code)

            ics_text = resp.text

        # Optional deps; import lazily.
        try:
            from icalendar import Calendar  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError("Missing dependency: icalendar. Install: pip install -e '.[gcal]'") from e

        cal = Calendar.from_ical(ics_text)

        # Expand recurrences if possible; fall back to simple VEVENT scan otherwise.
        events: List[CalendarEvent] = []

        try:
            import recurring_ical_events  # type: ignore

            components = recurring_ical_events.of(cal).between(window_start, window_end)
        except Exception:
            # No recurrence support; just read VEVENTs with DTSTART in range.
            components = [c for c in cal.walk() if getattr(c, "name", "") == "VEVENT"]

        for c in components:
            try:
                summary = str(c.get("SUMMARY") or "").strip()
                if not summary:
                    summary = "(Untitled)"

                loc = c.get("LOCATION")
                location = str(loc).strip() if loc is not None and str(loc).strip() else None

                dtstart = c.decoded("DTSTART") if hasattr(c, "decoded") else None
                dtend = c.decoded("DTEND") if hasattr(c, "decoded") else None

                all_day = False
                if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
                    # All-day events are date-only; interpret in local tz.
                    all_day = True
                    start_dt = datetime.combine(dtstart, datetime.min.time(), tzinfo=tz)
                    if isinstance(dtend, date) and not isinstance(dtend, datetime):
                        end_dt = datetime.combine(dtend, datetime.min.time(), tzinfo=tz)
                    else:
                        end_dt = start_dt + timedelta(days=1)
                else:
                    if not isinstance(dtstart, datetime):
                        continue
                    start_dt = dtstart
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=tz)
                    else:
                        start_dt = start_dt.astimezone(tz)

                    if isinstance(dtend, datetime):
                        end_dt = dtend
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=tz)
                        else:
                            end_dt = end_dt.astimezone(tz)
                    else:
                        # Some events omit DTEND; assume 1 hour.
                        end_dt = start_dt + timedelta(hours=1)

                # Filter to window.
                if start_dt >= window_end or end_dt <= window_start:
                    continue

                events.append(
                    CalendarEvent(
                        title=summary,
                        start=start_dt,
                        end=end_dt,
                        all_day=all_day,
                        location=location,
                    )
                )
            except Exception:
                continue

        events.sort(key=lambda e: (e.start, e.end, e.title))
        return events[: max(1, int(max_events))]

