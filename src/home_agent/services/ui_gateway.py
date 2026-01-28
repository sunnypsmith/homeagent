from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


def _html_page(*, title: str, actions: list[dict[str, object]], toast: Optional[str]) -> str:
    # Single-file, dependency-free UI (no external assets) for iPhone.
    # Big touch targets, safe-area padding, and a simple “toast” area.
    cards = []
    # Built-in controls.
    cards.append(
        """
        <form method="post" action="/mute/60" class="card">
          <button type="submit" class="btn btn-danger" aria-label="Mute Sonos announcements for 1 hour">
            <span class="label">Mute (1 hour)</span>
          </button>
        </form>
        """
    )
    cards.append(
        """
        <form method="post" action="/unmute" class="card">
          <button type="submit" class="btn btn-subtle" aria-label="Unmute Sonos announcements">
            <span class="label">Unmute</span>
          </button>
        </form>
        """
    )
    for a in actions:
        aid = str(a.get("id") or "").strip()
        label = str(a.get("label") or "").strip()
        if not aid or not label:
            continue
        cards.append(
            f"""
            <form method="post" action="/a/{quote(aid)}" class="card">
              <button type="submit" class="btn" aria-label="{label}">
                <span class="label">{label}</span>
              </button>
            </form>
            """
        )
    cards_html = "\n".join(cards) if cards else "<p class='muted'>No actions configured.</p>"
    toast_html = f"<div class='toast'>{toast}</div>" if toast else ""

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="theme-color" content="#0b1220" />
    <title>{title}</title>
    <style>
      :root {{
        --bg: #0b1220;
        --card: rgba(255,255,255,0.06);
        --card2: rgba(255,255,255,0.10);
        --text: rgba(255,255,255,0.92);
        --muted: rgba(255,255,255,0.60);
        --accent: #5eead4;
        --danger: #fb7185;
        --shadow: 0 10px 25px rgba(0,0,0,0.35);
        --radius: 18px;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
        background: radial-gradient(1200px 800px at 20% -10%, rgba(94,234,212,0.20), transparent 50%),
                    radial-gradient(900px 700px at 110% 0%, rgba(56,189,248,0.18), transparent 55%),
                    var(--bg);
        color: var(--text);
      }}
      .wrap {{
        max-width: 820px;
        margin: 0 auto;
        padding: 18px;
        padding-top: calc(18px + env(safe-area-inset-top));
        padding-bottom: calc(24px + env(safe-area-inset-bottom));
      }}
      header {{
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 14px;
      }}
      h1 {{
        font-size: 20px;
        letter-spacing: 0.2px;
        margin: 0;
      }}
      .sub {{
        font-size: 12px;
        color: var(--muted);
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
      }}
      @media (min-width: 720px) {{
        .grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      }}
      .card {{
        margin: 0;
      }}
      .btn {{
        width: 100%;
        border: 1px solid rgba(255,255,255,0.10);
        background: linear-gradient(180deg, var(--card), rgba(255,255,255,0.03));
        color: var(--text);
        padding: 16px 14px;
        border-radius: var(--radius);
        box-shadow: var(--shadow);
        text-align: left;
        font-size: 16px;
        font-weight: 650;
        letter-spacing: 0.1px;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
        transition: transform 120ms ease, background 120ms ease, border-color 120ms ease;
      }}
      .btn-danger {{
        border-color: rgba(251,113,133,0.35);
        background: linear-gradient(180deg, rgba(251,113,133,0.18), rgba(255,255,255,0.03));
      }}
      .btn-subtle {{
        border-color: rgba(255,255,255,0.06);
        background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02));
        color: rgba(255,255,255,0.84);
      }}
      .btn:active {{
        transform: scale(0.98);
        background: linear-gradient(180deg, var(--card2), rgba(255,255,255,0.04));
        border-color: rgba(94,234,212,0.35);
      }}
      .label {{ display: block; line-height: 1.15; }}
      .muted {{ color: var(--muted); font-size: 13px; margin-top: 12px; }}
      .toast {{
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 14px;
        border: 1px solid rgba(94,234,212,0.25);
        background: rgba(94,234,212,0.08);
        color: var(--text);
        font-size: 13px;
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <header>
        <h1>{title}</h1>
        <div class="sub">Tap a button</div>
      </header>
      <div class="grid">
        {cards_html}
      </div>
      {toast_html}
      <div class="muted">
        Tip: add this page to your Home Screen (Share → Add to Home Screen).
      </div>
    </div>
  </body>
</html>
"""


async def run_ui_gateway() -> None:
    """
    Simple LAN web UI that publishes MQTT events (no auth, LAN-only by config).
    """
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="ui_gateway")

    if not settings.ui.enabled:
        log.warning("ui_disabled", hint="Set UI_ENABLED=true to run ui-gateway")
        return

    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, RedirectResponse
        import uvicorn
    except Exception as e:  # pragma: no cover
        raise RuntimeError("UI deps not installed. Run: pip install -e '.[ui]'") from e

    actions = settings.ui.actions_list()
    by_id: dict[str, dict[str, object]] = {}
    for a in actions:
        aid = str(a.get("id") or "").strip()
        if aid:
            by_id[aid] = a

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-ui-gateway",
    )

    app = FastAPI()

    @app.on_event("startup")
    async def _startup() -> None:
        await mqttc.connect()
        log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await mqttc.close()

    @app.get("/", response_class=HTMLResponse)
    async def index(toast: Optional[str] = None) -> str:
        return _html_page(title=settings.ui.title, actions=actions, toast=toast)

    @app.post("/a/{action_id}")
    async def trigger(action_id: str) -> RedirectResponse:
        a = by_id.get(action_id)
        if not a:
            return RedirectResponse(url="/?toast=" + quote("Unknown action"), status_code=303)

        data: Dict[str, Any] = {"text": str(a.get("text") or "")}
        if isinstance(a.get("targets"), list):
            data["targets"] = list(a["targets"])  # type: ignore[index]
        if isinstance(a.get("volume"), int):
            data["volume"] = int(a["volume"])  # type: ignore[index]
        if isinstance(a.get("concurrency"), int):
            data["concurrency"] = int(a["concurrency"])  # type: ignore[index]

        topic = f"{settings.mqtt.base_topic}/announce/request"
        evt = make_event(source="ui-gateway", typ="announce.request", data=data)
        mqttc.publish_json(topic, evt)
        log.info("action_triggered", action=action_id)
        return RedirectResponse(url="/?toast=" + quote("Sent: " + str(a.get("label") or action_id)), status_code=303)

    @app.post("/mute/{minutes}")
    async def mute(minutes: int) -> RedirectResponse:
        mins = int(minutes)
        if mins <= 0:
            return RedirectResponse(url="/?toast=" + quote("Minutes must be > 0"), status_code=303)

        now = datetime.now(timezone.utc)
        muted_until = now + timedelta(minutes=mins)
        data: Dict[str, Any] = {
            "duration_minutes": mins,
            "muted_until_unix": int(muted_until.timestamp()),
        }

        topic = f"{settings.mqtt.base_topic}/announce/mute"
        evt = make_event(source="ui-gateway", typ="announce.mute", data=data)
        mqttc.publish_json(topic, evt, retain=True)
        log.info("mute_requested", minutes=mins, muted_until=str(muted_until))
        return RedirectResponse(url="/?toast=" + quote(f"Muted for {mins} minutes"), status_code=303)

    @app.post("/unmute")
    async def unmute() -> RedirectResponse:
        data: Dict[str, Any] = {"muted_until_unix": 0}
        topic = f"{settings.mqtt.base_topic}/announce/mute"
        evt = make_event(source="ui-gateway", typ="announce.mute", data=data)
        mqttc.publish_json(topic, evt, retain=True)
        log.info("unmute_requested")
        return RedirectResponse(url="/?toast=" + quote("Unmuted"), status_code=303)

    config = uvicorn.Config(
        app,
        host=str(settings.ui.bind_host),
        port=int(settings.ui.port),
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    log.info("ui_listening", host=settings.ui.bind_host, port=settings.ui.port)
    await server.serve()


def main() -> int:
    asyncio.run(run_ui_gateway())
    return 0

