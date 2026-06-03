#!/usr/bin/env python3
"""
Ferry board JSON server — runs on your Mac.
The Presto fetches GET /departures over WiFi.

GTFS zip is cached in memory and refreshed once per hour.
Alerts are fetched fresh on every request (they're tiny JSON).
"""

import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen

GTFS_URL   = "https://nycferry.connexionz.net/rtt/public/utility/gtfs.aspx"
ALERTS_URL = "https://us-central1-nyc-ferry.cloudfunctions.net/nycf_service_alerts?lang=en"

ORIGIN_KEYWORDS = ["greenpoint"]
DEST_KEYWORDS   = ["east 34"]

ROUTE_KEYWORDS = [
    "east river", "greenpoint", "wall st", "pier 11",
    "north williamsburg", "south williamsburg", "hunters point",
    "dumbo", "long island city", "roosevelt island", "astoria",
    "stuyvesant", "corlears", "brooklyn navy yard", " er ", "(er)",
]

# ── GTFS cache ────────────────────────────────────────────────────────────────

_gtfs_zip = None
_gtfs_fetched_at = None
GTFS_TTL = timedelta(hours=1)


def get_gtfs() -> zipfile.ZipFile:
    global _gtfs_zip, _gtfs_fetched_at
    now = datetime.now(timezone.utc)
    if _gtfs_zip is None or (now - _gtfs_fetched_at) > GTFS_TTL:
        print(f"[gtfs] fetching ... ", end="", flush=True)
        req = Request(GTFS_URL, headers={"User-Agent": "ferry-board/1.0"})
        with urlopen(req, timeout=30) as r:
            data = r.read()
        print(f"{len(data):,} bytes")
        _gtfs_zip = zipfile.ZipFile(io.BytesIO(data))
        _gtfs_fetched_at = now
    return _gtfs_zip


# ── CSV / GTFS helpers ────────────────────────────────────────────────────────

def read_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
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
        if len(parts) < len(headers):
            parts += [""] * (len(headers) - len(parts))
        rows.append({h: parts[i].strip().strip('"') for i, h in enumerate(headers)})
    return rows


def _split_csv(line: str) -> list[str]:
    fields, cur, in_q = [], [], False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            fields.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def active_service_ids(calendar, cal_dates, today) -> set[str]:
    dow      = today.strftime("%A").lower()
    date_str = today.strftime("%Y%m%d")
    active: set[str] = set()
    for row in calendar:
        if row.get(dow, "0") != "1":
            continue
        if row["start_date"] <= date_str <= row["end_date"]:
            active.add(row["service_id"])
    for row in cal_dates:
        if row["date"] != date_str:
            continue
        if row["exception_type"] == "1":
            active.add(row["service_id"])
        elif row["exception_type"] == "2":
            active.discard(row["service_id"])
    return active


# ── Alerts ────────────────────────────────────────────────────────────────────

def fetch_alerts(now_ms: int) -> list[dict]:
    try:
        req = Request(ALERTS_URL, headers={"User-Agent": "ferry-board/1.0"})
        with urlopen(req, timeout=10) as r:
            alerts = json.loads(r.read())
        live = [a for a in alerts if int(a.get("expirationDate", 0)) > now_ms]
        relevant = [
            a for a in live
            if any(
                kw in (a.get("notificationTitle", "") + " " + a.get("notificationBody", "")).lower()
                for kw in ROUTE_KEYWORDS
            )
        ]
        print(f"[alerts] {len(relevant)} relevant active alert(s)")
        return relevant
    except Exception as exc:
        print(f"[alerts] fetch failed: {exc}")
        return []


# ── Core logic ────────────────────────────────────────────────────────────────

def compute_departures() -> dict:
    now_utc   = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=-4)   # EDT; change to -5 in winter
    today     = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    now_ms    = int(now_utc.timestamp() * 1000)

    zf = get_gtfs()

    stops      = read_csv(zf, "stops.txt")
    stop_times = read_csv(zf, "stop_times.txt")
    trips      = read_csv(zf, "trips.txt")
    calendar   = read_csv(zf, "calendar.txt")
    cal_dates  = read_csv(zf, "calendar_dates.txt")

    origin_ids = {s["stop_id"] for s in stops if all(k in s["stop_name"].lower() for k in ORIGIN_KEYWORDS)}
    dest_ids   = {s["stop_id"] for s in stops if all(k in s["stop_name"].lower() for k in DEST_KEYWORDS)}

    trip_stops: dict[str, list] = {}
    for st in stop_times:
        trip_stops.setdefault(st["trip_id"], []).append(st)
    for v in trip_stops.values():
        v.sort(key=lambda x: int(x.get("stop_sequence", 0)))

    # walk forward day by day until we have 30 upcoming departures
    upcoming = []
    day = today
    while len(upcoming) < 30 and (day - today).days < 7:
        sids       = active_service_ids(calendar, cal_dates, day)
        active_ids = {t["trip_id"] for t in trips if t["service_id"] in sids}
        day_deps   = []
        for tid, sts in trip_stops.items():
            if tid not in active_ids:
                continue
            ids = [s["stop_id"] for s in sts]
            o = next((i for i, sid in enumerate(ids) if sid in origin_ids), None)
            d = next((i for i, sid in enumerate(ids) if sid in dest_ids),   None)
            if o is None or d is None or o >= d:
                continue
            h, m, s = (int(x) for x in sts[o]["departure_time"].split(":"))
            dep_dt = day + timedelta(hours=h, minutes=m, seconds=s)
            if dep_dt > now_local:
                day_deps.append(dep_dt)
        day_deps.sort()
        upcoming.extend(day_deps)
        day += timedelta(days=1)
    upcoming = upcoming[:30]

    alerts = fetch_alerts(now_ms)

    def fmt(dt):
        h = dt.hour % 12 or 12
        return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"

    return {
        "origin":      "Greenpoint",
        "destination": "East 34th Street",
        "departures": [
            {
                "time":         fmt(dt),
                "minutes_away": int((dt - now_local).total_seconds() // 60),
                "status":       "live" if alerts else "scheduled",
                "is_today":     dt.date() == now_local.date(),
                "day_abbr":     ["MON","TUE","WED","THU","FRI","SAT","SUN"][dt.weekday()],
            }
            for dt in upcoming
        ],
        "alerts": [
            {
                "title": a["notificationTitle"],
                "body":  a["notificationBody"][:200],
            }
            for a in alerts
        ],
        "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":   # Render health check
            self.send_response(200)
            self.end_headers()
            return
        if self.path != "/departures":
            self.send_response(404)
            self.end_headers()
            return
        try:
            data = json.dumps(compute_departures()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            print(f"[server] error: {exc}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")


PORT = int(os.environ.get("PORT", 8765))

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[server] listening on http://0.0.0.0:{PORT}/departures")
    print(f"[server] Ctrl-C to stop")
    server.serve_forever()
