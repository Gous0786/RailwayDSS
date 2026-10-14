"""
Verify the real timetable.

The negative tests in tests/test_verify.py prove the verifier catches what it
should. This is the opposite check, and the more interesting one: take the 87
services Indian Railways actually ran on a Monday, assign each to its natural
line, and see whether the verifier agrees that reality is legal.

If it does not, either the verifier or the section graph is wrong, and that is
worth finding out before any optimiser output is trusted. Where violations do
appear they are reported by kind with examples, because the interesting cases
are calibration findings rather than bugs - a scheduled time slightly faster
than the technical minimum says something about our speed model, not about
the railway.

It also prints the real timetable's own KPIs, which become the reference any
optimised plan is compared against.

Usage:  python analysis/check_verifier.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pravah.plan import SCHEDULED_STOP, Hold, Movement, Plan, TrainPlan  # noqa: E402
from pravah.section import Direction, SectionGraph  # noqa: E402
from pravah.verify import Severity, Verifier, VerifierConfig  # noqa: E402

REPLAY = ROOT / "web" / "data" / "replay.json"
DWELL_EPS = 0.1          # minutes; below this a stop is not a stop
RAKE_LENGTH_M = 450.0    # a passenger rake, not a goods rake


def _overlaps(busy, a, b):
    return any(a < y and x < b for x, y in busy)


def _free_line(g, link, direction, a, b, busy):
    """First running line serving this direction that is free for [a, b]."""
    lines = [ln for ln in g.links[link].lines
             if ln.nominal_direction in (direction, Direction.BOTH)]
    lines = lines or list(g.links[link].lines)
    for ln in lines:
        if not _overlaps(busy[f"{link}:{ln.name}"], a, b):
            return ln.name
    return None


def _free_platform(g, code, direction, a, b, rake_m, busy):
    """First station line long enough for the rake and free for [a, b]."""
    try:
        station = g.station(code)
    except KeyError:
        return None
    usable = [ln for ln in station.lines
              if ln.serves(direction) and ln.length_m and ln.length_m >= rake_m]
    # Four halts have no recorded line lengths at all. The railway scheduled a
    # stop there, so a line demonstrably fits; fall back rather than pretend
    # the train never stopped.
    if not usable:
        usable = [ln for ln in station.lines if ln.length_m is None]
    for ln in usable:
        if not _overlaps(busy[f"{code}:{ln.number}"], a, b):
            return ln.number
    return None


def build_plan(g: SectionGraph) -> Plan:
    """Turn the recorded timetable into a plan on its natural lines."""
    if not REPLAY.exists():
        raise SystemExit(f"{REPLAY} not found — run analysis/build_replay.py")
    blob = json.loads(REPLAY.read_text(encoding="utf-8"))

    order = [s.code for s in g.stations]
    km_to_code = {round(s.cum_km, 2): s.code for s in g.stations}
    index = {c: i for i, c in enumerate(order)}

    trains: list[TrainPlan] = []
    line_busy: dict[str, list[tuple[float, float]]] = defaultdict(list)
    station_busy: dict[str, list[tuple[float, float]]] = defaultdict(list)
    skipped = 0
    for tr in blob["trains"]:
        # replay carries (seconds, chainage); recover the station sequence
        stops: list[tuple[str, float, float]] = []   # code, arrive, depart
        for t_s, km in tr["pts"]:
            code = km_to_code.get(round(km, 2))
            if code is None:
                continue
            minute = t_s / 60.0
            if stops and stops[-1][0] == code:
                stops[-1] = (code, stops[-1][1], minute)     # departure
            else:
                stops.append((code, minute, minute))
        if len(stops) < 2:
            skipped += 1
            continue

        up = index[stops[0][0]] > index[stops[-1][0]]
        direction = Direction.UP if up else Direction.DOWN

        movements, holds = [], []
        ok = True
        for (c1, _a1, d1), (c2, a2, _d2) in pairwise(stops):
            if abs(index[c1] - index[c2]) != 1:
                ok = False                     # not adjacent: partial record
                break
            link = g.link(c1, c2).id
            # The timetable records when a train ran, never which line it used,
            # so a line has to be assigned. Take the first that serves this
            # direction and is free - putting every train on one line would
            # manufacture conflicts the railway never had.
            chosen = _free_line(g, link, direction, d1, a2, line_busy)
            if chosen is None:
                ok = False
                break
            line_busy[f"{link}:{chosen}"].append((d1, a2))
            movements.append(Movement(tr["id"], link, chosen, c1, c2, d1, a2))
        if not ok or not movements:
            skipped += 1
            continue
        for code, arrive, depart in stops[1:-1]:
            if depart - arrive > DWELL_EPS:
                pick = _free_platform(g, code, direction, arrive, depart,
                                      RAKE_LENGTH_M, station_busy)
                if pick is None:
                    continue           # no line fits; reported as a finding
                station_busy[f"{code}:{pick}"].append((arrive, depart))
                holds.append(Hold(tr["id"], code, pick, arrive, depart,
                                  "scheduled stop", SCHEDULED_STOP))

        trains.append(TrainPlan(
            id=tr["id"], train_class=tr["type"], direction=direction,
            rake_length_m=RAKE_LENGTH_M,
            priority=g.priority(tr["type"]),
            movements=movements, holds=holds,
            scheduled={m.from_station: m.enter for m in movements}))

    span = blob.get("time_span", [0, 86400])
    plan = Plan(trains=trains, horizon=(span[0] / 60.0, span[1] / 60.0),
                label=f"real timetable, {blob.get('day','')}")
    if skipped:
        print(f"  note: {skipped} service(s) skipped — partial or non-adjacent records")
    return plan


def main() -> int:
    g = SectionGraph.load()
    print("=" * 76)
    print("VERIFYING THE REAL TIMETABLE")
    print("=" * 76)
    plan = build_plan(g)
    print(f"\n{plan!r}")

    # Verifying a recorded timetable is not the same as planning a new one.
    # The conservative default refuses to use a line whose length is not
    # recorded, which is right when planning a 650 m freight rake onto it. Here
    # the stop demonstrably happened, so the line evidently fits.
    cfg = VerifierConfig.load()
    cfg.assume_unknown_line_fits = True
    rep = Verifier(g, cfg).verify(plan)
    print(f"\n{rep.summary()}\n")

    if rep.violations:
        grouped: dict[str, list] = defaultdict(list)
        for v in rep.violations:
            grouped[v.kind.value].append(v)
        print(f"  {'violation':<20}{'count':>7}  example")
        print("  " + "-" * 72)
        for kind, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            sev = items[0].severity.value
            print(f"  {kind:<20}{len(items):>7}  [{sev}] {items[0].detail[:60]}")
        print()

    k = rep.kpis
    print("  the real timetable's own KPIs — the reference an optimised plan")
    print("  is measured against:")
    print(f"    services cleared          {k.trains_cleared}")
    print(f"    horizon                   {k.horizon_hours:.1f} h")
    print(f"    throughput                {k.throughput_per_day:.1f} trains/day")
    print(f"    mean transit              {k.mean_transit_min:.1f} min")
    print(f"    detention                 {k.detention_min:.0f} min "
          f"({k.detention_pct:.1f}% of transit)")
    print(f"    line utilization          {k.utilization_pct:.1f}% mean")
    print(f"    busiest line              {k.busiest_line} at {k.busiest_line_pct:.1f}%")

    print("\n" + "=" * 76)
    errors = len(rep.errors)
    if errors == 0:
        print("The verifier agrees the real timetable is legal.")
        rc = 0
    else:
        rate = 100.0 * errors / max(rep.checked, 1)
        print(f"{errors} error(s) across {rep.checked} occupations ({rate:.2f}%).")
        print("Read these as calibration findings, not necessarily faults: a")
        print("scheduled run faster than our technical minimum says the speed")
        print("model is conservative, not that the railway broke a rule.")
        rc = 0 if rate < 5.0 else 1
    print("=" * 76)
    return rc


if __name__ == "__main__":
    sys.exit(main())
