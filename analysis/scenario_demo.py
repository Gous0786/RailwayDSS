"""
Mentor demonstration scenario.

A deliberately small, fully real slice of the problem: the first four block
sections of the KTV-PSA corridor (Kottavalasa Jn -> Vizianagaram Jn, 34.73 km)
on a Monday morning, using the actual passenger timetable.

It runs the same set of freight trains twice:

    GREEDY    - what a controller does today: dispatch as soon as the line
                ahead is clear, then keep going whenever the next block frees up.
    PLANNED   - what PRAVAH does: look at the whole morning's passenger
                timetable first, then choose departure times.

Usage:  python analysis/scenario_demo.py
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "ktv-psa"
SCHEDULE = DATA / "KTV_PSA_Passenger_Schedule" / "SCHEDULE-KTV-PSA-Table 1.csv"

# The four block sections we demonstrate on, with real distances (km).
STATIONS = ["KTV", "KPL", "ALM", "KUK", "VZM"]
NAMES = {"KTV": "Kottavalasa Jn", "KPL": "Kantakapalle", "ALM": "Alamanda",
         "KUK": "Koru Konda", "VZM": "Vizianagaram Jn"}
POS_KM = {"KTV": 0.0, "KPL": 7.74, "ALM": 16.97, "KUK": 24.08, "VZM": 34.73}

FREIGHT_KMPH = 30.0   # measured median running speed from the goods movement log
HEADWAY_MIN = 3.0     # clearance between a passenger clearing and freight entering
WINDOW = (6 * 60, 12 * 60)   # the morning we plan, in minutes past midnight


def hm(minutes: float) -> str:
    m = int(round(minutes)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def block_run_minutes(a: str, b: str) -> float:
    return (POS_KM[b] - POS_KM[a]) / FREIGHT_KMPH * 60.0


def load_passenger_paths() -> list[dict]:
    """Monday down-direction services crossing all five demo stations."""
    with open(SCHEDULE, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)]

    by_train: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_train[r["TRAIN_NO"]].append(r)

    paths = []
    for train_no, stops in by_train.items():
        stops.sort(key=lambda x: int(x["ROUTE_SEQ_NO"]))
        if stops[0]["DIRECTION"] != "DN":
            continue
        days = stops[0]["DAY_OF_SERVICES"].replace('"', "").split(",")
        if not days or days[0] != "1":       # Monday only
            continue
        at = {s["STTN_CODE"]: s for s in stops}
        if not all(code in at for code in STATIONS):
            continue
        times = {c: (int(at[c]["ARRIVAL"]) % 86400) / 60.0 for c in STATIONS}
        times["KTV"] = (int(at["KTV"]["DEPARTURE"]) % 86400) / 60.0
        if not (WINDOW[0] <= times["KTV"] < WINDOW[1]):
            continue
        paths.append({"train": train_no, "type": stops[0]["TRAIN_TYPE"], "t": times})
    return sorted(paths, key=lambda p: p["t"]["KTV"])


def passenger_occupancy(paths: list[dict]) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """When each block is occupied by a passenger train."""
    occ: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for p in paths:
        for a, b in zip(STATIONS, STATIONS[1:]):
            occ[(a, b)].append((p["t"][a], p["t"][b]))
    return occ


def block_free_at(occ, block, enter: float, exit_: float) -> bool:
    """Can freight hold this block for [enter, exit_] without meeting a passenger?"""
    for p_in, p_out in occ[block]:
        if enter < p_out + HEADWAY_MIN and p_in - HEADWAY_MIN < exit_:
            return False
    return True


def run_freight(occ, depart: float, horizon: float = 14 * 60) -> dict:
    """Move one freight from KTV to VZM, holding at stations when blocked."""
    t = depart
    halts, log = [], []
    for a, b in zip(STATIONS, STATIONS[1:]):
        run = block_run_minutes(a, b)
        waited = 0.0
        while not block_free_at(occ, (a, b), t, t + run):
            t += 1.0
            waited += 1.0
            if t > horizon:
                return {"completed": False, "depart": depart, "halts": halts, "log": log}
        if waited > 0:
            halts.append((a, waited))
        log.append((a, b, t, t + run, waited))
        t += run
    return {"completed": True, "depart": depart, "arrive": t,
            "journey": t - depart, "detention": sum(w for _, w in halts),
            "halts": halts, "log": log}


def reserve(occ, result) -> None:
    """Add a dispatched freight's own occupancy so later trains must respect it."""
    for a, b, enter, exit_, _ in result["log"]:
        occ[(a, b)].append((enter, exit_))


def plan_greedy(occ, ready_times: list[float]) -> list[dict]:
    """Today's practice: dispatch each rake as soon as the road ahead is clear."""
    out = []
    for ready in ready_times:
        t = ready
        while t < WINDOW[1] + 120:
            if block_free_at(occ, (STATIONS[0], STATIONS[1]), t,
                             t + block_run_minutes(STATIONS[0], STATIONS[1])):
                break
            t += 1.0
        r = run_freight(occ, t)
        if r["completed"]:
            reserve(occ, r)
        out.append(r)
    return out


def plan_optimised(occ, ready_times: list[float]) -> list[dict]:
    """PRAVAH: for each rake, search every departure minute for the best run.

    The objective is to clear the section as early as possible, with detention
    as the tie-breaker. Departing later than the first opportunity is allowed,
    and is frequently the better choice: a rake that waits for a clear road
    can overtake one that left earlier and is now standing in a loop.
    """
    out = []
    for ready in ready_times:
        best = None
        for depart in range(int(ready), int(WINDOW[1]) + 180):
            r = run_freight(occ, float(depart))
            if not r["completed"]:
                continue
            if best is None or (r["arrive"], r["detention"]) < (best["arrive"], best["detention"]):
                best = r
        if best:
            reserve(occ, best)
            out.append(best)
        else:
            out.append({"completed": False, "depart": ready, "halts": []})
    return out


def report(title: str, results: list[dict]) -> dict:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    done = [r for r in results if r.get("completed")]
    for i, r in enumerate(results, 1):
        if not r.get("completed"):
            print(f"  FREIGHT {i}: not cleared within the morning")
            continue
        halts = ", ".join(f"{NAMES[s]} {w:.0f}m" for s, w in r["halts"]) or "none"
        print(f"  FREIGHT {i}: depart {hm(r['depart'])}  arrive {hm(r['arrive'])}  "
              f"journey {r['journey']:.0f} min  detention {r['detention']:.0f} min")
        print(f"             held at: {halts}")
    if done:
        tot_d = sum(r["detention"] for r in done)
        tot_j = sum(r["journey"] for r in done)
        halts = sum(len(r["halts"]) for r in done)
        speed = 34.73 / (tot_j / len(done) / 60)
        print(f"\n  cleared {len(done)}/{len(results)} | total detention {tot_d:.0f} min | "
              f"loop halts {halts} | mean speed {speed:.1f} km/h")
        return {"cleared": len(done), "detention": tot_d, "halts": halts, "speed": speed}
    return {"cleared": 0, "detention": 0, "halts": 0, "speed": 0}


def main() -> None:
    paths = load_passenger_paths()

    print("=" * 74)
    print("KOTTAVALASA JN -> VIZIANAGARAM JN | 34.73 km | Monday morning")
    print("=" * 74)
    print(f"\nFixed passenger timetable ({len(paths)} services, cannot be moved):")
    print(f"  {'train':<8}{'type':<7}" + "".join(f"{c:>7}" for c in STATIONS))
    for p in paths:
        print(f"  {p['train']:<8}{p['type']:<7}" +
              "".join(f"{hm(p['t'][c]):>7}" for c in STATIONS))

    print("\nGaps between consecutive departures from Kottavalasa:")
    for a, b in zip(paths, paths[1:]):
        gap = b["t"]["KTV"] - a["t"]["KTV"]
        flag = "  <-- big enough for a freight run" if gap >= 69 else ""
        print(f"  {hm(a['t']['KTV'])} -> {hm(b['t']['KTV'])} : {gap:>3.0f} min{flag}")

    need = 34.73 / FREIGHT_KMPH * 60
    print(f"\n  A freight train at {FREIGHT_KMPH:.0f} km/h needs {need:.0f} min of clear road.")

    ready = [10 * 60] * 5   # five rakes standing at Kottavalasa, ready from 10:00

    g = report("TODAY - dispatch as soon as the line ahead is clear",
               plan_greedy(passenger_occupancy(paths), ready))
    o = report("PRAVAH - choose departure times against the whole morning",
               plan_optimised(passenger_occupancy(paths), ready))

    print(f"\n{'=' * 74}\nIMPACT\n{'=' * 74}")
    print(f"  {'':<22}{'today':>12}{'PRAVAH':>12}")
    print(f"  {'freight cleared':<22}{g['cleared']:>12}{o['cleared']:>12}")
    print(f"  {'total detention':<22}{str(int(g['detention'])) + ' min':>12}"
          f"{str(int(o['detention'])) + ' min':>12}")
    print(f"  {'loop halts':<22}{g['halts']:>12}{o['halts']:>12}")
    print(f"  {'mean speed':<22}{f'{g['speed']:.1f} km/h':>12}{f'{o['speed']:.1f} km/h':>12}")
    print("\n  Passenger timetable: completely unchanged in both cases.\n")


if __name__ == "__main__":
    main()
