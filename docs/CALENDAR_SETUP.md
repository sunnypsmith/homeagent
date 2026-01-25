# Calendar setup (ICS feed)

This project can include **today’s calendar events** in the morning briefing by reading an **ICS feed URL** (no OAuth).

The morning briefing agent reads calendar events at runtime and sends a compact “calendar JSON” to the LLM. The LLM is instructed to only mention events from that JSON.

## Install calendar support

Calendar parsing requires optional dependencies:

```bash
pip install -e ".[gcal]"
```

## Configure `.env`

```bash
GCAL_ENABLED=true
GCAL_ICS_URL=<your_ics_url_here>
GCAL_POLL_SECONDS=600
GCAL_LOOKAHEAD_DAYS=2
```

Notes:
- Treat `GCAL_ICS_URL` like a **password** (bearer-secret).
- `GCAL_LOOKAHEAD_DAYS` controls how many days we fetch (the briefing currently only speaks events that **start today**).

## Option A: Google Calendar ICS (no OAuth)

Use the calendar’s **Secret address in iCal format** and set it as `GCAL_ICS_URL`.

## Option B: Apple iCloud Calendar (Public Calendar)

Apple Calendar can publish a “Public Calendar” ICS URL.

Typical flow:
- Apple Calendar → right click the calendar → **Share Calendar…**
- Enable **Public Calendar**
- Copy the link (often `webcal://…`)

You can paste either:
- `webcal://…` (supported; we normalize to HTTPS internally), or
- `https://…`

If you find the iCloud URL only works over `http://` on your network, you can use `http://` as well.

## Validate the feed (recommended)

From the machine/container running Home Agent:

```bash
python3 -c "import httpx; from home_agent.config import AppSettings; u=AppSettings().gcal.ics_url.replace('webcal://','https://'); r=httpx.get(u, follow_redirects=True, timeout=20); print(r.status_code, r.headers.get('content-type'), len(r.content))"
```

You want to see:
- status `200`
- content type including `text/calendar`
- non-zero length

## Test end-to-end (hear it)

1) Ensure the morning briefing agent is running:

```bash
home-agent morning-briefing-agent
```

2) Trigger a briefing immediately:

```bash
home-agent trigger-morning-briefing
```

If you don’t hear calendar items:
- restart `home-agent morning-briefing-agent` (it loads `.env` at startup)
- confirm the calendar feed returns `200` (see validation above)

