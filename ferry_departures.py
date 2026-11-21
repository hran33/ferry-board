#!/usr/bin/env python3
"""
NYC Ferry GTFS proof of concept.
Returns the next 3 scheduled departures: Greenpoint → Wall St / Pier 11.
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

GTFS_URL = "https://nycferry.connexionz.net/rtt/public/utility/gtfs.aspx"

ORIGIN_KEYWORDS = ["greenpoint"]
DEST_KEYWORDS = ["wall st", "pier 11"]


# ── helpers ──────────────────────────────────────────────────────────────────

def fetch_gtfs(url: str) -> zipfile.ZipFile:
    print(f"[fetch] downloading GTFS from {url}")
    with urlopen(url, timeout=30) as resp:
        data = resp.read()
    print(f"[fetch] {len(data):,} bytes received")
    return zipfile.ZipFile(io.BytesIO(data))


def read_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
    """Read a GTFS text file into a list of dicts."""
    with zf.open(name) as f:
        lines = f.read().decode("utf-8-sig").splitlines()
    if not lines:
        return []
    # Strip BOM / whitespace from headers
    headers = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        # Simple split; GTFS fields rarely contain commas inside quotes,
        # but handle quoted fields just in case.
        parts = _split_csv_line(line)
        if len(parts) < len(headers):
            parts += [""] * (len(headers) - len(parts))
        rows.append({h: parts[i].strip() for i, h in enumerate(headers)})
    return rows


def _split_csv_line(line: str) -> list[str]:
    """Minimal quoted-CSV split."""
    fields, cur, in_q = [], [], False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def match_stop(name: str, keywords: list[str]) -> bool:
    n = name.lower()
    return all(kw in n for kw in keywords)


# ── calendar logic ────────────────────────────────────────────────────────────

def active_service_ids(calendar: list[dict], cal_dates: list[dict], today: datetime) -> set[str]:
    dow = today.strftime("%A").lower()          # monday, tuesday …
    date_str = today.strftime("%Y%m%d")
    start_key = "start_date"
    end_key = "end_date"

    # Base services active today by weekday pattern
    active = set()
    for row in calendar:
        if row.get(dow, "0") != "1":
            continue
        if row[start_key] <= date_str <= row[end_key]:
            active.add(row["service_id"])

    # Apply exceptions from calendar_dates.txt
    for row in cal_dates:
        if row["date"] != date_str:
            continue
        if row["exception_type"] == "1":        # added
            active.add(row["service_id"])
        elif row["exception_type"] == "2":      # removed
            active.discard(row["service_id"])

    return active


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    # NYC is UTC-4 (EDT) / UTC-5 (EST); ferry schedule uses local time
    ny_offset = timedelta(hours=-4)            # use EDT (summer)
    now_local = now_utc + ny_offset
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    zf = fetch_gtfs(GTFS_URL)

    print(f"[gtfs] files: {zf.namelist()}")

    stops_raw     = read_csv(zf, "stops.txt")
    stop_times_raw = read_csv(zf, "stop_times.txt")
    trips_raw     = read_csv(zf, "trips.txt")
    calendar_raw  = read_csv(zf, "calendar.txt")
    cal_dates_raw = read_csv(zf, "calendar_dates.txt")

    # ── Debug: print all stop names ───────────────────────────────────────────
    all_stop_names = sorted({s["stop_name"] for s in stops_raw})
    print("\n[stops] all stop names:")
    for n in all_stop_names:
        print(f"  {n!r}")

    # ── Find origin / destination stops ───────────────────────────────────────
    origin_stops = [s for s in stops_raw if match_stop(s["stop_name"], ORIGIN_KEYWORDS)]
    dest_stops   = [s for s in stops_raw if match_stop(s["stop_name"], DEST_KEYWORDS)]

    if not origin_stops:
        print(f"\n[WARN] No stops matched origin keywords {ORIGIN_KEYWORDS}")
        print("       Please check the stop names printed above and adjust ORIGIN_KEYWORDS.")
        return
    if not dest_stops:
        print(f"\n[WARN] No stops matched destination keywords {DEST_KEYWORDS}")
        print("       Please check the stop names printed above and adjust DEST_KEYWORDS.")
        return

    origin_ids = {s["stop_id"] for s in origin_stops}
    dest_ids   = {s["stop_id"] for s in dest_stops}

    print(f"\n[stops] origin matched: {[s['stop_name'] for s in origin_stops]}")
    print(f"[stops] dest   matched: {[s['stop_name'] for s in dest_stops]}")

    # ── Build today's active service IDs ─────────────────────────────────────
    active_sids = active_service_ids(calendar_raw, cal_dates_raw, today)
    print(f"\n[calendar] today ({today.strftime('%A %Y-%m-%d')}) active service_ids: {active_sids}")

    # ── Index stop_times by trip_id ───────────────────────────────────────────
    trip_stops: dict[str, list[dict]] = {}
    for st in stop_times_raw:
        tid = st["trip_id"]
        trip_stops.setdefault(tid, []).append(st)

    # Sort each trip's stops by stop_sequence
    for tid in trip_stops:
        trip_stops[tid].sort(key=lambda x: int(x.get("stop_sequence", 0)))

    # ── Active trip_ids ───────────────────────────────────────────────────────
    active_trip_ids = {t["trip_id"] for t in trips_raw if t["service_id"] in active_sids}
    print(f"[trips] active trips today: {len(active_trip_ids)}")

    # ── Find qualifying departures ────────────────────────────────────────────
    departures = []

    for tid, stops in trip_stops.items():
        if tid not in active_trip_ids:
            continue

        stop_ids_in_order = [s["stop_id"] for s in stops]

        # Find first occurrence of origin and destination
        origin_idx = next((i for i, sid in enumerate(stop_ids_in_order) if sid in origin_ids), None)
        dest_idx   = next((i for i, sid in enumerate(stop_ids_in_order) if sid in dest_ids),   None)

        if origin_idx is None or dest_idx is None:
            continue
        if origin_idx >= dest_idx:
            continue

        dep_time_str = stops[origin_idx]["departure_time"]   # HH:MM:SS (can be > 23:xx)

        # Parse possibly-overflow time (e.g. 25:03:00 for next-day)
        parts = dep_time_str.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        dep_dt = today + timedelta(hours=h, minutes=m, seconds=s)

        departures.append({
            "trip_id": tid,
            "dep_time_str": f"{h % 24:02d}:{m:02d}",
            "dep_dt": dep_dt,
        })

    # Sort and filter to future only
    departures.sort(key=lambda x: x["dep_dt"])
    future = [d for d in departures if d["dep_dt"] > now_local]
    next3 = future[:3]

    if not next3:
        print("\n[result] No upcoming departures found for today.")
        return

    # ── Format output ─────────────────────────────────────────────────────────
    def fmt_time(dt: datetime) -> str:
        h = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{h}:{dt.minute:02d} {ampm}"

    def minutes_away(dt: datetime) -> int:
        delta = dt - now_local
        return int(delta.total_seconds() // 60)

    result = {
        "origin": "Greenpoint",
        "destination": "Wall St / Pier 11",
        "departures": [
            {
                "time": fmt_time(d["dep_dt"]),
                "minutes": minutes_away(d["dep_dt"]),
            }
            for d in next3
        ],
        "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    print("\n" + json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
