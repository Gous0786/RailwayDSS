"""
The scheduled services, as a plan.

Passenger services are fixed obstacles: the optimiser threads freight around
them and the verifier checks nothing disturbed them. Both need the same
object, so it is built once here.

The timetable records when a train ran, never which line it used, so a line
has to be assigned. A train is put on the first line serving its direction
that is free, which is what the railway does and what keeps the assignment
from manufacturing conflicts that never happened.

    from pravah.timetable import scheduled_plan
    fixed = scheduled_plan(graph)        # 68 services as fixed movements
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

from .plan import SCHEDULED_STOP, Hold, Movement, Plan, TrainPlan
from .section import Direction, SectionGraph

REPLAY = Path(__file__).resolve().parent.parent / "web" / "data" / "replay.json"

DWELL_EPS = 0.1          # minutes; below this a stop is not a stop
PASSENGER_RAKE_M = 450.0


def _overlaps(busy: list[tuple[float, float]], a: float, b: float) -> bool:
    return any(a < y and x < b for x, y in busy)


def _free_line(g: SectionGraph, link: str, direction: Direction,
               a: float, b: float, busy, closed: set[str] | None = None) -> str | None:
    closed = closed or set()
    all_lines = g.links[link].lines
    usable = [ln for ln in all_lines if f"{link}:{ln.name}" not in closed]
    own = [ln for ln in usable
           if ln.nominal_direction in (direction, Direction.BOTH)]
    for ln in own or usable:
        if not _overlaps(busy[f"{link}:{ln.name}"], a, b):
            return ln.name
    # Nothing free. If this direction still has a line of its own, the train
    # simply cannot be placed and is skipped, as before - forcing it onto a
    # busy line would manufacture conflicts the railway never had. Only when
    # every line for this direction is CLOSED is the remaining line shared,
    # which is single-line working and genuinely what happens.
    own_all = [ln for ln in all_lines
               if ln.nominal_direction in (direction, Direction.BOTH)]
    if own_all and all(f"{link}:{ln.name}" in closed for ln in own_all) and usable:
        return usable[0].name
    return None


def _free_platform(g: SectionGraph, code: str, direction: Direction,
                   a: float, b: float, rake_m: float, busy) -> str | None:
    try:
        station = g.station(code)
    except KeyError:
        return None
    usable = [ln for ln in station.lines
              if ln.serves(direction) and ln.length_m and ln.length_m >= rake_m]
    # Four halts record no line lengths at all. The railway scheduled a stop
    # there, so a line evidently fits.
    if not usable:
        usable = [ln for ln in station.lines if ln.length_m is None]
    for ln in usable:
        if not _overlaps(busy[f"{code}:{ln.number}"], a, b):
            return ln.number
    return None


def scheduled_plan(g: SectionGraph, path: Path | str = REPLAY,
                   rake_length_m: float = PASSENGER_RAKE_M,
                   closures=()) -> Plan:
    """Build the day's scheduled services as a Plan of fixed movements.

    Closures apply to scheduled services too. A line withdrawn for an
    engineering block does not carry passenger trains either, so they are
    re-platformed onto what remains - which is what puts opposing traffic on
    one line and forces crossings.
    """
    closed = {c.resource for c in closures}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found - run analysis/build_replay.py")
    blob = json.loads(p.read_text(encoding="utf-8"))

    order = [s.code for s in g.stations]
    by_km = {round(s.cum_km, 2): s.code for s in g.stations}
    index = {c: i for i, c in enumerate(order)}

    line_busy: dict[str, list[tuple[float, float]]] = defaultdict(list)
    station_busy: dict[str, list[tuple[float, float]]] = defaultdict(list)
    trains: list[TrainPlan] = []
    skipped = 0

    for tr in blob["trains"]:
        stops: list[tuple[str, float, float]] = []
        for seconds, km in tr["pts"]:
            code = by_km.get(round(km, 2))
            if code is None:
                continue
            minute = seconds / 60.0
            if stops and stops[-1][0] == code:
                stops[-1] = (code, stops[-1][1], minute)
            else:
                stops.append((code, minute, minute))
        if len(stops) < 2:
            skipped += 1
            continue

        up = index[stops[0][0]] > index[stops[-1][0]]
        direction = Direction.UP if up else Direction.DOWN

        movements, holds, ok = [], [], True
        for (c1, _a1, d1), (c2, a2, _d2) in pairwise(stops):
            if abs(index[c1] - index[c2]) != 1:
                ok = False
                break
            link = g.link(c1, c2).id
            chosen = _free_line(g, link, direction, d1, a2, line_busy, closed)
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
                                      rake_length_m, station_busy)
                if pick is None:
                    continue
                station_busy[f"{code}:{pick}"].append((arrive, depart))
                holds.append(Hold(tr["id"], code, pick, arrive, depart,
                                  "scheduled stop", SCHEDULED_STOP))

        trains.append(TrainPlan(
            id=tr["id"], train_class=tr["type"], direction=direction,
            rake_length_m=rake_length_m, priority=g.priority(tr["type"]),
            movements=movements, holds=holds,
            scheduled={m.from_station: m.enter for m in movements}))

    span = blob.get("time_span", [0, 86400])
    plan = Plan(trains=trains, horizon=(span[0] / 60.0, span[1] / 60.0),
                label=f"scheduled services, {blob.get('day', '')}")
    plan.skipped = skipped        # type: ignore[attr-defined]
    return plan
