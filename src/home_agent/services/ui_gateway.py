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


def _fetch_db_activity_cached(settings: Any) -> Dict[str, Any]:
    cached = _db_activity.get("_cached_at", 0.0)
    if (time.time() - cached) < 5.0 and _db_activity.get("rows") is not None:
        return _db_activity
    try:
        import psycopg
        conn = psycopg.connect(settings.db.conninfo, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT now(), (SELECT max(ingested_at) FROM events),
                           (SELECT count(*) FROM events WHERE ingested_at > now() - interval '60 seconds')
                """)
                now_utc, last_at, last_60 = cur.fetchone()
                cur.execute("SELECT ingested_at, topic, source, type FROM events WHERE type NOT IN ('service.heartbeat', 'voice.room_status', 'watchdog.health', 'service.error') ORDER BY ingested_at DESC LIMIT 8")
                rows = cur.fetchall()
        finally:
            conn.close()
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
      <div class="nav"><a href="/status">System Status &#x2192;</a></div>
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


def _status_html(*, title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
  <meta name="theme-color" content="#0a0e1a"/>
  <title>{title} &#x2014; Status</title>
  <style>
    {_CSS_VARS}
    .w{{max-width:1100px;margin:0 auto;padding:14px;padding-top:calc(14px + env(safe-area-inset-top));padding-bottom:calc(20px + env(safe-area-inset-bottom))}}
    header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}}
    h1{{font-size:17px;font-weight:700}}
    .nav{{display:flex;gap:10px;align-items:center}}
    .nav a{{color:var(--blue);font-size:12px;text-decoration:none}}
    .pulse{{width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block;animation:p 2s infinite}}
    @keyframes p{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
    .bar{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;margin-bottom:12px;font-size:11px;font-family:var(--mono);color:var(--dim);display:flex;flex-wrap:wrap;gap:6px 18px}}
    .bar span{{color:var(--text)}}
    .bar .g{{color:var(--green)}} .bar .y{{color:var(--yellow)}} .bar .r{{color:var(--red)}}
    .st{{font-size:13px;font-weight:600;margin:14px 0 8px;display:flex;align-items:center;gap:8px}}
    .badge{{font-size:10px;font-weight:700;font-family:var(--mono);padding:1px 6px;border-radius:7px}}
    .bg{{background:rgba(52,211,153,.12);color:var(--green)}} .br{{background:rgba(248,113,113,.15);color:var(--red)}}
    .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:4px}}
    @media(min-width:540px){{.grid{{grid-template-columns:repeat(3,1fr)}}}}
    @media(min-width:800px){{.grid{{grid-template-columns:repeat(4,1fr)}}}}
    .c{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:10px 12px;transition:border-color .2s}}
    .c:hover{{border-color:var(--border2)}}
    .c .n{{font-size:12px;font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:5px;white-space:nowrap;overflow:hidden}}
    .dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
    .dot.ok{{background:var(--green)}}.dot.error{{background:var(--yellow)}}.dot.down{{background:var(--red)}}.dot.u{{background:var(--dim)}}
    .c .s{{font-size:10px;color:var(--dim);font-family:var(--mono);line-height:1.65}}
    .c .s b{{color:var(--text);font-weight:500}}
    .cols{{display:grid;grid-template-columns:1fr;gap:12px;margin-top:4px}}
    @media(min-width:700px){{.cols{{grid-template-columns:1fr 1fr}}}}
    .pan{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}}
    .pan-t{{font-size:12px;font-weight:600;padding:8px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px}}
    .pan-t .ico{{font-size:14px}}
    table{{width:100%;border-collapse:collapse;font-size:11px;font-family:var(--mono)}}
    th{{text-align:left;color:var(--dim);font-weight:500;padding:5px 10px;border-bottom:1px solid var(--border)}}
    td{{padding:4px 10px;border-bottom:1px solid rgba(255,255,255,.03);color:var(--dim)}}
    td b{{color:var(--text);font-weight:500}}
    tr:last-child td{{border-bottom:none}}
    .err-row{{padding:8px 12px;border-bottom:1px solid var(--border);font-size:11px;line-height:1.5}}
    .err-row:last-child{{border-bottom:none}}
    .ets{{color:var(--dim);font-family:var(--mono);font-size:10px}}
    .esvc{{color:var(--yellow);font-weight:600}}
    .ectx{{color:var(--dim)}}
    .emsg{{color:var(--red);font-family:var(--mono);font-size:10px;margin-top:2px;word-break:break-all}}
    .etb{{color:var(--dim);font-family:var(--mono);font-size:9px;margin-top:3px;white-space:pre-wrap;max-height:100px;overflow-y:auto;background:rgba(0,0,0,.3);padding:5px 7px;border-radius:7px}}
    .empty{{color:var(--dim);font-size:12px;padding:16px;text-align:center}}
  </style>
</head>
<body>
<div class="w">
  <header><h1>System Status</h1><div class="nav"><span class="pulse" id="pulse"></span><a href="/">&#x2190; Controls</a></div></header>
  <div class="bar" id="bar">Connecting...</div>
  <div class="bar" id="sys" style="margin-bottom:8px">System loading...</div>
  <div class="st">Services</div>
  <div class="grid" id="grid"></div>
  <div class="cols">
    <div>
      <div class="st">MQTT Sources <span class="badge bg" id="src-n">0</span></div>
      <div class="pan"><div class="pan-t"><span class="ico">&#x1f4e1;</span>Activity by source</div><div id="src-tbl"><div class="empty">Loading...</div></div></div>
      <div class="st" style="margin-top:14px">Top Topics (60s)</div>
      <div class="pan"><div class="pan-t"><span class="ico">&#x1f4ca;</span>Message rates</div><div id="top-tbl"><div class="empty">Loading...</div></div></div>
    </div>
    <div>
      <div class="st">Recent Activity</div>
      <div class="pan" style="max-height:300px;overflow-y:auto"><div class="pan-t"><span class="ico">&#x26a1;</span>Live feed</div><div id="feed"><div class="empty">Loading...</div></div></div>
      <div class="st" style="margin-top:14px">Database <span class="badge bg" id="db-badge">&#x2014;</span></div>
      <div class="pan"><div class="pan-t"><span class="ico">&#x1f5c4;&#xfe0f;</span>Recent ingested events</div><div id="db-tbl"><div class="empty">Loading...</div></div></div>
    </div>
  </div>
  <div class="st">Voice Assistants</div>
  <div class="grid" id="voice-grid"></div>
  <div class="st" style="margin-top:10px">Recent Voice Commands</div>
  <div class="pan" id="voice-cmds" style="max-height:200px;overflow-y:auto"><div class="empty">No commands yet</div></div>
  <div class="st" style="margin-top:14px">Errors <span class="badge bg" id="err-badge">0</span></div>
  <div class="pan" id="errors" style="max-height:350px;overflow-y:auto"><div class="empty">No errors</div></div>
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

    let bh=`MQTT: <span class="${{mq.connected?'g':'r'}}">${{mq.connected?'connected':'disconnected'}}</span> \u00b7 `
      +`recv: <span>${{mq.received_total||0}}</span> \u00b7 dropped: <span>${{mq.dropped_total||0}}</span> \u00b7 `
      +`queue: <span>${{mq.queue_size||0}}</span> \u00b7 `
      +`sources: <span>${{srcs.length}}</span> \u00b7 `
      +`updated: <span>${{ts(d.ts)}}</span>`;
    $('bar').innerHTML=bh;

    const sys=d.system||{{}};
    if(sys.cpu_cores){{
      const loadColor=sys.load_1m>sys.cpu_cores*0.8?'r':sys.load_1m>sys.cpu_cores*0.5?'y':'g';
      const memColor=sys.mem_pct>85?'r':sys.mem_pct>70?'y':'g';
      const diskColor=sys.disk_pct>90?'r':sys.disk_pct>80?'y':'g';
      $('sys').innerHTML=`CPU: <span class="${{loadColor}}">${{sys.load_1m}}</span>/${{sys.cpu_cores}} cores `
        +`\u00b7 RAM: <span class="${{memColor}}">${{sys.mem_used_gb}}G</span>/${{sys.mem_total_gb}}G (${{sys.mem_pct}}%) `
        +`\u00b7 Disk: <span class="${{diskColor}}">${{sys.disk_used_gb}}G</span>/${{sys.disk_total_gb}}G (${{sys.disk_pct}}%)`;
    }}

    const wk=Object.keys(w).sort();
    let gh='';
    for(const k of wk){{
      const s=w[k];const st=s.status||'unknown';
      const src=srcs.find(x=>x.source===k);
      gh+=`<div class="c"><div class="n"><span class="dot ${{st==='ok'?'ok':st==='error'?'error':st==='down'?'down':'u'}}"></span>${{esc(k)}}</div>`
        +`<div class="s">Status: <b>${{st.toUpperCase()}}</b><br>HB: <b>${{ago(s.heartbeat_age_seconds)}}</b>`
        +`<br>Errs: <b>${{s.error_count||0}}</b> \u00b7 PID: <b>${{s.pid||'\u2014'}}</b>`
        +(src?`<br>Rate: <b>${{src.rate}}/s</b> \u00b7 Msgs: <b>${{src.total}}</b>`:'')
        +(s.restart_attempted?'<br><span style="color:var(--yellow)">restarted</span>':'')
        +`</div></div>`;
    }}
    $('grid').innerHTML=gh||'<div class="empty">Waiting for watchdog...</div>';

    $('src-n').textContent=srcs.length;
    if(srcs.length){{
      let st='<table><tr><th>Source</th><th>Age</th><th>Rate</th><th>Total</th><th>Last Type</th></tr>';
      for(const s of srcs)st+=`<tr><td><b>${{esc(s.source)}}</b></td><td>${{ago(s.age_s)}}</td><td>${{s.rate}}/s</td><td>${{s.total}}</td><td>${{esc((s.last_type||'').substring(0,30))}}</td></tr>`;
      $('src-tbl').innerHTML=st+'</table>';
    }}

    if(topics.length){{
      let tt='<table><tr><th>Topic</th><th>Count</th><th>Rate</th></tr>';
      for(const t of topics)tt+=`<tr><td><b>${{esc(t.topic)}}</b></td><td>${{t.count}}</td><td>${{t.rate}}/s</td></tr>`;
      $('top-tbl').innerHTML=tt+'</table>';
    }}else{{$('top-tbl').innerHTML='<div class="empty">No traffic</div>'}}

    if(feed.length){{
      let fh='<table><tr><th>Time</th><th>Source</th><th>Type</th></tr>';
      for(const f of feed.slice(0,25))fh+=`<tr><td>${{ts(f.ts)}}</td><td><b>${{esc(f.source)}}</b></td><td>${{esc((f.type||'').substring(0,35))}}</td></tr>`;
      $('feed').innerHTML=fh+'</table>';
    }}

    if(db&&db.rows){{
      $('db-badge').textContent=`${{db.events_last_60s||0}}/60s`;
      let dh=`<div style="padding:6px 10px;font-size:10px;font-family:var(--mono);color:var(--dim)">last ingest: <b style="color:var(--text)">${{ago(db.last_ingest_age_s)}}</b> \u00b7 events/60s: <b style="color:var(--text)">${{db.events_last_60s||0}}</b></div>`;
      if(db.rows.length){{
        dh+='<table><tr><th>Age</th><th>Source</th><th>Type</th></tr>';
        for(const r of db.rows)dh+=`<tr><td>${{ago(r.age_s)}}</td><td><b>${{esc(r.source)}}</b></td><td>${{esc((r.type||'').substring(0,30))}}</td></tr>`;
        dh+='</table>';
      }}
      $('db-tbl').innerHTML=dh;
    }}else{{$('db-tbl').innerHTML='<div class="empty">DB unavailable</div>'}}

    // voice rooms
    const vr=d.voice_rooms||{{}};
    const vrk=Object.keys(vr).sort();
    if(vrk.length){{
      let vh='';
      for(const k of vrk){{
        const v=vr[k];
        const act=v.active;
        vh+=`<div class="c"><div class="n"><span class="dot ${{act?'ok':'down'}}"></span>${{esc(v.room_name||k)}}</div>`
          +`<div class="s">State: <b>${{esc(v.state||'?')}}</b><br>`
          +`Active: <b>${{act?'yes':'no'}}</b><br>`
          +`Wakes: <b>${{v.wakes||0}}</b> \u00b7 STT: <b>${{v.stt_reqs||0}}</b><br>`
          +`Frames: <b>${{v.frames||0}}</b>`
          +`</div></div>`;
      }}
      $('voice-grid').innerHTML=vh;
    }}else{{$('voice-grid').innerHTML='<div class="empty">No voice data yet</div>'}}

    // voice commands
    const vc=d.voice_commands||[];
    if(vc.length){{
      let vch='<table><tr><th>Time</th><th>Room</th><th>Command</th></tr>';
      for(const c of vc.slice(0,10))vch+=`<tr><td>${{ts(c.ts)}}</td><td><b>${{esc(c.room_name||c.room_id)}}</b></td><td>${{esc(c.text)}}</td></tr>`;
      $('voice-cmds').innerHTML=vch+'</table>';
    }}

    const eb=$('err-badge');
    if(errs.length){{
      let eh='';
      for(const e of errs)eh+=`<div class="err-row"><span class="ets">${{ts(e.ts)}}</span> <span class="esvc">${{esc(e.service)}}</span> <span class="ectx">${{esc(e.context)}}</span><div class="emsg">${{esc(e.error_type)}}: ${{esc((e.error||'').substring(0,200))}}</div>`+(e.traceback?`<div class="etb">${{esc(e.traceback)}}</div>`:'')+`</div>`;
      $('errors').innerHTML=eh;eb.textContent=errs.length;eb.className='badge br';
    }}else{{$('errors').innerHTML='<div class="empty">No errors</div>';eb.textContent='0';eb.className='badge bg'}}

    $('pulse').style.background='var(--green)';
  }}catch(e){{$('pulse').style.background='var(--red)';$('bar').innerHTML='Connection error'}}
}}
refresh();setInterval(refresh,5000);
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
                            "frames": data.get("frames", 0),
                            "wakes": data.get("wakes", 0),
                            "stt_reqs": data.get("stt_reqs", 0),
                            "ts": payload.get("ts", ""),
                        }
                elif typ == "voice.command":
                    _voice_commands.appendleft({
                        "ts": payload.get("ts", ""),
                        "room_id": data.get("room_id", ""),
                        "room_name": data.get("room_name", ""),
                        "text": data.get("text", ""),
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
            "system": _get_system_stats(),
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/status", response_class=HTMLResponse)
    async def status_page() -> str:
        return _status_html(title=settings.ui.title)

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
