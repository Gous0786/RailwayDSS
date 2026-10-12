"""
Build a one-day replay feed for the corridor viewer.

Converts the scheduled services into chainage-over-time paths, which is the
form the map consumes: a train is a list of (seconds past midnight, km along
the corridor) points, and the client interpolates between them.

This is a replay of recorded timetable data, not a live feed. The structure is
deliberately what a live feed would produce, so a real COA/FOIS source can
replace it later without the viewer changing.

Output: web/data/replay.json

Usage:  python analysis/build_replay.py [--day MONDAY]
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ktv-psa"
WEB = ROOT / "web" / "data"
SCHEDULE = DATA / "KTV_PSA_Passenger_Schedule" / "SCHEDULE-KTV-PSA-Table 1.csv"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def main() -> None:
    day = "MONDAY"
    if "--day" in sys.argv:
        day = sys.argv[sys.argv.index("--day") + 1].upper()
    didx = DAYS.index(day)

    with open(ROUTE, newline="", encoding="utf-8-sig") as f:
        km = {r["STATION_CODE"].strip().upper(): float(r["CUM_DISTANCE"] or 0)
              for r in csv.DictReader(f)}

    with open(SCHEDULE, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)]

    by_train: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_train[r["TRAIN_NO"]].append(r)

    trains = []
    for tno, stops in by_train.items():
        stops.sort(key=lambda x: int(x["ROUTE_SEQ_NO"]))
        days = stops[0]["DAY_OF_SERVICES"].replace('"', "").split(",")
        if len(days) <= didx or days[didx] != "1":
            continue

        # ARRIVAL/DEPARTURE are seconds since midnight of day 1 of the journey,
        # so they are already monotonic across the route. Keep them raw here;
        # taking % 86400 first and re-unwrapping compounds the day offset.
        stop_pts = []           # (arrival, departure, chainage) per stop
        for s in stops:
            code = s["STTN_CODE"].strip().upper()
            if code not in km:
                continue
            try:
                arr = int(s["ARRIVAL"])
                dep = int(s["DEPARTURE"])
            except ValueError:
                continue
            stop_pts.append((arr, dep, km[code]))
        if len(stop_pts) < 2:
            continue

        # UP services are stored along the KTV->PSA route axis like DN ones,
        # but their times count backwards because the train actually runs
        # PSA->KTV. Reverse the STOP order - not the flattened point list,
        # which would also swap arrival before departure at each stop.
        if stop_pts[-1][0] < stop_pts[0][0]:
            stop_pts.reverse()

        raw = []
        for arr, dep, k in stop_pts:
            raw.append([arr, k])
            if dep != arr:
                raw.append([dep, k])

        # Re-base onto a wall clock: the train enters the section at its real
        # time of day, and may run past midnight by the few hours a 177 km
        # transit takes.
        t0 = raw[0][0]
        start_of_day = t0 % 86400
        pts = [[start_of_day + (t - t0), k] for t, k in raw]
        if any(b[0] < a[0] for a, b in zip(pts, pts[1:])):
            continue                      # genuinely inconsistent record

        trains.append({
            "id": tno,
            "type": stops[0]["TRAIN_TYPE"],
            "dir": stops[0]["DIRECTION"],
            "pts": pts,
        })

    trains.sort(key=lambda t: t["pts"][0][0])
    span = [min(t["pts"][0][0] for t in trains),
            max(t["pts"][-1][0] for t in trains)] if trains else [0, 86400]

    WEB.mkdir(parents=True, exist_ok=True)
    out = {
        "corridor": "KTV-PSA",
        "day": day,
        "kind": "replay",
        "source": "scheduled services (recorded timetable), not a live feed",
        "time_span": span,
        "trains": trains,
    }
    path = WEB / "replay.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")

    from collections import Counter
    kinds = Counter(t["type"] for t in trains)
    dirs = Counter(t["dir"] for t in trains)
    print(f"day              : {day}")
    print(f"trains           : {len(trains)}  {dict(dirs)}")
    print(f"types            : {dict(kinds.most_common(6))}")
    print(f"time span        : {span[0] // 3600:02d}:{span[0] % 3600 // 60:02d} "
          f"-> {span[1] // 3600:02d}:{span[1] % 3600 // 60:02d}")
    print(f"saved            : {path}  ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
