"""
Phase 1 - Historical freight baseline for the KTV-PSA section.

Establishes what Indian Railways actually achieved on the Kottavalasa Jn - Palasa
corridor (176.83 km, 22 stations, Waltair division / ECoR) during 1-27 Aug 2025,
from block-section-level goods movement logs.

This is the number the optimiser has to beat. Run before building anything else.

Usage:  python analysis/baseline.py
"""

from __future__ import annotations

import csv
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "ktv-psa"
ROUTE_CSV = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"
GOODS_CSV = DATA / "KTV-PSA-Infra" / "WAT_GOODS_TRAIN_AUG25_01082025_27082025.csv"

CORRIDOR_KM = 176.83
# NOTE ON THE DATA DICTIONARY:
# The "Block Hrs" column is a DAY FRACTION, not hours. Verified: a 9.24 km block
# with Block Hrs = 0.006944 (= 1/144 day = 10 min) carries a stated Speed of
# 55.44 km/h, and 9.24 / (10/60) = 55.44 exactly. Multiply by 24 to get hours.
# The stated "Speed" column is internally consistent and is what we use.
HOURS_PER_BLOCK_HRS_UNIT = 24.0
# No freight rake on this route runs above this; anything higher is a bad row.
MAX_PLAUSIBLE_SPEED = 100.0
MIN_PLAUSIBLE_SPEED = 1.0
# A journey spanning more than this is a different consignment cycle, not one transit.
MAX_TRANSIT_HOURS = 48.0


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return None


def load_route() -> tuple[list[str], dict[str, int]]:
    stations = [r["STATION_CODE"].strip().upper() for r in read_csv(ROUTE_CSV)]
    return stations, {code: i for i, code in enumerate(stations)}


def corridor_block_sections(route: list[str]) -> set[str]:
    """Both directions of every adjacent station pair on the route."""
    sections = set()
    for a, b in zip(route, route[1:]):
        sections.add(f"{a}-{b}")
        sections.add(f"{b}-{a}")
    return sections


def block_speed_stats(goods: list[dict], corridor: set[str]) -> None:
    """Running speed inside block sections, raw vs cleaned.

    The reference implementation we studied hardcodes 63 km/h as the freight
    speed constant, taken from the raw mean of this column. That mean is
    destroyed by a long tail of impossible values (up to 20,901 km/h) caused by
    truncated Block Hrs values.
    """
    raw, cleaned = [], []
    for row in goods:
        if row["Block Section"].upper() not in corridor:
            continue
        try:
            speed = float(row["Speed"])
        except ValueError:
            continue
        if speed <= 0:
            continue
        raw.append(speed)
        if MIN_PLAUSIBLE_SPEED <= speed <= MAX_PLAUSIBLE_SPEED:
            cleaned.append(speed)

    print("\n--- Block running speed (corridor only) ---")
    for label, series in (("raw", raw), ("cleaned", cleaned)):
        if not series:
            continue
        s = sorted(series)
        print(
            f"  {label:<8} n={len(s):>6}  mean={statistics.mean(s):>7.1f}  "
            f"median={s[len(s) // 2]:>5.1f}  p95={s[int(len(s) * 0.95)]:>6.1f}  max={s[-1]:>9.1f}"
        )
    if raw and cleaned:
        print(f"  -> raw mean ({statistics.mean(raw):.1f}) is an artefact of the tail; "
              f"use median of cleaned ({sorted(cleaned)[len(cleaned) // 2]:.1f} km/h)")


def transit_baseline(goods: list[dict], route: list[str], idx: dict[str, int]) -> None:
    """Per-consignment corridor transit time, including all detention."""
    journeys: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for row in goods:
        station = row["Sttn"].upper()
        if station not in idx:
            continue
        ts = parse_ts(row["Arrival Time"]) or parse_ts(row["Depart Time"])
        if ts:
            journeys[row["LoadId"]].append((ts, idx[station]))

    partial, full = [], []
    starts_per_day: Counter[object] = Counter()
    near_full = len(route) - 3

    for events in journeys.values():
        events.sort()
        hops = abs(events[-1][1] - events[0][1])
        hours = (events[-1][0] - events[0][0]).total_seconds() / 3600
        if hops < 2 or not (0 < hours < MAX_TRANSIT_HOURS):
            continue
        partial.append(hours)
        starts_per_day[events[0][0].date()] += 1
        if hops >= near_full:
            full.append(hours)

    def pct(series: list[float], p: float) -> float:
        return sorted(series)[int(len(series) * p)]

    daily = sorted(starts_per_day.values())
    print("\n--- Freight volume ---")
    print(f"  consignments touching corridor (>=2 hops): {len(partial)}")
    print(f"  end-to-end traversals:                     {len(full)}")
    print(f"  starts/day: median={daily[len(daily) // 2]}  min={daily[0]}  max={daily[-1]}  over {len(daily)} days")
    print(f"  end-to-end traversals/day: {len(full) / len(daily):.1f}")

    print("\n--- Corridor transit time (includes detention) ---")
    print(f"  any partial run : median={pct(partial, 0.5):>6.2f} h  p25={pct(partial, 0.25):.2f}  p75={pct(partial, 0.75):.2f}")
    if full:
        med = pct(full, 0.5)
        effective = CORRIDOR_KM / med
        print(f"  full corridor   : median={med:>6.2f} h  p25={pct(full, 0.25):.2f}  p75={pct(full, 0.75):.2f}")
        print(f"\n  BASELINE: {CORRIDOR_KM} km in {med:.2f} h -> {effective:.1f} km/h effective")
        return effective
    return None


def main() -> None:
    route, idx = load_route()
    goods = read_csv(GOODS_CSV)
    corridor = corridor_block_sections(route)

    print("=" * 68)
    print(f"KTV-PSA BASELINE  |  {len(route)} stations, {CORRIDOR_KM} km  |  1-27 Aug 2025")
    print("=" * 68)
    print(f"  goods movement rows (division-wide): {len(goods)}")
    print(f"  rows on corridor block sections:     "
          f"{sum(1 for r in goods if r['Block Section'].upper() in corridor)}")

    block_speed_stats(goods, corridor)
    effective = transit_baseline(goods, route, idx)

    if effective:
        cleaned = sorted(
            float(r["Speed"]) for r in goods
            if r["Block Section"].upper() in corridor and r["Speed"]
            and MIN_PLAUSIBLE_SPEED <= float(r["Speed"]) <= MAX_PLAUSIBLE_SPEED
        )
        running = cleaned[len(cleaned) // 2]
        detention = (1 - effective / running) * 100
        print(f"  running speed when moving:  {running:.1f} km/h")
        print(f"  => ~{detention:.0f}% of freight transit time is detention, not motion.")
        print("\n  That gap is the headroom this project targets.")


if __name__ == "__main__":
    main()
