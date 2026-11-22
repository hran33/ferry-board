#!/usr/bin/env python3
"""
NYC Ferry – static schedule + live service alerts.

GTFS-Realtime investigation findings:
  - No public GTFS-RT feed exists on nycferry.connexionz.net.
    Every candidate path (/rtt/public/utility/gtfsrt.aspx, trip_updates.pb,
    /rtt/public/realtime/arrival.aspx, etc.) returns a 905-byte
    "This page doesn't exist" HTML page regardless of query parameters.
  - The official ferry.nyc website itself uses only static GTFS data
    (live_ferry_data.js contains get_static_trips() — no HTTP calls).
  - The one confirmed live API is the service alerts Cloud Function.

Strategy: static GTFS schedule + service alert overlay.
  status = "live"        → if a non-expired alert mentions this route/stop
  status = "scheduled"   → otherwise
  predicted_time, delay_minutes → always null (no RT feed)
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

GTFS_URL       = "https://nycferry.connexionz.net/rtt/public/utility/gtfs.aspx"
ALERTS_URL     = "https://us-central1-nyc-ferry.cloudfunctions.net/nycf_service_alerts?lang=en"

ORIGIN_KEYWORDS = ["greenpoint"]
DEST_KEYWORDS   = ["wall st", "pier 11"]

# Keywords that indicate an alert touches the East River route or our stops.
# Any alert whose title+body contains at least one of these (case-insensitive)
# is considered relevant and will set status → "live".
ROUTE_KEYWORDS = [
    "east river",
    "greenpoint",
    "wall st",
    "pier 11",
    "north williamsburg",
    "south williamsburg",
    "hunters point",
    "dumbo",
    "long island city",
    "roosevelt island",
    "astoria",
    "stuyvesant",
    "corlears",
    "brooklyn navy yard",
    " er ",          # route code with spaces
    "(er)",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def fetch(url: str, label: str) -> bytes:
    req = Request(url, headers={"User-Agent": "ferry-poc/1.0"})
    with urlopen(req, timeout=20) as r:
        data = r.read()
    print(f"[{label}] {len(data):,} bytes")
    return data


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


def match_stop(name: str, keywords: list[str]) -> bool:
    n = name.lower()
    return all(k in n for k in keywords)


# ── calendar ─────────────────────────────────────────────────────────────────

def active_service_ids(calendar: list[dict], cal_dates: list[dict],
                       today: datetime) -> set[str]:
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


# ── service alerts ────────────────────────────────────────────────────────────

def fetch_alerts(now_ms: int) -> list[dict]:
    """Return non-expired alerts from the Cloud Function."""
    try:
        raw = fetch(ALERTS_URL, "alerts")
        alerts = json.loads(raw)
    except Exception as exc:
        print(f"[alerts] fetch failed: {exc}")
        return []
    live = [a for a in alerts if int(a.get("expirationDate", 0)) > now_ms]
    print(f"[alerts] {len(live)} active alert(s)")
    return live


def relevant_alerts(alerts: list[dict]) -> list[dict]:
    """Filter alerts that mention our route or stops."""
    out = []
    for a in alerts:
        text = (a.get("notificationTitle", "") + " " +
                a.get("notificationBody", "")).lower()
        if any(kw in text for kw in ROUTE_KEYWORDS):
            out.append(a)
    return out


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    now_utc   = datetime.now(timezone.utc)
    ny_offset = timedelta(hours=-4)            # EDT; swap to -5 in winter
    now_local = now_utc + ny_offset
    today     = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    now_ms    = int(now_utc.timestamp() * 1000)

    # ── GTFS ─────────────────────────────────────────────────────────────────
    zf = zipfile.ZipFile(io.BytesIO(fetch(GTFS_URL, "gtfs")))

    stops_raw      = read_csv(zf, "stops.txt")
    stop_times_raw = read_csv(zf, "stop_times.txt")
    trips_raw      = read_csv(zf, "trips.txt")
    calendar_raw   = read_csv(zf, "calendar.txt")
    cal_dates_raw  = read_csv(zf, "calendar_dates.txt")

    # ── stop matching ─────────────────────────────────────────────────────────
    origin_stops = [s for s in stops_raw if match_stop(s["stop_name"], ORIGIN_KEYWORDS)]
    dest_stops   = [s for s in stops_raw if match_stop(s["stop_name"], DEST_KEYWORDS)]

    if not origin_stops:
        print("\n[WARN] No origin stops matched. All stop names:")
        for s in sorted({s["stop_name"] for s in stops_raw}):
            print(f"  {s!r}")
        return
    if not dest_stops:
        print("\n[WARN] No destination stops matched. All stop names:")
        for s in sorted({s["stop_name"] for s in stops_raw}):
            print(f"  {s!r}")
        return

    origin_ids = {s["stop_id"] for s in origin_stops}
    dest_ids   = {s["stop_id"] for s in dest_stops}
    print(f"[stops] origin → {[s['stop_name'] for s in origin_stops]}")
    print(f"[stops] dest   → {[s['stop_name'] for s in dest_stops]}")

    # ── calendar ──────────────────────────────────────────────────────────────
    active_sids = active_service_ids(calendar_raw, cal_dates_raw, today)
    print(f"[calendar] {today.strftime('%A %Y-%m-%d')} → service_ids: {active_sids}")

    # ── index stop_times by trip ──────────────────────────────────────────────
    trip_stops: dict[str, list[dict]] = {}
    for st in stop_times_raw:
        trip_stops.setdefault(st["trip_id"], []).append(st)
    for stops in trip_stops.values():
        stops.sort(key=lambda x: int(x.get("stop_sequence", 0)))

    active_trip_ids = {t["trip_id"] for t in trips_raw
                       if t["service_id"] in active_sids}
    print(f"[trips] {len(active_trip_ids)} active trips today")

    # ── find qualifying departures ────────────────────────────────────────────
    departures = []
    for tid, stops in trip_stops.items():
        if tid not in active_trip_ids:
            continue
        ids_seq = [s["stop_id"] for s in stops]
        o_idx = next((i for i, sid in enumerate(ids_seq) if sid in origin_ids), None)
        d_idx = next((i for i, sid in enumerate(ids_seq) if sid in dest_ids),   None)
        if o_idx is None or d_idx is None or o_idx >= d_idx:
            continue
        dep_str = stops[o_idx]["departure_time"]
        h, m, s = (int(x) for x in dep_str.split(":"))
        dep_dt  = today + timedelta(hours=h, minutes=m, seconds=s)
        departures.append({"trip_id": tid, "dep_dt": dep_dt})

    departures.sort(key=lambda x: x["dep_dt"])
    next3 = [d for d in departures if d["dep_dt"] > now_local][:3]

    if not next3:
        print("\n[result] No upcoming departures found for today.")
        return

    # ── service alerts ────────────────────────────────────────────────────────
    all_alerts   = fetch_alerts(now_ms)
    route_alerts = relevant_alerts(all_alerts)

    if route_alerts:
        print(f"[alerts] {len(route_alerts)} alert(s) relevant to this route:")
        for a in route_alerts:
            print(f"  • {a['notificationTitle']}")
    else:
        print("[alerts] no route-relevant alerts active")

    route_status = "live" if route_alerts else "scheduled"

    # ── format output ─────────────────────────────────────────────────────────
    def fmt_12h(dt: datetime) -> str:
        h    = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{h}:{dt.minute:02d} {ampm}"

    def mins_away(dt: datetime) -> int:
        return int((dt - now_local).total_seconds() // 60)

    result = {
        "origin":      "Greenpoint",
        "destination": "Wall St / Pier 11",
        "departures": [
            {
                "scheduled_time": fmt_12h(d["dep_dt"]),
                "predicted_time": None,    # no GTFS-RT available
                "delay_minutes":  None,    # no GTFS-RT available
                "minutes_away":   mins_away(d["dep_dt"]),
                "status": route_status,
            }
            for d in next3
        ],
        "service_alerts": [
            {
                "title": a["notificationTitle"],
                "body":  a["notificationBody"][:200] + ("…" if len(a["notificationBody"]) > 200 else ""),
            }
            for a in route_alerts
        ],
        "data_sources": {
            "schedule": "static GTFS (nycferry.connexionz.net)",
            "realtime":  "none — no public GTFS-RT feed exists",
            "alerts":    ALERTS_URL,
        },
        "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    print("\n" + json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
