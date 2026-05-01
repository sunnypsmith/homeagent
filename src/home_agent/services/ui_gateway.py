from __future__ import annotations

import asyncio
import collections
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from home_agent.bus.envelope import make_event
from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.audio_host import AudioHost
from home_agent.integrations.sonos_playback import SonosPlayback

_MAX_ERRORS = 50
_MAX_FEED = 100
_TOPIC_WINDOW = 50_000

_latest_health: Dict[str, Any] = {}
_recent_errors: collections.deque = collections.deque(maxlen=_MAX_ERRORS)
_source_stats: Dict[str, Dict[str, Any]] = {}
_recent_feed: collections.deque = collections.deque(maxlen=_MAX_FEED)
_topic_events: collections.deque = collections.deque(maxlen=_TOPIC_WINDOW)
_db_activity: Dict[str, Any] = {}
_voice_rooms: Dict[str, Dict[str, Any]] = {}
_voice_commands: collections.deque = collections.deque(maxlen=20)
_voice_responses: collections.deque = collections.deque(maxlen=20)
_chat_history: collections.deque = collections.deque(maxlen=50)


def _update_source(source: str, typ: str, topic: str) -> None:
    now = time.time()
    st = _source_stats.get(source)
    if st is None:
        st = {"source": source, "total": 0, "last_ts": 0.0, "last_type": "", "last_topic": "", "seen": collections.deque(maxlen=10_000)}
        _source_stats[source] = st
    st["total"] += 1
    st["last_ts"] = now
    st["last_type"] = typ
    st["last_topic"] = topic
    st["seen"].append(now)


def _source_rate(st: Dict[str, Any]) -> float:
    seen = st.get("seen")
    if not seen:
        return 0.0
    now = time.time()
    while seen and (now - seen[0]) > 60.0:
        seen.popleft()
    return len(seen) / 60.0


def _top_topics(limit: int = 10) -> List[Dict[str, Any]]:
    now = time.time()
    while _topic_events and (now - _topic_events[0][0]) > 60.0:
        _topic_events.popleft()
    counts: Dict[str, int] = {}
    for _, topic in _topic_events:
        counts[topic] = counts.get(topic, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"topic": t, "count": c, "rate": round(c / 60.0, 2)} for t, c in top]


_ui_db_conn: Any = None


def _get_ui_db(conninfo: str) -> Any:
    """Return a persistent DB connection for the UI dashboard polls."""
    global _ui_db_conn
    try:
        import psycopg
    except ImportError:
        return None
    if _ui_db_conn is not None:
        try:
            if not _ui_db_conn.closed:
                return _ui_db_conn
        except Exception:
            pass
        try:
            _ui_db_conn.close()
        except Exception:
            pass
    try:
        _ui_db_conn = psycopg.connect(conninfo, autocommit=True)
        return _ui_db_conn
    except Exception:
        _ui_db_conn = None
        return None


def _reset_ui_db() -> None:
    global _ui_db_conn
    if _ui_db_conn is not None:
        try:
            _ui_db_conn.close()
        except Exception:
            pass
        _ui_db_conn = None


def _fetch_db_activity_cached(settings: Any) -> Dict[str, Any]:
    cached = _db_activity.get("_cached_at", 0.0)
    if (time.time() - cached) < 5.0 and _db_activity.get("rows") is not None:
        return _db_activity
    conn = _get_ui_db(settings.db.conninfo)
    if conn is None:
        return _db_activity
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT now(), (SELECT max(ingested_at) FROM events),
                       (SELECT count(*) FROM events WHERE ingested_at > now() - interval '60 seconds')
            """)
            now_utc, last_at, last_60 = cur.fetchone()
            cur.execute("SELECT ingested_at, topic, source, type FROM events WHERE ingested_at > now() - interval '1 hour' AND type NOT IN ('service.heartbeat', 'voice.room_status', 'watchdog.health', 'service.error', 'raw') ORDER BY ingested_at DESC LIMIT 8")
            rows = cur.fetchall()
        age_s = None
        if last_at and now_utc:
            age_s = round(max(0.0, (now_utc - last_at).total_seconds()), 1)
        result = {
            "last_ingest_age_s": age_s,
            "events_last_60s": int(last_60 or 0),
            "rows": [{"age_s": round(max(0, (now_utc - r[0]).total_seconds()), 1) if r[0] else None,
                       "topic": str(r[1] or ""), "source": str(r[2] or ""), "type": str(r[3] or "")} for r in (rows or [])],
            "_cached_at": time.time(),
        }
        _db_activity.clear()
        _db_activity.update(result)
        return _db_activity
    except Exception:
        _reset_ui_db()
        return _db_activity


# ---------------------------------------------------------------------------
# Shared CSS variables (used by both pages)
# ---------------------------------------------------------------------------
_CSS_VARS = """
    :root{
      --bg:#0a0e1a;--surface:#12172a;--surface2:#1a2038;
      --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.14);
      --text:rgba(255,255,255,0.92);--dim:rgba(255,255,255,0.45);
      --green:#34d399;--cyan:#22d3ee;--blue:#60a5fa;--red:#f87171;--yellow:#fbbf24;
      --mono:'SF Mono',SFMono-Regular,ui-monospace,'Cascadia Mono',Menlo,Consolas,monospace;
      --r:14px;
    }
    *{box-sizing:border-box;margin:0}
    body{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Inter,sans-serif;
      background:var(--bg);color:var(--text);min-height:100vh;
    }
"""



def _get_system_stats() -> Dict[str, Any]:
    import os
    try:
        load1, load5, load15 = os.getloadavg()
        cpus = os.cpu_count() or 1
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0].rstrip(':') in ('MemTotal', 'MemAvailable'):
                    mem[parts[0].rstrip(':')] = int(parts[1])
        mem_total = mem.get('MemTotal', 0) / 1024 / 1024
        mem_avail = mem.get('MemAvailable', 0) / 1024 / 1024
        mem_used = mem_total - mem_avail
        st = os.statvfs('/')
        disk_total = st.f_blocks * st.f_frsize / 1024 / 1024 / 1024
        disk_free = st.f_bavail * st.f_frsize / 1024 / 1024 / 1024
        disk_used = disk_total - disk_free
        return {
            "cpu_cores": cpus,
            "load_1m": round(load1, 2),
            "load_5m": round(load5, 2),
            "load_15m": round(load15, 2),
            "mem_used_gb": round(mem_used, 1),
            "mem_total_gb": round(mem_total, 1),
            "mem_pct": round(100 * mem_used / max(1, mem_total)),
            "disk_used_gb": round(disk_used, 1),
            "disk_total_gb": round(disk_total, 1),
            "disk_pct": round(100 * disk_used / max(1, disk_total)),
        }
    except Exception:
        return {}


def _html_page(*, title: str, actions: list[dict[str, object]], toast: Optional[str]) -> str:
    cards = []
    cards.append(
        '<form method="post" action="/mute/60" class="card">'
        '<button type="submit" class="btn btn-danger" aria-label="Mute 1 hour">'
        '<span class="ico">&#x1f507;</span><span class="label">Mute (1 hour)</span>'
        '</button></form>'
    )
    cards.append(
        '<form method="post" action="/mute/120" class="card">'
        '<button type="submit" class="btn btn-danger" aria-label="Mute 2 hours">'
        '<span class="ico">&#x1f507;</span><span class="label">Mute (2 hours)</span>'
        '</button></form>'
    )
    cards.append(
        '<form method="post" action="/unmute" class="card">'
        '<button type="submit" class="btn btn-subtle" aria-label="Unmute">'
        '<span class="ico">&#x1f50a;</span><span class="label">Unmute</span>'
        '</button></form>'
    )
    for a in actions:
        aid = str(a.get("id") or "").strip()
        label = str(a.get("label") or "").strip()
        if not aid or not label:
            continue
        cards.append(
            f'<form method="post" action="/a/{quote(aid)}" class="card">'
            f'<button type="submit" class="btn" aria-label="{label}">'
            f'<span class="label">{label}</span>'
            f'</button></form>'
        )
    cards.append(
        '<form method="post" action="/tone-test" class="card">'
        '<button type="submit" class="btn btn-subtle" aria-label="Test Tone">'
        '<span class="ico">&#x1f514;</span><span class="label">Test Tone (10s)</span>'
        '</button></form>'
    )
    cards_html = "\n".join(cards) if cards else "<p class='dim'>No actions configured.</p>"
    toast_html = f"<div class='toast'>{toast}</div>" if toast else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
  <meta name="theme-color" content="#0a0e1a"/>
  <title>{title}</title>
  <style>
    {_CSS_VARS}
    .w{{
      max-width:820px;margin:0 auto;padding:16px;
      padding-top:calc(16px + env(safe-area-inset-top));
      padding-bottom:calc(24px + env(safe-area-inset-bottom));
    }}
    header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}}
    h1{{font-size:18px;font-weight:700;letter-spacing:.3px}}
    .nav{{display:flex;gap:10px;align-items:center}}
    .nav a{{color:var(--blue);font-size:12px;text-decoration:none}}
    .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}
    @media(min-width:600px){{.grid{{grid-template-columns:repeat(3,1fr)}}}}
    .card{{margin:0}}
    .btn{{
      width:100%;display:flex;align-items:center;gap:10px;
      border:1px solid var(--border);background:var(--surface);
      color:var(--text);padding:14px;border-radius:var(--r);
      text-align:left;font-size:15px;font-weight:600;letter-spacing:.1px;
      cursor:pointer;-webkit-tap-highlight-color:transparent;
      transition:transform 100ms ease,border-color 150ms ease,background 150ms ease;
    }}
    .btn:active{{transform:scale(0.97);border-color:rgba(52,211,153,0.35);background:var(--surface2)}}
    .btn-danger{{border-color:rgba(248,113,113,0.25);background:linear-gradient(180deg,rgba(248,113,113,0.10),var(--surface))}}
    .btn-danger:active{{border-color:rgba(248,113,113,0.5)}}
    .btn-subtle{{border-color:rgba(255,255,255,0.04);color:rgba(255,255,255,0.75)}}
    .ico{{font-size:18px;flex-shrink:0}}
    .label{{display:block;line-height:1.2}}
    .toast{{
      margin-top:14px;padding:10px 14px;border-radius:var(--r);
      border:1px solid rgba(52,211,153,0.25);background:rgba(52,211,153,0.08);
      color:var(--text);font-size:13px;
    }}
    .foot{{color:var(--dim);font-size:12px;margin-top:16px;display:flex;gap:6px;align-items:center}}
    .foot a{{color:var(--blue);text-decoration:none}}
  </style>
</head>
<body>
  <div class="w">
    <header>
      <h1>{title}</h1>
      <div class="nav"><a href="/chat">Chat</a> &middot; <a href="/status">Status</a></div>
    </header>
    <div class="grid">
      {cards_html}
    </div>
    {toast_html}
    <div class="foot">Add to Home Screen for quick access</div>
  </div>
</body>
</html>
"""


def _chat_html(*, title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#0a0e1a"/>
<title>{title} — Chat</title>
<style>
{_CSS_VARS}
.w{{max-width:700px;margin:0 auto;padding:12px;padding-top:calc(12px + env(safe-area-inset-top));padding-bottom:calc(80px + env(safe-area-inset-bottom));display:flex;flex-direction:column;min-height:100vh}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-shrink:0}}
h1{{font-size:17px;font-weight:700}}
.nav a{{color:var(--blue);font-size:12px;text-decoration:none}}
.chat{{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding-bottom:8px}}
.msg{{max-width:85%;padding:10px 14px;border-radius:14px;font-size:14px;line-height:1.5;word-wrap:break-word}}
.msg.user{{align-self:flex-end;background:var(--blue);color:#fff;border-bottom-right-radius:4px}}
.msg.assistant{{align-self:flex-start;background:var(--surface);border:1px solid var(--border);border-bottom-left-radius:4px}}
.msg .meta{{font-size:10px;color:var(--dim);margin-top:4px}}
.input-bar{{position:fixed;bottom:0;left:0;right:0;padding:10px 12px;padding-bottom:calc(10px + env(safe-area-inset-bottom));background:var(--bg);border-top:1px solid var(--border);display:flex;gap:8px}}
.input-bar input{{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:15px;outline:none}}
.input-bar input:focus{{border-color:var(--blue)}}
.input-bar button{{background:var(--blue);color:#fff;border:none;border-radius:10px;padding:10px 18px;font-size:15px;font-weight:600;cursor:pointer}}
.empty{{color:var(--dim);text-align:center;margin-top:40px;font-size:14px}}
</style>
</head>
<body>
<div class="w">
<header><h1>Higgins Chat</h1><div class="nav"><a href="/">Controls</a> &middot; <a href="/status">Status</a></div></header>
<div class="chat" id="chat"><div class="empty">Say something to Higgins...</div></div>
</div>
<div class="input-bar">
<input type="text" id="input" placeholder="Type a command or question..." autocomplete="off"/>
<button id="send">Send</button>
</div>
<script>
(function(){{
const chat=document.getElementById('chat');
const input=document.getElementById('input');
const send=document.getElementById('send');
let lastHash="";

function ts(t){{if(!t)return'';try{{return new Date(t).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}})}}catch(e){{return''}}}}
function esc(s){{const d=document.createElement('div');d.textContent=s;return d.innerHTML}}

async function sendMsg(){{
  const text=input.value.trim();
  if(!text)return;
  input.value='';
  // Add user message immediately
  chat.innerHTML+=`<div class="msg user">${{esc(text)}}</div>`;
  chat.scrollTop=chat.scrollHeight;
  try{{
    await fetch('/api/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text:text}})}});
  }}catch(e){{}}
}}

async function poll(){{
  try{{
    const r=await fetch('/api/health');
    const d=await r.json();
    const hist=d.chat_history||[];
    const newHash=JSON.stringify(hist.slice(0,10));
    if(newHash!==lastHash){{
      lastHash=newHash;
      const reversed=[...hist].reverse();
      let h='';
      for(const m of reversed){{
        if(m.role==='user'){{
          h+=`<div class="msg user">${{esc(m.text)}}${{m.room?`<div class="meta">${{esc(m.room)}} ${{ts(m.ts)}}</div>`:``}}</div>`;
        }}else{{
          h+=`<div class="msg assistant">${{esc(m.text)}}<div class="meta">${{ts(m.ts)}}</div></div>`;
        }}
      }}
      chat.innerHTML=h||'<div class="empty">Say something to Higgins...</div>';
      chat.scrollTop=chat.scrollHeight;
    }}
  }}catch(e){{}}
}}

send.addEventListener('click',sendMsg);
input.addEventListener('keydown',e=>{{if(e.key==='Enter')sendMsg()}});
poll();setInterval(poll,2000);
input.focus();
}})();
</script>
</body>
</html>"""


def _status_html(*, title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#0a0e1a"/>
<title>{title} — Status</title>
<style>
{_CSS_VARS}
.w{{max-width:1100px;margin:0 auto;padding:12px;padding-top:calc(12px + env(safe-area-inset-top));padding-bottom:calc(20px + env(safe-area-inset-bottom))}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
h1{{font-size:17px;font-weight:700;display:flex;align-items:center;gap:8px}}
.pulse{{width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block;animation:p 2s infinite}}
@keyframes p{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.nav a{{color:var(--blue);font-size:12px;text-decoration:none}}
.vitals{{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:11px;font-family:var(--mono);color:var(--dim);margin-bottom:12px}}
.vitals b{{color:var(--text);font-weight:500}}
.vitals .g{{color:var(--green)}}.vitals .y{{color:#fbbf24}}.vitals .r{{color:#f87171}}
.section{{margin-bottom:16px}}
.sh{{font-size:13px;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px}}
.sh .ct{{font-size:10px;font-weight:700;font-family:var(--mono);padding:1px 6px;border-radius:7px;background:rgba(52,211,153,.12);color:var(--green)}}
.sh .ct.warn{{background:rgba(248,113,113,.15);color:#f87171}}
.svc-strip{{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;margin-bottom:4px}}
.svc-strip .s{{display:flex;align-items:center;gap:4px;color:var(--dim)}}
.svc-strip .s b{{color:var(--text);font-weight:500}}
.dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
.dot.ok{{background:var(--green)}}.dot.err{{background:#fbbf24}}.dot.dn{{background:#f87171}}.dot.u{{background:var(--dim)}}
.vgrid{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}
@media(min-width:540px){{.vgrid{{grid-template-columns:repeat(3,1fr)}}}}
@media(min-width:800px){{.vgrid{{grid-template-columns:repeat(5,1fr)}}}}
.vc{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 12px}}
.vc .vn{{font-size:13px;font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:5px}}
.vc .vs{{font-size:10px;color:var(--dim);font-family:var(--mono);line-height:1.6}}
.vc .vs b{{color:var(--text);font-weight:500}}
.pan{{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}}
table{{width:100%;border-collapse:collapse;font-size:11px;font-family:var(--mono)}}
th{{text-align:left;color:var(--dim);font-weight:500;padding:6px 10px;border-bottom:1px solid var(--border)}}
td{{padding:5px 10px;border-bottom:1px solid rgba(255,255,255,.03);color:var(--dim)}}
td b{{color:var(--text);font-weight:500}}
tr:last-child td{{border-bottom:none}}
.err-row{{padding:8px 12px;border-bottom:1px solid var(--border);font-size:11px}}
.err-row:last-child{{border-bottom:none}}
.ets{{color:var(--dim);font-family:var(--mono);font-size:10px}}
.esvc{{color:#fbbf24;font-weight:600}}
.emsg{{color:#f87171;font-family:var(--mono);font-size:10px;margin-top:2px;word-break:break-all}}
.etb{{color:var(--dim);font-family:var(--mono);font-size:9px;margin-top:3px;white-space:pre-wrap;max-height:80px;overflow-y:auto;background:rgba(0,0,0,.3);padding:5px 7px;border-radius:7px}}
.empty{{color:var(--dim);font-size:12px;padding:14px;text-align:center}}
.cols{{display:grid;grid-template-columns:1fr;gap:12px}}
@media(min-width:700px){{.cols{{grid-template-columns:1fr 1fr}}}}
details{{margin-bottom:12px}}
summary{{cursor:pointer;font-size:13px;font-weight:600;color:var(--dim);padding:6px 0}}
summary:hover{{color:var(--text)}}
</style>
</head>
<body>
<div class="w">
<header>
<h1><span class="pulse" id="pulse"></span>System Status</h1>
<div class="nav"><a href="/chat">Chat</a> &middot; <a href="/live-audio">Live</a> &middot; <a href="/audio-debug">Audio</a> &middot; <a href="/">Controls</a></div>
</header>
<div class="vitals" id="vitals">Loading...</div>

<div class="section" id="err-section" style="display:none">
<div class="sh">Errors <span class="ct warn" id="err-badge">0</span> <button id="err-clear" onclick="fetch('/api/clear-errors',{{method:'POST'}}).then(()=>refresh())" style="display:none;background:none;border:1px solid var(--dim);color:var(--dim);border-radius:6px;padding:1px 8px;font-size:10px;cursor:pointer;margin-left:6px">Clear</button></div>
<div class="pan" id="errors" style="max-height:200px;overflow-y:auto"></div>
</div>

<div class="section">
<div class="sh">Services <span class="ct" id="svc-ct">0</span></div>
<div class="svc-strip" id="svcs"></div>
</div>

<div class="section">
<div class="sh">Voice Assistants</div>
<div class="vgrid" id="voice"></div>
</div>

<div class="section">
<div class="sh">Voice Transcript</div>
<div class="pan" id="cmds" style="max-height:300px;overflow-y:auto"><div class="empty">No voice activity yet</div></div>
</div>

<div class="cols">
<div class="section">
<div class="sh">Live Feed</div>
<div class="pan" id="feed"><div class="empty">Loading...</div></div>
</div>
<div class="section">
<div class="sh">Database <span class="ct" id="db-ct">&#x2014;</span></div>
<div class="pan" id="db"><div class="empty">Loading...</div></div>
</div>
</div>

<details>
<summary>MQTT Details</summary>
<div class="cols">
<div class="pan" id="src-tbl"><div class="empty">Loading...</div></div>
<div class="pan" id="top-tbl"><div class="empty">Loading...</div></div>
</div>
</details>
</div>
<script>
(function(){{
const $=id=>document.getElementById(id);
function ago(s){{if(s==null)return'\u2014';s=Math.round(s);if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m'}}
function ts(t){{if(!t)return'';try{{return new Date(t).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit',second:'2-digit'}})}}catch(e){{return t}}}}
function esc(s){{const d=document.createElement('div');d.textContent=s;return d.innerHTML}}

async function refresh(){{
try{{
const r=await fetch('/api/health');
const d=await r.json();
const w=d.watchdog||{{}};const srcs=d.sources||[];const feed=d.feed||[];
const topics=d.topics||[];const errs=d.errors||[];const db=d.db;const mq=d.mqtt||{{}};
const sys=d.system||{{}};const vr=d.voice_rooms||{{}};const vc=d.voice_commands||[];

// vitals bar
let v=`MQTT: <b class="${{mq.connected?'g':'r'}}">${{mq.connected?'connected':'disconnected'}}</b>`;
if(sys.cpu_cores){{
const lc=sys.load_1m>sys.cpu_cores*0.8?'r':sys.load_1m>sys.cpu_cores*0.5?'y':'g';
const mc=sys.mem_pct>85?'r':sys.mem_pct>70?'y':'g';
v+=` \\u00b7 CPU: <b class="${{lc}}">${{sys.load_1m}}</b>/${{sys.cpu_cores}}`;
v+=` \\u00b7 RAM: <b class="${{mc}}">${{sys.mem_pct}}%</b>`;
v+=` \\u00b7 Disk: <b>${{sys.disk_pct}}%</b>`;
}}
v+=` \\u00b7 Sources: <b>${{srcs.length}}</b> \\u00b7 ${{ts(d.ts)}}`;
$('vitals').innerHTML=v;

// errors (show section only if errors exist)
if(errs.length){{
$('err-section').style.display='';
      var ec=$('err-clear');if(ec)ec.style.display='inline';
$('err-badge').textContent=errs.length;
let eh='';
for(const e of errs)eh+=`<div class="err-row"><span class="ets">${{ts(e.ts)}}</span> <span class="esvc">${{esc(e.service)}}</span> <span style="color:var(--dim)">${{esc(e.context)}}</span><div class="emsg">${{esc(e.error_type)}}: ${{esc((e.error||'').substring(0,200))}}</div>`+(e.traceback?`<div class="etb">${{esc(e.traceback)}}</div>`:'')+`</div>`;
$('errors').innerHTML=eh;
}}else{{$('err-section').style.display='none'}}

// services strip
const wk=Object.keys(w).sort();
$('svc-ct').textContent=wk.length;
let sh='';
for(const k of wk){{
const s=w[k];const st=s.status||'unknown';
const dc=st==='ok'?'ok':st==='error'?'err':st==='down'?'dn':'u';
sh+=`<div class="s"><span class="dot ${{dc}}"></span><b>${{esc(k.replace(/-agent$/,''))}}</b></div>`;
}}
$('svcs').innerHTML=sh||'<span style="color:var(--dim)">Waiting for watchdog...</span>';

// voice rooms
const vrk=Object.keys(vr).sort();
if(vrk.length){{
let vh='';
for(const k of vrk){{
const rm=vr[k];const act=rm.active;const st=rm.state||'?';
const spk=rm.sonos_playing;const thr=rm.porcupine_thread;const qs=rm.queue_size||0;
const pps=rm.pps||0;const gap=rm.max_gap_s||0;const qd=rm.queue_drops||0;
const sok=rm.session_ok||0;const sfail=rm.session_fail||0;
const srate=sok+sfail>0?Math.round(100*sok/(sok+sfail)):0;
const lstt=rm.last_stt||'';
const dc=!thr&&thr!==null?'err':st==='busy'?'wrn':act?'ok':'dn';
const ppsc=pps>30?'g':pps>10?'y':'r';
const tm=rm.last_timing||{{}};
const cmd=rm.last_command||'';const resp=rm.last_response||'';
vh+=`<div class="vc"><div class="vn"><span class="dot ${{dc}}"></span>${{esc(rm.room_name||k)}}</div>`
+`<div class="vs">${{st.toUpperCase()}}${{spk?' <b style="color:var(--cyan)">PLAYING</b>':''}}`
+`${{thr===false?' <b style="color:var(--red)">WW DEAD</b>':''}}<br>`
+`<b class="${{ppsc}}">${{pps}} pps</b>`
+`${{act?'':' <span style="color:var(--dim)">no audio</span>'}}`
+`${{gap>1?' gap:<b class="r">'+gap.toFixed(1)+'s</b>':''}}`
+`${{qd?' drops:<b class="y">'+qd+'</b>':''}}`
+`<br>Wakes: <b>${{rm.wakes||0}}</b> STT: <b>${{rm.stt_reqs||0}}</b>`
+` OK: <b class="g">${{sok}}</b> Fail: <b class="${{sfail?'r':''}}">${{sfail}}</b>`
+`${{lstt?' <span style="color:var(--dim)">'+esc(lstt.substring(0,40))+'</span>':''}}`
+`${{tm.total_ms?'<br><span style="font-size:10px;color:var(--dim)">prompt:'+tm.prompt_ms+'ms cap:'+tm.capture_ms+'ms proc:'+tm.audio_process_ms+'ms stt:'+tm.stt_ms+'ms total:<b>'+tm.total_ms+'ms</b></span>':''}}`
+`${{cmd?'<br><span style="font-size:11px;color:var(--cyan)">&#x1f399; '+esc(cmd.substring(0,80))+'</span>':''}}`
+`${{resp?'<br><span style="font-size:11px;color:var(--green)">&#x1f50a; '+esc(resp.substring(0,80))+'</span>':''}}`
+`</div></div>`;
}}
$('voice').innerHTML=vh;
}}else{{$('voice').innerHTML='<div class="empty">No voice data yet</div>'}}

// voice transcript
const vresp=d.voice_responses||[];
if(vc.length||vresp.length){{
let items=[];
for(const c of vc)items.push({{ts:c.ts,room:c.room_name||c.room_id,text:c.text,dir:'in'}});
for(const r of vresp)items.push({{ts:r.ts,room:r.room_name||r.room_id,text:r.text,dir:'out'}});
items.sort((a,b)=>(b.ts||'').localeCompare(a.ts||''));
let ch='';
for(const m of items.slice(0,20)){{
const icon=m.dir==='in'?'&#x1f399;':'&#x1f50a;';
const col=m.dir==='in'?'var(--cyan)':'var(--green)';
ch+=`<div style="padding:6px 12px;border-bottom:1px solid var(--border);font-size:12px;display:flex;gap:8px;align-items:baseline">`
+`<span style="color:var(--dim);font-family:var(--mono);font-size:10px;flex-shrink:0">${{ts(m.ts)}}</span>`
+`<span style="color:${{col}};flex-shrink:0">${{icon}}</span>`
+`<b style="color:var(--text);flex-shrink:0;min-width:60px">${{esc(m.room)}}</b>`
+`<span style="color:${{col}}">${{esc(m.text)}}</span></div>`;
}}
$('cmds').innerHTML=ch;
}}

// feed
if(feed.length){{
let fh='<table><tr><th>Time</th><th>Source</th><th>Type</th></tr>';
for(const f of feed.slice(0,15))fh+=`<tr><td>${{ts(f.ts)}}</td><td><b>${{esc(f.source)}}</b></td><td>${{esc((f.type||'').substring(0,30))}}</td></tr>`;
$('feed').innerHTML=fh+'</table>';
}}

// db
if(db&&db.rows){{
$('db-ct').textContent=`${{db.events_last_60s||0}}/min`;
let dh='<table><tr><th>Age</th><th>Source</th><th>Type</th></tr>';
for(const r of db.rows)dh+=`<tr><td>${{ago(r.age_s)}}</td><td><b>${{esc(r.source)}}</b></td><td>${{esc((r.type||'').substring(0,28))}}</td></tr>`;
$('db').innerHTML=dh+'</table>';
}}

// mqtt details (collapsed)
if(srcs.length){{
let st='<table><tr><th>Source</th><th>Age</th><th>Rate</th><th>Total</th></tr>';
for(const s of srcs)st+=`<tr><td><b>${{esc(s.source)}}</b></td><td>${{ago(s.age_s)}}</td><td>${{s.rate}}/s</td><td>${{s.total}}</td></tr>`;
$('src-tbl').innerHTML=st+'</table>';
}}
if(topics.length){{
let tt='<table><tr><th>Topic</th><th>Count</th><th>Rate</th></tr>';
for(const t of topics)tt+=`<tr><td><b>${{esc(t.topic)}}</b></td><td>${{t.count}}</td><td>${{t.rate}}/s</td></tr>`;
$('top-tbl').innerHTML=tt+'</table>';
}}

$('pulse').style.background='var(--green)';
}}catch(e){{$('pulse').style.background='var(--red)';$('vitals').innerHTML='Connection error'}}
}}
refresh();setInterval(refresh,5000);
}})();
</script>
</body>
</html>"""


def _audio_debug_html(*, title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#0a0e1a"/>
<title>{title} — Audio Debug</title>
<style>
{_CSS_VARS}
.w{{max-width:900px;margin:0 auto;padding:12px;padding-top:calc(12px + env(safe-area-inset-top));padding-bottom:calc(20px + env(safe-area-inset-bottom))}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}}
h1{{font-size:17px;font-weight:700}}
.nav a{{color:var(--blue);font-size:12px;text-decoration:none}}
.room{{margin-bottom:20px}}
.rh{{font-size:14px;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px}}
.rh .ct{{font-size:10px;font-weight:700;font-family:var(--mono);padding:1px 6px;border-radius:7px;background:rgba(52,211,153,.12);color:var(--green)}}
.pan{{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}}
.af{{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.03);font-size:11px;font-family:var(--mono)}}
.af:last-child{{border-bottom:none}}
.af .fn{{color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.af .dur{{color:var(--dim);width:50px;text-align:right}}
.af .typ{{font-size:9px;font-weight:700;padding:1px 5px;border-radius:4px;flex-shrink:0}}
.typ.raw{{background:rgba(248,113,113,.15);color:#f87171}}
.typ.proc{{background:rgba(52,211,153,.12);color:var(--green)}}
.af audio{{height:28px;flex-shrink:0}}
.empty{{color:var(--dim);font-size:12px;padding:14px;text-align:center}}
.refresh{{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:6px 14px;border-radius:8px;font-size:12px;cursor:pointer}}
</style>
</head>
<body>
<div class="w">
<header>
<h1>Audio Debug</h1>
<div class="nav"><a href="/status">Status</a> &middot; <a href="/live-audio">Live</a> &middot; <a href="/chat">Chat</a> &middot; <a href="/">Controls</a> &middot; <button class="refresh" onclick="load()">Refresh</button></div>
</header>
<div id="content"><div class="empty">Loading...</div></div>
</div>
<script>
(function(){{
function esc(s){{const d=document.createElement('div');d.textContent=s;return d.innerHTML}}

window.load=async function(){{
try{{
const r=await fetch('/api/debug-audio');
const d=await r.json();
const rooms=d.rooms||{{}};
const rk=Object.keys(rooms).sort();
if(!rk.length){{document.getElementById('content').innerHTML='<div class="empty">No debug audio files yet. Trigger a voice session first.</div>';return}}
let h='';
for(const room of rk){{
const files=rooms[room];
h+=`<div class="room"><div class="rh">${{esc(room)}} <span class="ct">${{files.length}} files</span></div><div class="pan">`;
for(const f of files){{
const isRaw=f.name.includes('_raw');
const typCls=isRaw?'raw':'proc';
const typLbl=isRaw?'RAW':'PROC';
h+=`<div class="af">`
+`<span class="typ ${{typCls}}">${{typLbl}}</span>`
+`<span class="fn">${{esc(f.name)}}</span>`
+`<span class="dur">${{f.duration_s}}s</span>`
+`<audio controls preload="none" src="${{f.url}}"></audio>`
+`</div>`;
}}
h+=`</div></div>`;
}}
document.getElementById('content').innerHTML=h;
}}catch(e){{document.getElementById('content').innerHTML='<div class="empty">Error loading audio files</div>'}}
}};
load();
}})();
</script>
</body>
</html>"""


def _live_audio_html(*, title: str, ws_port: int) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#0a0e1a"/>
<title>{title} — Live Audio</title>
<style>
{_CSS_VARS}
.w{{max-width:900px;margin:0 auto;padding:12px;padding-top:calc(12px + env(safe-area-inset-top));padding-bottom:calc(20px + env(safe-area-inset-bottom))}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}}
h1{{font-size:17px;font-weight:700}}
.nav a{{color:var(--blue);font-size:12px;text-decoration:none}}
.rgrid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}
@media(min-width:600px){{.rgrid{{grid-template-columns:repeat(3,1fr)}}}}
@media(min-width:800px){{.rgrid{{grid-template-columns:repeat(4,1fr)}}}}
.rc{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px;text-align:center}}
.rc .rn{{font-size:14px;font-weight:600;margin-bottom:8px}}
.rc .vu{{height:6px;background:rgba(255,255,255,.08);border-radius:3px;margin:8px 0;overflow:hidden}}
.rc .vu-bar{{height:100%;width:0%;border-radius:3px;background:var(--green);transition:width 80ms linear}}
.rc .lvl{{font-family:var(--mono);font-size:11px;color:var(--dim);margin-bottom:6px}}
.btn-play{{background:var(--surface2);border:1px solid var(--border2);color:var(--text);padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:all 100ms}}
.btn-play:hover{{border-color:var(--green);color:var(--green)}}
.btn-play.active{{background:rgba(248,113,113,.15);border-color:rgba(248,113,113,.4);color:#f87171}}
.empty{{color:var(--dim);font-size:12px;padding:14px;text-align:center}}
.hint{{color:var(--dim);font-size:11px;margin-top:14px;text-align:center}}
</style>
</head>
<body>
<div class="w">
<header>
<h1>Live Audio Monitor</h1>
<div class="nav"><a href="/status">Status</a> &middot; <a href="/audio-debug">Debug</a> &middot; <a href="/">Controls</a></div>
</header>
<div class="rgrid" id="rooms"></div>
<div class="hint">Tap a room to listen to its live mic audio. Requires VOICE_LIVE_AUDIO_PORT in .env.</div>
</div>
<script>
(function(){{
const WS_PORT={ws_port};
const SAMPLE_RATE=16000;
const esc=s=>{{const d=document.createElement('div');d.textContent=s;return d.innerHTML}};
const state={{}};

async function loadRooms(){{
  try{{
    const r=await fetch('/api/health');
    const d=await r.json();
    const vr=d.voice_rooms||{{}};
    const el=document.getElementById('rooms');
    const rk=Object.keys(vr).sort();
    if(!rk.length){{el.innerHTML='<div class="empty">No voice rooms found</div>';return}}
    let h='';
    for(const k of rk){{
      const rm=vr[k];
      const name=rm.room_name||k;
      h+=`<div class="rc" id="rc-${{k}}">`;
      h+=`<div class="rn">${{esc(name)}}</div>`;
      h+=`<div class="vu"><div class="vu-bar" id="vu-${{k}}"></div></div>`;
      h+=`<div class="lvl" id="lvl-${{k}}">—</div>`;
      h+=`<button class="btn-play" id="btn-${{k}}" onclick="toggle('${{k}}')">`
        +`&#x1f50a; Listen</button>`;
      h+=`</div>`;
    }}
    el.innerHTML=h;
  }}catch(e){{}}
}}

window.toggle=function(roomId){{
  if(state[roomId]){{stopRoom(roomId);return}}
  if(!WS_PORT){{alert('VOICE_LIVE_AUDIO_PORT not configured');return}}
  startRoom(roomId);
}};

function startRoom(roomId){{
  const wsHost=location.hostname;
  const ws=new WebSocket(`ws://${{wsHost}}:${{WS_PORT}}/live/${{roomId}}`);
  ws.binaryType='arraybuffer';
  const ctx=new AudioContext({{sampleRate:SAMPLE_RATE}});
  const bufSize=4096;
  const node=ctx.createScriptProcessor(bufSize,0,1);
  const queue=[];
  let leftover=new Float32Array(0);

  ws.onmessage=(e)=>{{
    const pcm=new Int16Array(e.data);
    const f32=new Float32Array(pcm.length);
    let sum=0;
    for(let i=0;i<pcm.length;i++){{
      f32[i]=pcm[i]/32768.0;
      sum+=f32[i]*f32[i];
    }}
    queue.push(f32);
    const rms=Math.sqrt(sum/pcm.length);
    const pct=Math.min(100,Math.round(rms*400));
    const bar=document.getElementById('vu-'+roomId);
    const lvl=document.getElementById('lvl-'+roomId);
    if(bar)bar.style.width=pct+'%';
    if(bar)bar.style.background=pct>60?'#f87171':pct>25?'#fbbf24':'var(--green)';
    if(lvl)lvl.textContent='RMS: '+rms.toFixed(3);
  }};

  node.onaudioprocess=(e)=>{{
    const out=e.outputBuffer.getChannelData(0);
    let src;
    if(leftover.length>0){{
      let total=leftover.length;
      for(const c of queue)total+=c.length;
      src=new Float32Array(total);
      src.set(leftover);let off=leftover.length;
      while(queue.length){{const c=queue.shift();src.set(c,off);off+=c.length}}
    }}else{{
      let total=0;for(const c of queue)total+=c.length;
      src=new Float32Array(total);let off=0;
      while(queue.length){{const c=queue.shift();src.set(c,off);off+=c.length}}
    }}
    if(src.length>=out.length){{
      out.set(src.subarray(0,out.length));
      leftover=src.subarray(out.length);
    }}else{{
      out.set(src);
      for(let i=src.length;i<out.length;i++)out[i]=0;
      leftover=new Float32Array(0);
    }}
  }};
  node.connect(ctx.destination);

  ws.onclose=()=>stopRoom(roomId);
  ws.onerror=()=>stopRoom(roomId);

  const btn=document.getElementById('btn-'+roomId);
  if(btn){{btn.textContent='\\u23f9 Stop';btn.classList.add('active')}}
  state[roomId]={{ws,ctx,node,queue}};
}}

function stopRoom(roomId){{
  const s=state[roomId];
  if(!s)return;
  try{{s.ws.close()}}catch(e){{}}
  try{{s.node.disconnect()}}catch(e){{}}
  try{{s.ctx.close()}}catch(e){{}}
  delete state[roomId];
  const btn=document.getElementById('btn-'+roomId);
  if(btn){{btn.textContent='\\u1f50a Listen';btn.classList.remove('active')}}
  const bar=document.getElementById('vu-'+roomId);
  if(bar)bar.style.width='0%';
  const lvl=document.getElementById('lvl-'+roomId);
  if(lvl)lvl.textContent='\\u2014';
}}

loadRooms();
}})();
</script>
</body>
</html>"""


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

    def _tone_wav_bytes(*, duration_s: float, frequency_hz: int) -> bytes:
        import io
        import math
        import struct
        import wave

        sample_rate = 44100
        n_samples = int(sample_rate * max(0.05, float(duration_s)))
        amplitude = 0.85
        fade_samples = int(sample_rate * 0.02)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)

            frames = bytearray()
            for i in range(n_samples):
                t = float(i) / float(sample_rate)
                fade = 1.0
                if fade_samples > 0:
                    fade = min(1.0, i / fade_samples, (n_samples - 1 - i) / fade_samples)
                v = float(fade) * amplitude * math.sin(2.0 * math.pi * float(frequency_hz) * t)
                frames += struct.pack("<h", int(v * 32767.0))
            wf.writeframes(frames)
        return buf.getvalue()

    @app.on_event("startup")
    async def _startup() -> None:
        await mqttc.connect()
        reporter = ErrorReporter(mqttc=mqttc, service="ui-gateway", base_topic=settings.mqtt.base_topic)
        reporter.start_heartbeat(interval_seconds=30.0)
        mqttc.subscribe("%s/#" % settings.mqtt.base_topic)
        asyncio.create_task(_mqtt_reader())
        asyncio.create_task(_db_poll_loop())
        log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    async def _mqtt_reader() -> None:
        while True:
            try:
                msg = await mqttc.next_message()
                _topic_events.append((time.time(), msg.topic))
                try:
                    payload = msg.json()
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue

                typ = payload.get("type", "")
                source = payload.get("source", "")
                data = payload.get("data") or {}

                if source:
                    _update_source(source, typ, msg.topic)

                _recent_feed.appendleft({
                    "ts": payload.get("ts", ""),
                    "source": source,
                    "type": typ,
                    "topic": msg.topic,
                })

                if typ == "service.heartbeat" and source == "voice-service":
                    # Extract room status from heartbeat... not available there.
                    pass
                elif typ == "voice.room_status":
                    rid = data.get("room_id", "")
                    if rid:
                        _voice_rooms[rid] = {
                            "room_id": rid,
                            "room_name": data.get("room_name", rid),
                            "active": data.get("active", False),
                            "state": data.get("state", "unknown"),
                            "sonos_playing": data.get("sonos_playing", False),
                            "porcupine_thread": data.get("porcupine_thread", None),
                            "queue_size": data.get("queue_size", 0),
                            "frames": data.get("frames", 0),
                            "wakes": data.get("wakes", 0),
                            "stt_reqs": data.get("stt_reqs", 0),
                            "pps": data.get("pps", 0),
                            "max_gap_s": data.get("max_gap_s", 0),
                            "queue_drops": data.get("queue_drops", 0),
                            "session_ok": data.get("session_ok", 0),
                            "session_fail": data.get("session_fail", 0),
                            "last_stt": data.get("last_stt", ""),
                            "ts": payload.get("ts", ""),
                        }
                elif typ == "voice.session_timing":
                    _voice_rooms.setdefault(data.get("room_id", ""), {}).update({
                        "last_timing": {
                            "prompt_ms": data.get("prompt_ms"),
                            "capture_ms": data.get("capture_ms"),
                            "audio_process_ms": data.get("audio_process_ms"),
                            "stt_ms": data.get("stt_ms"),
                            "total_ms": data.get("total_ms"),
                        },
                        "last_command": data.get("text", ""),
                    })
                elif typ == "voice.command":
                    _chat_history.appendleft({
                        "role": "user",
                        "text": data.get("text", ""),
                        "room": data.get("room_name", ""),
                        "ts": payload.get("ts", ""),
                    })
                    _voice_commands.appendleft({
                        "ts": payload.get("ts", ""),
                        "room_id": data.get("room_id", ""),
                        "room_name": data.get("room_name", ""),
                        "text": data.get("text", ""),
                    })
                elif typ == "voice.response":
                    _voice_responses.appendleft({
                        "ts": payload.get("ts", ""),
                        "room_id": data.get("room_id", ""),
                        "room_name": data.get("room_name", ""),
                        "text": data.get("text", ""),
                    })
                    rid = data.get("room_id", "")
                    if rid:
                        _voice_rooms.setdefault(rid, {}).update({
                            "last_response": (data.get("text") or "")[:80],
                        })
                    _chat_history.appendleft({
                        "role": "assistant",
                        "text": data.get("text", ""),
                        "ts": payload.get("ts", ""),
                    })
                elif typ == "watchdog.health":
                    _latest_health.clear()
                    _latest_health.update(data.get("services", {}))
                elif typ == "service.error":
                    _recent_errors.appendleft({
                        "ts": payload.get("ts", ""),
                        "service": data.get("service", ""),
                        "context": data.get("context", ""),
                        "error_type": data.get("error_type", ""),
                        "error": data.get("error", ""),
                        "traceback": data.get("traceback"),
                    })
            except Exception:
                await asyncio.sleep(1.0)

    async def _db_poll_loop() -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                await asyncio.to_thread(_fetch_db_activity_cached, settings)
            except Exception:
                pass

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

    @app.get("/api/health")
    async def api_health() -> Dict[str, Any]:
        now = time.time()
        sources = []
        for k in sorted(_source_stats.keys()):
            st = _source_stats[k]
            age = round(now - st["last_ts"], 1) if st["last_ts"] else None
            sources.append({
                "source": k, "total": st["total"], "age_s": age,
                "rate": round(_source_rate(st), 2),
                "last_type": st["last_type"], "last_topic": st["last_topic"],
            })
        mqtt_stats = mqttc.stats()
        return {
            "watchdog": dict(_latest_health),
            "sources": sources,
            "feed": list(_recent_feed)[:50],
            "topics": _top_topics(10),
            "errors": list(_recent_errors),
            "db": dict(_db_activity) if _db_activity.get("rows") is not None else None,
            "mqtt": {
                "connected": bool(mqtt_stats.get("connected")),
                "queue_size": mqtt_stats.get("queue_size", 0),
                "received_total": mqtt_stats.get("received_total", 0),
                "dropped_total": mqtt_stats.get("dropped_total", 0),
            },
            "voice_rooms": dict(_voice_rooms),
            "voice_commands": list(_voice_commands),
            "voice_responses": list(_voice_responses),
            "chat_history": list(_chat_history),
            "system": _get_system_stats(),
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/status", response_class=HTMLResponse)
    async def status_page() -> str:
        return _status_html(title=settings.ui.title)

    @app.post("/api/clear-errors")
    async def api_clear_errors() -> Dict[str, str]:
        _recent_errors.clear()
        return {"status": "ok"}

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page() -> str:
        return _chat_html(title=settings.ui.title)

    @app.post("/api/chat")
    async def api_chat_send(request_data: Dict[str, Any]) -> Dict[str, str]:
        text = str(request_data.get("text", "")).strip()
        if not text:
            return {"status": "error", "message": "Empty text"}
        evt = make_event(source="web-chat", typ="voice.command",
            data={"room_id": "web", "room_name": "Web Chat", "text": text})
        mqttc.publish_json("%s/voice/command" % settings.mqtt.base_topic, evt)
        return {"status": "ok"}

    @app.get("/api/debug-audio")
    async def api_debug_audio() -> Dict[str, Any]:
        """List available debug WAV files grouped by room."""
        import os
        debug_dir = getattr(settings, "voice_debug_dir", "/tmp/voice_debug")
        if not os.path.isdir(debug_dir):
            return {"rooms": {}}
        rooms_out: Dict[str, list] = {}
        for room_name in sorted(os.listdir(debug_dir)):
            room_path = os.path.join(debug_dir, room_name)
            if not os.path.isdir(room_path):
                continue
            files = []
            for f in sorted(os.listdir(room_path), reverse=True):
                if not f.endswith(".wav"):
                    continue
                fpath = os.path.join(room_path, f)
                try:
                    sz = os.path.getsize(fpath)
                except Exception:
                    sz = 0
                dur = round(max(0, sz - 44) / (16000 * 2), 1)
                files.append({"name": f, "size": sz, "duration_s": dur,
                              "url": f"/api/debug-audio/{room_name}/{f}"})
            if files:
                rooms_out[room_name] = files[:50]
        return {"rooms": rooms_out}

    @app.get("/api/debug-audio/{room}/{filename}")
    async def api_debug_audio_file(room: str, filename: str):
        import os
        from fastapi.responses import FileResponse
        debug_dir = getattr(settings, "voice_debug_dir", "/tmp/voice_debug")
        safe_room = os.path.basename(room)
        safe_file = os.path.basename(filename)
        fpath = os.path.join(debug_dir, safe_room, safe_file)
        if not os.path.isfile(fpath) or not safe_file.endswith(".wav"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(fpath, media_type="audio/wav", filename=safe_file)

    @app.get("/audio-debug", response_class=HTMLResponse)
    async def audio_debug_page() -> str:
        return _audio_debug_html(title=settings.ui.title)

    @app.get("/live-audio", response_class=HTMLResponse)
    async def live_audio_page() -> str:
        return _live_audio_html(
            title=settings.ui.title,
            ws_port=settings.voice_live_audio_port,
        )

    @app.post("/tone-test")
    async def tone_test() -> RedirectResponse:
        targets = settings.sonos.announce_target_ips
        if not targets:
            return RedirectResponse(
                url="/?toast=" + quote("Missing SONOS_ANNOUNCE_TARGETS"),
                status_code=303,
            )

        async def _run_tone() -> None:
            try:
                data = await asyncio.to_thread(_tone_wav_bytes, duration_s=10.0, frequency_hz=880)
                host = AudioHost()
                player = SonosPlayback(
                    speaker_ips=targets,
                    default_volume=settings.sonos.default_volume,
                    speaker_volume_map=settings.sonos.speaker_volume_map,
                )
                hosted = host.host_bytes(
                    data=data,
                    filename="tone_test.wav",
                    content_type="audio/wav",
                    route_to_ip=targets[0],
                )
                await player.play_url(
                    url=hosted.url,
                    title="Home Agent tone test",
                    concurrency=12,
                    tail_padding_seconds=float(settings.sonos.tail_padding_seconds),
                    expected_duration_seconds=10.0,
                    done_timeout_seconds=20.0,
                )
                log.info("tone_test_done", seconds=10, concurrency=12)
            except Exception:
                log.exception("tone_test_failed")

        asyncio.create_task(_run_tone())
        return RedirectResponse(url="/?toast=" + quote("Test tone started"), status_code=303)

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
