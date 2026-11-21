#!/usr/bin/env python3
"""
ferry_display.py — 480×480 transit board preview for Pimoroni Presto.

Run:   python3 ferry_display.py
Opens: http://127.0.0.1:8765

No external deps. Uses Python's built-in http.server + urllib.
GTFS data is cached for 5 minutes so refreshes are fast.
"""

import io
import json
import threading
import time
import webbrowser
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen

# ── config ────────────────────────────────────────────────────────────────────

GTFS_URL       = "https://nycferry.connexionz.net/rtt/public/utility/gtfs.aspx"
ALERTS_URL     = "https://us-central1-nyc-ferry.cloudfunctions.net/nycf_service_alerts?lang=en"
PORT           = 8765
CACHE_TTL      = 300          # re-fetch GTFS every 5 minutes
NY_OFFSET      = timedelta(hours=-4)   # EDT; change to -5 in winter

ORIGIN_KW = ["greenpoint"]
DEST_KW   = ["east 34"]
ROUTE_KW  = [
    "east river", "greenpoint", "wall st", "pier 11",
    "north williamsburg", "south williamsburg", "hunters point",
    "dumbo", "long island city", "roosevelt island", "astoria",
    "stuyvesant", "corlears", "brooklyn navy yard",
]


# ── GTFS helpers ──────────────────────────────────────────────────────────────

def fetch_bytes(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "ferry-presto-poc/1.0"})
    with urlopen(req, timeout=25) as r:
        return r.read()


def parse_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
    with zf.open(name) as f:
        lines = f.read().decode("utf-8-sig").splitlines()
    if not lines:
        return []
    headers = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = _split_csv(line)
        parts += [""] * max(0, len(headers) - len(parts))
        rows.append({h: parts[i].strip().strip('"') for i, h in enumerate(headers)})
    return rows


def _split_csv(line: str) -> list[str]:
    fields, cur, in_q = [], [], False
    for ch in line:
        if ch == '"':    in_q = not in_q
        elif ch == "," and not in_q:
            fields.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def active_sids(calendar, cal_dates, today):
    dow      = today.strftime("%A").lower()
    date_str = today.strftime("%Y%m%d")
    active   = {r["service_id"] for r in calendar
                if r.get(dow) == "1"
                and r["start_date"] <= date_str <= r["end_date"]}
    for r in cal_dates:
        if r["date"] != date_str:
            continue
        if r["exception_type"] == "1":   active.add(r["service_id"])
        elif r["exception_type"] == "2": active.discard(r["service_id"])
    return active


# ── main compute function ─────────────────────────────────────────────────────

def compute_departures() -> dict:
    now_utc   = datetime.now(timezone.utc)
    now_local = now_utc + NY_OFFSET
    today     = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    now_ms    = int(now_utc.timestamp() * 1000)

    # GTFS
    zf = zipfile.ZipFile(io.BytesIO(fetch_bytes(GTFS_URL)))
    stops      = parse_csv(zf, "stops.txt")
    stop_times = parse_csv(zf, "stop_times.txt")
    trips      = parse_csv(zf, "trips.txt")
    calendar   = parse_csv(zf, "calendar.txt")
    cal_dates  = parse_csv(zf, "calendar_dates.txt")

    origin_ids = {s["stop_id"] for s in stops if all(k in s["stop_name"].lower() for k in ORIGIN_KW)}
    dest_ids   = {s["stop_id"] for s in stops if all(k in s["stop_name"].lower() for k in DEST_KW)}

    # index stop_times by trip
    by_trip: dict[str, list[dict]] = {}
    for st in stop_times:
        by_trip.setdefault(st["trip_id"], []).append(st)
    for v in by_trip.values():
        v.sort(key=lambda x: int(x.get("stop_sequence", 0)))

    # walk forward day by day until we have 30 upcoming departures
    future = []
    day = today
    while len(future) < 30 and (day - today).days < 7:
        sids       = active_sids(calendar, cal_dates, day)
        active_tid = {t["trip_id"] for t in trips if t["service_id"] in sids}
        day_deps   = []
        for tid, stops_seq in by_trip.items():
            if tid not in active_tid:
                continue
            ids = [s["stop_id"] for s in stops_seq]
            oi  = next((i for i, sid in enumerate(ids) if sid in origin_ids), None)
            di  = next((i for i, sid in enumerate(ids) if sid in dest_ids),   None)
            if oi is None or di is None or oi >= di:
                continue
            h, m, s = (int(x) for x in stops_seq[oi]["departure_time"].split(":"))
            dep_dt = day + timedelta(hours=h, minutes=m, seconds=s)
            if dep_dt > now_local:
                day_deps.append(dep_dt)
        day_deps.sort()
        future.extend(day_deps)
        day += timedelta(days=1)
    future = future[:30]

    def fmt12(dt):
        h    = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{h}:{dt.minute:02d} {ampm}"

    def mins_away(dt):
        return max(0, int((dt - now_local).total_seconds() // 60))

    # Alerts
    try:
        raw    = fetch_bytes(ALERTS_URL)
        alerts = [a for a in json.loads(raw) if int(a.get("expirationDate", 0)) > now_ms]
        route_alerts = [
            a for a in alerts
            if any(kw in (a.get("notificationTitle","") + a.get("notificationBody","")).lower()
                   for kw in ROUTE_KW)
        ]
    except Exception:
        route_alerts = []

    has_alerts = bool(route_alerts)

    return {
        "origin":      "Greenpoint",
        "destination": "East 34th Street",
        "departures": [
            {
                "scheduled_time":  fmt12(d),
                "predicted_time":  None,
                "delay_minutes":   None,
                "minutes_away":    mins_away(d),
                "status":          "live" if has_alerts else "scheduled",
                "_iso":            (today + (d - today)).strftime("%Y-%m-%dT%H:%M:%S"),
            }
            for d in future
        ],
        "service_alerts": [
            {"title": a["notificationTitle"], "body": a["notificationBody"][:180]}
            for a in route_alerts
        ],
        "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── HTTP server ───────────────────────────────────────────────────────────────

_cache_data = None
_cache_time: float = 0.0
_cache_lock = threading.Lock()


def get_data() -> dict:
    global _cache_data, _cache_time
    with _cache_lock:
        if _cache_data is None or time.time() - _cache_time > CACHE_TTL:
            print("  [cache miss] fetching GTFS + alerts …")
            _cache_data = compute_departures()
            _cache_time = time.time()
            print("  [cache] updated")
        return _cache_data


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode()
            self._respond(200, "text/html; charset=utf-8", body)
        elif self.path == "/api/departures":
            try:
                data = get_data()
                body = json.dumps(data).encode()
                self._respond(200, "application/json", body)
            except Exception as exc:
                err  = json.dumps({"error": str(exc)}).encode()
                self._respond(500, "application/json", err)
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"  [{self.client_address[0]}] {fmt % args}")


# ── HTML / UI ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=480">
<title>NYC Ferry – Presto Preview</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: #111827;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  }

  /* ── device chrome ─────────────────────────────── */
  .device {
    position: relative;
    background: #0a0a0a;
    border-radius: 18px;
    padding: 16px;
    box-shadow:
      0 0 0 1px #222,
      0 0 60px rgba(0,131,156,.25),
      0 24px 60px rgba(0,0,0,.6);
  }

  /* ── 480×480 screen ────────────────────────────── */
  .screen {
    width: 480px;
    height: 480px;
    background: #0b0f19;
    border-radius: 6px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    position: relative;
  }

  /* ── header ────────────────────────────────────── */
  .hdr {
    background: linear-gradient(135deg, #00839C 0%, #006478 100%);
    padding: 16px 22px 14px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .hdr-icon {
    font-size: 28px;
    line-height: 1;
    flex-shrink: 0;
  }
  .hdr-text { flex: 1; }
  .hdr-label {
    font-size: 10px;
    color: rgba(255,255,255,.6);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 2px;
  }
  .hdr-title {
    font-size: 19px;
    font-weight: 700;
    color: #fff;
    letter-spacing: -.3px;
    line-height: 1.2;
  }
  .hdr-sub {
    font-size: 12px;
    color: rgba(255,255,255,.75);
    margin-top: 1px;
  }
  .hdr-day {
    text-align: right;
    flex-shrink: 0;
  }
  .hdr-day-name {
    font-size: 10px;
    color: rgba(255,255,255,.6);
    letter-spacing: 1.5px;
    text-transform: uppercase;
  }
  .hdr-day-time {
    font-size: 16px;
    font-weight: 700;
    color: #fff;
    font-variant-numeric: tabular-nums;
  }

  /* ── departures ────────────────────────────────── */
  .deps {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
  }

  .deps-scroll {
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    scrollbar-width: none;
    scroll-snap-type: y mandatory;
    height: 100%;
  }
  .deps-scroll::-webkit-scrollbar { display: none; }

  .dep {
    display: flex;
    align-items: center;
    padding: 0 22px;
    min-height: calc(100% / 3);
    border-bottom: 1px solid #141929;
    gap: 14px;
    position: relative;
    transition: background .2s;
    flex-shrink: 0;
    scroll-snap-align: start;
  }
  .dep:last-child { border-bottom: none; }
  .dep:active { background: #141929; }

  .dep-num {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    background: #141929;
    color: #4B5563;
    font-size: 12px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .dep-num.imminent { background: #064e3b; color: #10b981; }

  .dep-main { flex: 1; display: flex; flex-direction: column; gap: 5px; }
  .dep-time {
    font-size: 38px;
    font-weight: 800;
    color: #E2E8F0;
    letter-spacing: -1px;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .dep-time.imminent { color: #34d399; }

  .dep-meta { display: flex; align-items: center; gap: 8px; }
  .dep-label {
    font-size: 11px;
    color: #4B5563;
    letter-spacing: .5px;
    text-transform: uppercase;
  }
  .dep-type-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: #4B5563;
  }
  .dep-type-dot.live { background: #34d399; animation: blink 2s ease-in-out infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }

  /* countdown badge */
  .dep-badge {
    flex-shrink: 0;
    min-width: 84px;
    padding: 10px 16px;
    border-radius: 24px;
    text-align: center;
    font-size: 16px;
    font-weight: 800;
    letter-spacing: -.3px;
    line-height: 1;
    font-variant-numeric: tabular-nums;
    transition: background .3s, color .3s;
  }
  .badge-now  { background: #064e3b; color: #34d399; }
  .badge-soon { background: #00839C; color: #fff; }
  .badge-later{ background: #141929; color: #4B91A1; }

  /* ── alert strip ───────────────────────────────── */
  .alert {
    background: #1c1008;
    border-top: 1px solid #78350f;
    padding: 9px 18px;
    display: none;
    align-items: flex-start;
    gap: 9px;
    flex-shrink: 0;
  }
  .alert.show { display: flex; }
  .alert-icon { font-size: 13px; margin-top: 1px; flex-shrink: 0; }
  .alert-body { flex: 1; }
  .alert-title {
    font-size: 11px;
    font-weight: 700;
    color: #fbbf24;
    line-height: 1.3;
    display: -webkit-box;
    -webkit-line-clamp: 1;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .alert-text {
    font-size: 10px;
    color: #92400e;
    margin-top: 2px;
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  /* ── footer ────────────────────────────────────── */
  .footer {
    background: #080c14;
    padding: 7px 22px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
    gap: 8px;
  }
  .footer-src {
    font-size: 9px;
    color: #1f2937;
    letter-spacing: 1.5px;
    text-transform: uppercase;
  }
  .footer-refresh {
    font-size: 9px;
    color: #1f2937;
    letter-spacing: .5px;
  }

  /* ── scroll hint ───────────────────────────────── */
  .scroll-hint {
    position: absolute;
    bottom: 6px;
    right: 14px;
    font-size: 9px;
    color: #1f2937;
    letter-spacing: 1px;
    text-transform: uppercase;
    pointer-events: none;
    transition: opacity .3s;
  }
  .scroll-hint.hidden { opacity: 0; }

  /* ── loading skeleton ──────────────────────────── */
  .skeleton { flex: 1; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 14px; }
  .spin { width: 36px; height: 36px; border: 3px solid #141929; border-top-color: #00839C; border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin-text { font-size: 12px; color: #374151; letter-spacing: 1px; }

  /* ── label below device ────────────────────────── */
  .device-label {
    font-size: 11px;
    color: #374151;
    letter-spacing: 2px;
    text-transform: uppercase;
    text-align: center;
  }
</style>
</head>
<body>

<div class="device">
  <div class="screen">

    <div class="hdr">
      <div class="hdr-icon">⛴</div>
      <div class="hdr-text">
        <div class="hdr-label">East River Route</div>
        <div class="hdr-title">Greenpoint</div>
        <div class="hdr-sub">→ East 34th Street</div>
      </div>
      <div class="hdr-day">
        <div class="hdr-day-name" id="day-name">—</div>
        <div class="hdr-day-time" id="clock">—</div>
      </div>
    </div>

    <div class="deps" id="deps">
      <div class="deps-scroll" id="deps-scroll">
        <div class="skeleton">
          <div class="spin"></div>
          <div class="spin-text">Fetching schedule…</div>
        </div>
      </div>
      <div class="scroll-hint hidden" id="scroll-hint">▼ more</div>
    </div>

    <div class="alert" id="alert">
      <span class="alert-icon">⚠</span>
      <div class="alert-body">
        <div class="alert-title" id="alert-title"></div>
        <div class="alert-text" id="alert-body"></div>
      </div>
    </div>

    <div class="footer">
      <span class="footer-src" id="footer-src">Connecting…</span>
      <span class="footer-refresh" id="footer-refresh"></span>
    </div>

  </div>
</div>

<div class="device-label">Pimoroni Presto &nbsp;•&nbsp; 480 × 480 px &nbsp;•&nbsp; IPS LCD</div>

<script>
let lastData   = null;
let lastFetch  = 0;

const DAYS      = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const SHORT_DAYS = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
const todayStr   = new Date().toDateString();

// ── clock ─────────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  const h   = now.getHours() % 12 || 12;
  const m   = String(now.getMinutes()).padStart(2,'0');
  const ap  = now.getHours() < 12 ? 'AM' : 'PM';
  document.getElementById('clock').textContent     = `${h}:${m} ${ap}`;
  document.getElementById('day-name').textContent  = SHORT_DAYS[now.getDay()].toUpperCase();
}
setInterval(updateClock, 1000);
updateClock();

// ── badge helpers ─────────────────────────────────────────────────────────────
function isToday(isoStr) {
  return new Date(isoStr).toDateString() === todayStr;
}
function badgeClass(mins, isoStr) {
  if (!isToday(isoStr))  return 'badge-later';
  if (mins <= 1)         return 'badge-now';
  if (mins <= 20)        return 'badge-soon';
  return 'badge-later';
}
function badgeText(mins, isoStr) {
  if (!isToday(isoStr)) return SHORT_DAYS[new Date(isoStr).getDay()];
  if (mins <= 0)        return 'NOW';
  if (mins === 1)       return '1 MIN';
  return `${mins} MIN`;
}

// ── render ────────────────────────────────────────────────────────────────────
function render(data) {
  lastData = data;

  const scrollEl = document.getElementById('deps-scroll');
  const hintEl   = document.getElementById('scroll-hint');
  if (!data.departures || data.departures.length === 0) {
    scrollEl.innerHTML = `<div class="skeleton"><div class="spin-text">No departures found</div></div>`;
    hintEl.classList.add('hidden');
    return;
  }

  scrollEl.innerHTML = data.departures.map((dep, i) => {
    const mins      = dep.minutes_away;
    const imminent  = mins <= 3;
    const isLive    = dep.status === 'live';
    return `
      <div class="dep">
        <div class="dep-num${imminent ? ' imminent' : ''}">${i+1}</div>
        <div class="dep-main">
          <div class="dep-time${imminent ? ' imminent' : ''}" data-idx="${i}">${dep.scheduled_time}</div>
          <div class="dep-meta">
            <div class="dep-label">Scheduled</div>
            <div class="dep-type-dot${isLive ? ' live' : ''}"></div>
            <div class="dep-label">${isLive ? 'Alert active' : 'On time'}</div>
          </div>
        </div>
        <div class="dep-badge ${badgeClass(mins, dep._iso)}" data-badge="${i}" data-iso="${dep._iso}">${badgeText(mins, dep._iso)}</div>
      </div>`;
  }).join('');

  // scroll hint — show if more than 3 departures
  if (data.departures.length > 3) {
    hintEl.classList.remove('hidden');
    scrollEl.addEventListener('scroll', () => {
      const atBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 10;
      hintEl.classList.toggle('hidden', atBottom);
    }, { passive: true });
  } else {
    hintEl.classList.add('hidden');
  }

  // alerts
  const alertEl = document.getElementById('alert');
  if (data.service_alerts && data.service_alerts.length > 0) {
    document.getElementById('alert-title').textContent = data.service_alerts[0].title;
    document.getElementById('alert-body').textContent  = data.service_alerts[0].body;
    alertEl.classList.add('show');
  } else {
    alertEl.classList.remove('show');
  }

  // footer
  const t = new Date(data.updated_at);
  document.getElementById('footer-src').textContent     = 'Static GTFS + Live Alerts';
  document.getElementById('footer-refresh').textContent =
    `Updated ${t.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}`;
}

// ── live countdown tick (updates badges without re-fetching) ──────────────────
function tick() {
  if (!lastData) return;
  const now = Date.now();
  lastData.departures.forEach((dep, i) => {
    const depMs = new Date(lastData.updated_at).getTime()
                + dep.minutes_away * 60000
                + (Date.now() - lastFetch);
    const mins  = Math.max(0, Math.round((depMs - now) / 60000));
    const scrollEl = document.getElementById('deps-scroll');
    if (!scrollEl) return;
    const badge = scrollEl.querySelector(`[data-badge="${i}"]`);
    if (!badge) return;
    const iso = badge.dataset.iso;
    badge.className = `dep-badge ${badgeClass(mins, iso)}`;
    badge.textContent = badgeText(mins, iso);
    const timeEl = scrollEl.querySelector(`[data-idx="${i}"]`);
    if (timeEl) timeEl.classList.toggle('imminent', mins <= 3);
    const numEl = badge.closest('.dep')?.querySelector('.dep-num');
    if (numEl) numEl.classList.toggle('imminent', mins <= 3);
  });
}
setInterval(tick, 15000);

// ── fetch ─────────────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const res  = await fetch('/api/departures');
    const data = await res.json();
    lastFetch  = Date.now();
    render(data);
  } catch (e) {
    document.getElementById('footer-src').textContent = 'Error – retrying…';
    console.error(e);
  }
  setTimeout(refresh, 60 * 1000);
}

refresh();
</script>
</body>
</html>
"""


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"Starting ferry display server on {url}")
    print("  Ctrl-C to stop\n")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
