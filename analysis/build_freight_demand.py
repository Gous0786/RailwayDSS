"""
Extract freight demand from the goods movement log.

The log records movements that already happened. The optimiser needs the
opposite: a list of rakes presenting themselves at the section wanting to
cross. This inverts one into the other.

For each consignment the log is replayed over the corridor to recover where
and when it joined, where it left, and what it was. The result is demand -
"at 17:07 a loaded BOXN rake arrived at Pundi wanting to reach Kottavalasa" -
which is what CP-SAT schedules.

It also emits the distributions a traffic generator would need later: arrival
pattern, wagon mix, load state and per-block traversal times. That is
deliberate; the two jobs share all their inputs.

One honest modelling assumption: rake length is not recorded anywhere in the
extract, so it is derived from wagon type using standard Indian Railways rake
compositions. Length decides where a rake can be held, so this matters, and
the table below is the place to correct it.

Output: data/derived/freight_demand.json

Usage:  python analysis/build_freight_demand.py [--day 2025-08-05]
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ktv-psa"
DERIVED = ROOT / "data" / "derived"
GOODS = DATA / "KTV-PSA-Infra" / "WAT_GOODS_TRAIN_AUG25_01082025_27082025.csv"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

# Rake length and axle load by wagon type. NOT recorded in the extract -
# these are standard Indian Railways compositions (wagons x over-buffer
# length, plus locomotive). Length decides which loops a rake can be held in,
# so correct these here rather than anywhere downstream.
RAKE = {
    #  type        wagons  len_m  axle_t   description
    "BOXN":        (58, 686.0, 22.9),   # open hopper, mineral
    "BOXNHL":      (59, 660.0, 25.0),   # higher axle load variant
    "BOXNHL25T":   (59, 660.0, 25.0),
    "BOXNS":       (58, 686.0, 22.9),
    "BOXNR":       (58, 686.0, 22.9),
    "BCN":         (42, 622.0, 22.9),   # covered
    "BCNA":        (42, 622.0, 22.9),
    "BCNHL":       (42, 622.0, 25.0),
    "BTAP":        (50, 640.0, 20.3),   # tank
    "BTPN":        (50, 640.0, 20.3),
    "BLC":         (45, 640.0, 20.3),   # container flat
    "BLCA":        (45, 640.0, 20.3),
    "BLCB":        (45, 640.0, 20.3),
    "BOBRN":       (59, 600.0, 22.9),   # bottom discharge hopper
    "BOBYN":       (59, 600.0, 22.9),
    "BRN":         (50, 640.0, 22.9),   # flat, steel
    "BFNS":        (50, 640.0, 22.9),
    "BOST":        (58, 686.0, 22.9),
    "BOY":         (58, 686.0, 22.9),
}
DEFAULT_RAKE = (58, 650.0, 22.9)

# The extract carries dozens of wagon-type variants - BCNAHSM1, BRN22.9,
# BFNSM1 - that differ in brake gear or axle load but not materially in rake
# length. Matching the family prefix covers them without an endless table.
FAMILY = {
    "BOX":  (58, 686.0, 22.9),   # open hopper, mineral
    "BOB":  (59, 600.0, 22.9),   # bottom discharge hopper
    "BOS":  (58, 686.0, 22.9),
    "BCN":  (42, 622.0, 22.9),   # covered
    "BRN":  (50, 640.0, 22.9),   # flat, steel
    "BFN":  (50, 640.0, 22.9),   # flat
    "BT":   (50, 640.0, 20.3),   # tank
    "BL":   (45, 640.0, 20.3),   # container flat / low-sided
    "NMG":  (30, 620.0, 18.0),   # converted coaches, vehicle traffic
    "SHRN": (58, 620.0, 22.9),
}


def rake_for(wagon_type: str) -> tuple[int, float, float]:
    """Exact match, then longest family prefix, then the default."""
    t = (wagon_type or "").strip().upper()
    if t in RAKE:
        return RAKE[t]
    for prefix in sorted(FAMILY, key=len, reverse=True):
        if t.startswith(prefix):
            return FAMILY[prefix]
    return DEFAULT_RAKE

MAX_TRANSIT_H = 48.0          # beyond this it is a different cycle, not one run


@dataclass
class FreightDemand:
    """A rake presenting itself at the section, wanting to cross."""
    id: str
    rake_id: str
    entry_station: str
    exit_station: str
    direction: str                 # DN or UP
    ready_min: float               # minutes past midnight on its day
    day: str
    wagon_type: str
    loaded: bool
    commodity: str
    length_m: float
    axle_load_t: float
    hops: int
    observed_transit_min: float    # what actually happened, for comparison


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


def extract(route: list[str], goods: list[dict]) -> tuple[list[FreightDemand], dict]:
    index = {c: i for i, c in enumerate(route)}

    passages: dict[str, list[tuple[datetime, int, dict]]] = defaultdict(list)
    for r in goods:
        station = (r.get("Sttn") or "").strip().upper()
        if station not in index:
            continue
        when = parse_ts(r.get("Arrival Time", "")) or parse_ts(r.get("Depart Time", ""))
        if when:
            passages[r["LoadId"]].append((when, index[station], r))

    demand: list[FreightDemand] = []
    dropped = Counter()
    for load_id, events in passages.items():
        events.sort(key=lambda e: (e[0], e[1]))
        first, last = events[0], events[-1]
        if first[1] == last[1]:
            dropped["single station"] += 1
            continue
        transit = (last[0] - first[0]).total_seconds() / 60.0
        if not (0 < transit < MAX_TRANSIT_H * 60):
            dropped["implausible transit"] += 1
            continue

        rec = first[2]
        wagon = (rec.get("Load Type") or "").strip().upper()
        _, length_m, axle = rake_for(wagon)
        demand.append(FreightDemand(
            id=load_id,
            rake_id=(rec.get("RakeId") or "").strip(),
            entry_station=route[first[1]],
            exit_station=route[last[1]],
            direction="DN" if last[1] > first[1] else "UP",
            ready_min=first[0].hour * 60 + first[0].minute + first[0].second / 60.0,
            day=first[0].date().isoformat(),
            wagon_type=wagon or "UNKNOWN",
            loaded=(rec.get("LE", "").strip().upper() == "L"),
            commodity=(rec.get("Commodity") or "").strip(),
            length_m=length_m,
            axle_load_t=axle,
            hops=abs(last[1] - first[1]),
            observed_transit_min=round(transit, 1),
        ))

    demand.sort(key=lambda d: (d.day, d.ready_min))
    return demand, dict(dropped)


def calibration(demand: list[FreightDemand], route: list[str],
                goods: list[dict]) -> dict:
    """Distributions a traffic generator would need. Same inputs, so free."""
    per_day = Counter(d.day for d in demand)
    counts = sorted(per_day.values())

    gaps: dict[str, list[float]] = defaultdict(list)
    for direction in ("DN", "UP"):
        by_day: dict[str, list[float]] = defaultdict(list)
        for d in demand:
            if d.direction == direction:
                by_day[d.day].append(d.ready_min)
        for times in by_day.values():
            times.sort()
            gaps[direction] += [b - a for a, b in zip(times, times[1:]) if b > a]

    corridor = set()
    for a, b in zip(route, route[1:]):
        corridor |= {f"{a}-{b}", f"{b}-{a}"}
    block_speed: dict[str, list[float]] = defaultdict(list)
    for r in goods:
        bs = (r.get("Block Section") or "").strip().upper()
        if bs not in corridor:
            continue
        try:
            s = float(r.get("Speed", ""))
        except ValueError:
            continue
        if 1.0 <= s <= 100.0:
            block_speed[f"{bs}|{r.get('LE','').strip().upper()}"].append(s)

    def quantiles(xs: list[float]) -> dict:
        xs = sorted(xs)
        if not xs:
            return {}
        q = lambda p: xs[min(int(len(xs) * p), len(xs) - 1)]
        return {"n": len(xs), "p10": round(q(.10), 2), "median": round(q(.50), 2),
                "p90": round(q(.90), 2), "mean": round(statistics.mean(xs), 2)}

    return {
        "entries_per_day": {"median": counts[len(counts) // 2],
                            "min": counts[0], "max": counts[-1],
                            "days": len(counts)},
        "direction_split": dict(Counter(d.direction for d in demand)),
        "loaded_share": round(sum(d.loaded for d in demand) / max(len(demand), 1), 3),
        "wagon_mix": dict(Counter(d.wagon_type for d in demand).most_common(12)),
        "commodity_mix": dict(Counter(d.commodity for d in demand if d.commodity)
                              .most_common(10)),
        "interarrival_min": {k: quantiles(v) for k, v in gaps.items()},
        "hops": quantiles([float(d.hops) for d in demand]),
        "observed_transit_min": quantiles([d.observed_transit_min for d in demand]),
        "block_speed_kmph": {k: quantiles(v) for k, v in sorted(block_speed.items())
                             if len(v) >= 20},
    }


def main() -> None:
    day_filter = None
    if "--day" in sys.argv:
        day_filter = sys.argv[sys.argv.index("--day") + 1]

    route = [r["STATION_CODE"].strip().upper() for r in rows(ROUTE)]
    goods = rows(GOODS)

    print("=" * 74)
    print("FREIGHT DEMAND")
    print("=" * 74)
    print(f"\ngoods movement records : {len(goods)}")
    print(f"corridor stations      : {len(route)}  ({route[0]} - {route[-1]})")

    demand, dropped = extract(route, goods)
    print(f"\nconsignments resolved  : {len(demand)}")
    if dropped:
        print(f"  dropped: {dropped}")

    cal = calibration(demand, route, goods)
    e = cal["entries_per_day"]
    print(f"\nentries per day        : median {e['median']}  "
          f"range {e['min']}-{e['max']}  over {e['days']} days")
    print(f"direction              : {cal['direction_split']}")
    print(f"loaded share           : {cal['loaded_share'] * 100:.0f}%")
    print(f"wagon mix (top 5)      : "
          f"{dict(list(cal['wagon_mix'].items())[:5])}")
    tr = cal["observed_transit_min"]
    print(f"observed transit (min) : median {tr['median']}  p90 {tr['p90']}")
    for d in ("DN", "UP"):
        g = cal["interarrival_min"].get(d, {})
        if g:
            print(f"interarrival {d}        : median {g['median']} min  "
                  f"p10 {g['p10']}  p90 {g['p90']}")

    lengths = Counter(d.length_m for d in demand)
    print(f"\nderived rake lengths   : "
          f"{dict(sorted(lengths.items(), key=lambda kv: -kv[1])[:4])}")
    unknown = [d.wagon_type for d in demand
               if rake_for(d.wagon_type) == DEFAULT_RAKE
               and d.wagon_type not in RAKE]
    if unknown:
        print(f"  {len(unknown)} rake(s) matched neither a type nor a family, "
              f"so used the {DEFAULT_RAKE[1]:.0f} m default: "
              f"{sorted(set(unknown))[:6]}")
    else:
        print("  every rake matched a wagon type or family")

    if day_filter:
        demand = [d for d in demand if d.day == day_filter]
        print(f"\nfiltered to {day_filter}: {len(demand)} rake(s)")

    by_day: dict[str, list[dict]] = defaultdict(list)
    for d in demand:
        by_day[d.day].append(asdict(d))

    DERIVED.mkdir(parents=True, exist_ok=True)
    out = DERIVED / "freight_demand.json"
    out.write_text(json.dumps({
        "corridor": f"{route[0]}-{route[-1]}",
        "source": GOODS.name,
        "note": ("Demand inverted from recorded goods movements. Rake length is "
                 "derived from wagon type, not recorded in the source."),
        "rake_table": {k: {"wagons": v[0], "length_m": v[1], "axle_load_t": v[2]}
                       for k, v in RAKE.items()},
        "calibration": cal,
        "days": {k: v for k, v in sorted(by_day.items())},
    }, indent=1), encoding="utf-8")

    busiest = max(by_day.items(), key=lambda kv: len(kv[1]))
    print(f"\nsaved: {out}  ({out.stat().st_size // 1024} KB)")
    print(f"  {len(by_day)} day(s); busiest {busiest[0]} with {len(busiest[1])} rakes")
    print("=" * 74)


if __name__ == "__main__":
    main()
