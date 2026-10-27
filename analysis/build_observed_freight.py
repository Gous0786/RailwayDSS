"""
Recorded freight paths, so the plan can be compared against what happened.

The demand builder reads each consignment's whole passage over the corridor
and then keeps only the first station and the last, because demand is just
"a rake presented here, at this time". The comparison needs exactly the part
that was thrown away: where the rake actually was, hour by hour.

This emits the same shape as the replay feed - chainage over time - so the
train graph can draw the recorded day and the planned day on one set of axes,
at one scale, with nothing rescaled between them.

Where a station records a departure later than its arrival, both are emitted.
That makes the standing time a flat segment on the graph rather than a number
in a table, which matters: standing time is the whole thing the optimiser is
trying to remove, and 45% of observed transit is exactly that.

What this is NOT: a verified plan. These are recorded movements, so they
contain whatever really happened - including waits for reasons outside this
section that the optimiser can neither see nor fix.

Output: data/derived/observed_freight.json

Usage:  python analysis/build_observed_freight.py [--day 2025-08-01]
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ktv-psa"
DERIVED = ROOT / "data" / "derived"
GOODS = DATA / "KTV-PSA-Infra" / "WAT_GOODS_TRAIN_AUG25_01082025_27082025.csv"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

MAX_TRANSIT_H = 48.0      # the bound the demand builder uses; kept identical
MIN_HOPS = 1              # a rake seen at one station only has no path


def rows(path: Path) -> list[dict]:
    """Read an ICMS extract, dropping any header row repeated mid-file."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        out = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
               for r in csv.DictReader(f)]
    if not out:
        return []
    key = next(iter(out[0]))
    return [r for r in out if r.get(key) and r[key] != key]


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return None


def corridor() -> tuple[list[str], dict[str, float]]:
    """Station order and official chainage, from the route extract.

    CUM_DISTANCE is the railway's own kilometrage, which is what the train
    graph plots. Taking it from here rather than from the OSM geometry keeps
    the recorded paths on the same axis as everything else.
    """
    order, km = [], {}
    for r in rows(ROUTE):
        code = r["STATION_CODE"].strip().upper()
        order.append(code)
        km[code] = float(r["CUM_DISTANCE"] or 0.0)
    return order, km


def passages(goods: list[dict], index: dict[str, int]):
    """Every corridor event per consignment: (arrival, departure, station)."""
    seen: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    for r in goods:
        station = (r.get("Sttn") or "").strip().upper()
        if station not in index:
            continue
        arr = parse_ts(r.get("Arrival Time", ""))
        dep = parse_ts(r.get("Depart Time", ""))
        if arr is None and dep is None:
            continue
        arr = arr or dep
        dep = dep or arr
        if dep < arr:                 # a clerical inversion; trust the arrival
            dep = arr
        seen[r["LoadId"]].append((arr, dep, station))
    return seen


def build(goods: list[dict], order: list[str], km: dict[str, float]):
    index = {c: i for i, c in enumerate(order)}
    by_day: dict[str, list[dict]] = defaultdict(list)
    dropped: dict[str, int] = defaultdict(int)

    for load_id, events in passages(goods, index).items():
        events.sort(key=lambda e: (e[0], index[e[2]]))
        if len({e[2] for e in events}) <= MIN_HOPS:
            dropped["single station"] += 1
            continue

        # A rake cannot leave one station after it reached the next. Where the
        # log says so the two records disagree and there is no way to tell
        # which is right, so the consignment is dropped rather than repaired:
        # a path invented here would be compared against the plan as if it
        # were evidence. About 4.5% of consignments go this way.
        if any(a[1] > b[0] for a, b in zip(events, events[1:])):
            dropped["inconsistent times"] += 1
            continue

        first, last = events[0], events[-1]
        # Measured arrival to arrival, the same span the demand extract calls
        # observed_transit_min. The rake has cleared the section once it
        # reaches its exit station; what it does in the yard after that is not
        # this section's business.
        transit = (last[0] - first[0]).total_seconds() / 60.0
        if not (0 < transit < MAX_TRANSIT_H * 60):
            dropped["implausible transit"] += 1
            continue

        # seconds from midnight of the day the rake joined the corridor, so a
        # path running past midnight simply continues past 86400 - the same
        # convention the replay feed uses.
        base = first[0].replace(hour=0, minute=0, second=0, microsecond=0)
        pts: list[list[float]] = []
        for i, (arr, dep, station) in enumerate(events):
            at = km[station]
            pts.append([round((arr - base).total_seconds(), 1), at])
            if dep > arr and i < len(events) - 1:
                pts.append([round((dep - base).total_seconds(), 1), at])
        if len(pts) < 2:
            dropped["no path"] += 1
            continue

        up = index[last[2]] < index[first[2]]
        # dwells up to the last arrival, so standing time is a part of the
        # transit above rather than something added on top of it
        standing = sum((dep - arr).total_seconds() / 60.0
                       for arr, dep, _ in events[:-1])
        by_day[first[0].date().isoformat()].append({
            "id": load_id,
            "type": "FREIGHT",
            "dir": "UP" if up else "DN",
            "entry": first[2],
            "exit": last[2],
            "hops": abs(index[last[2]] - index[first[2]]),
            "transit_min": round(transit, 1),
            "standing_min": round(standing, 1),
            "pts": pts,
        })

    return by_day, dict(dropped)


def day_stats(trains: list[dict]) -> dict:
    transits = [t["transit_min"] for t in trains]
    standing = [t["standing_min"] for t in trains]
    end_to_end = [t for t in trains if t["hops"] >= 19]
    return {
        "rakes": len(trains),
        "end_to_end": len(end_to_end),
        "median_transit_min": round(statistics.median(transits), 1),
        "median_standing_min": round(statistics.median(standing), 1),
        "standing_share_pct": round(
            100.0 * sum(standing) / sum(transits), 1) if sum(transits) else 0.0,
    }


def main() -> None:
    day_filter = None
    if "--day" in sys.argv:
        day_filter = sys.argv[sys.argv.index("--day") + 1]

    order, km = corridor()
    goods = rows(GOODS)

    print("=" * 74)
    print("RECORDED FREIGHT PATHS")
    print("=" * 74)
    print(f"\ngoods movement records : {len(goods)}")
    print(f"corridor stations      : {len(order)}  "
          f"({order[0]} 0 km - {order[-1]} {km[order[-1]]:.2f} km)")

    by_day, dropped = build(goods, order, km)
    total = sum(len(v) for v in by_day.values())
    print(f"\nrakes with a path      : {total} over {len(by_day)} days")
    if dropped:
        print(f"  dropped: {dropped}")

    days = {}
    for day in sorted(by_day):
        trains = sorted(by_day[day], key=lambda t: t["pts"][0][0])
        days[day] = {"stats": day_stats(trains), "trains": trains}

    busiest = max(days, key=lambda d: days[d]["stats"]["rakes"])
    s = days[busiest]["stats"]
    print(f"\nbusiest day            : {busiest}  {s['rakes']} rakes, "
          f"{s['end_to_end']} end to end")
    print(f"  median transit       : {s['median_transit_min']:.0f} min")
    print(f"  median standing      : {s['median_standing_min']:.0f} min")
    print(f"  standing share       : {s['standing_share_pct']:.0f}% of transit")

    if day_filter:
        if day_filter not in days:
            print(f"\nno recorded freight on {day_filter}")
            return
        days = {day_filter: days[day_filter]}
        print(f"\nfiltered to {day_filter}: "
              f"{days[day_filter]['stats']['rakes']} rake(s)")

    out = {
        "corridor": f"{order[0]}-{order[-1]}",
        "source": GOODS.name,
        "note": ("Recorded movements, not a verified plan. Standing time here "
                 "includes waits for causes outside this section, which the "
                 "optimiser can neither see nor remove."),
        "chainage": "official CUM_DISTANCE from the route extract, km",
        "days": days,
    }
    DERIVED.mkdir(parents=True, exist_ok=True)
    path = DERIVED / "observed_freight.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {path.relative_to(ROOT)}  "
          f"({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
