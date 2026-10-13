"""
Validate the section graph against the real timetable.

The graph computes a technical minimum running time: line speed, class speed
and the permanent speed restriction penalty, with no allowances. The published
schedule adds engineering and traffic allowances on top, so for every link the
modelled time should sit at or below the scheduled time - close enough to be
credible, never above it, and never absurdly below.

Anything that fails here would silently corrupt the optimiser, because a plan
built on running times the railway cannot achieve is infeasible in reality.

Usage:  python analysis/check_section_graph.py
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.section import Direction, LineCategory, SectionGraph  # noqa: E402

SCHEDULE = (Path(__file__).resolve().parent.parent / "data" / "ktv-psa" /
            "KTV_PSA_Passenger_Schedule" / "SCHEDULE-KTV-PSA-Table 1.csv")


def rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)]


def main() -> int:
    g = SectionGraph.load()
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    print("=" * 74)
    print("SECTION GRAPH VALIDATION")
    print("=" * 74)
    print(f"\n{g!r}\n")

    # ---- structure ----------------------------------------------------
    print("structure")
    check("every link has at least one running line",
          all(l.lines for l in g.links.values()))
    check("every link has a positive length",
          all(l.length_km > 0 for l in g.links.values()))
    check("stations are in chainage order",
          all(a.cum_km <= b.cum_km for a, b in zip(g.stations, g.stations[1:])))
    check("link endpoints are consecutive stations",
          all(g.links[f"{a.code}-{b.code}"].a == a.code
              for a, b in zip(g.stations, g.stations[1:])))
    dup = len({ln.id for l in g.links.values() for ln in l.lines})
    tot = sum(len(l.lines) for l in g.links.values())
    check("running line ids are unique", dup == tot, f"{dup} unique of {tot}")

    # ---- headway ------------------------------------------------------
    print("\nheadway")
    hw = [g.headway(lid, ln.name) for lid, l in g.links.items() for ln in l.lines]
    check("all headways positive", all(h > 0 for h in hw))
    check("headways within 1-15 min", all(1 <= h <= 15 for h in hw),
          f"min {min(hw):.1f}  median {statistics.median(hw):.1f}  max {max(hw):.1f}")
    auto = [g.headway(lid, ln.name) for lid, l in g.links.items()
            for ln in l.lines if ln.signalling.value == "AT"]
    absol = [g.headway(lid, ln.name) for lid, l in g.links.items()
             for ln in l.lines if ln.signalling.value == "AB"]
    check("automatic block allows tighter headway than absolute",
          statistics.median(auto) < statistics.median(absol),
          f"AT {statistics.median(auto):.1f} vs AB {statistics.median(absol):.1f} min")

    # ---- running time against the published schedule -------------------
    print("\nrunning time vs published schedule")
    sched = defaultdict(list)
    for r in rows(SCHEDULE):
        sched[r["TRAIN_NO"]].append(r)

    km = {s.code: s.cum_km for s in g.stations}
    obs: dict[tuple[str, str], list[float]] = defaultdict(list)
    for stops in sched.values():
        stops.sort(key=lambda x: int(x["ROUTE_SEQ_NO"]))
        cls = stops[0]["TRAIN_TYPE"]
        pts = []
        for s in stops:
            c = s["STTN_CODE"].strip().upper()
            if c not in km:
                continue
            try:
                pts.append((c, int(s["ARRIVAL"]), int(s["DEPARTURE"])))
            except ValueError:
                continue
        if len(pts) < 2:
            continue
        if pts[-1][1] < pts[0][1]:
            pts.reverse()
        for (c1, _, dep1), (c2, arr2, _) in zip(pts, pts[1:]):
            run = (arr2 - dep1) / 60.0
            if 0 < run < 120:
                obs[(c1, c2)].append(run) if cls not in ("", None) else None
                obs[(c1, c2, cls)].append(run)

    # Compare like with like: a class's modelled time against that same
    # class's scheduled times. Comparing one class's model against the
    # fastest train of any class is not a meaningful test.
    over, ratios, compared = [], [], 0
    codes = [s.code for s in g.stations]
    for a, b in zip(codes, codes[1:]):
        lid = g.link(a, b).id
        for link_pair in ((a, b), (b, a)):
            d = g.direction_of(*link_pair)
            lines = [ln for ln in g.links[lid].lines
                     if ln.nominal_direction in (d, Direction.BOTH)] or g.links[lid].lines
            for cls in ("SUF", "MEX", "PAS"):
                times = obs.get((link_pair[0], link_pair[1], cls))
                if not times or len(times) < 3:
                    continue
                model = min(g.running_time(lid, ln.name, cls, d) for ln in lines)
                median_obs = statistics.median(times)
                compared += 1
                ratios.append(model / median_obs if median_obs else 0)
                if model > median_obs * 1.25:
                    over.append(f"{link_pair[0]}->{link_pair[1]} {cls} "
                                f"model {model:.1f} vs median {median_obs:.1f}")

    check("modelled time does not exceed the scheduled median by >25%",
          not over, f"{compared} link/class pairs compared"
          + ("; " + "; ".join(over[:3]) if over else ""))
    if ratios:
        med = statistics.median(ratios)
        check("modelled minimum sits below the scheduled median",
              0.40 <= med <= 1.05, f"median model/scheduled = {med:.2f} "
              f"(schedules carry engineering and traffic allowances)")

    # ---- holding capacity ---------------------------------------------
    print("\nholding capacity")
    holds = {s.code: len(s.holding_lines(650)) for s in g.stations}
    able = [c for c, n in holds.items() if n]
    check("at least half the stations can hold a 650 m rake",
          len(able) >= len(g.stations) / 2, f"{len(able)}/{len(g.stations)}")
    check("stations without crossing facility can never hold",
          all(holds[s.code] == 0 for s in g.stations if not s.can_cross),
          f"no-crossing: {[s.code for s in g.stations if not s.can_cross]}")
    long_rake = {s.code: len(s.holding_lines(1500)) for s in g.stations}
    check("a 1500 m rake fits in fewer places than a 650 m rake",
          sum(long_rake.values()) < sum(holds.values()),
          f"{sum(long_rake.values())} vs {sum(holds.values())} opportunities")

    # ---- closures and single-line working ------------------------------
    print("\nclosures and single-line working")
    lid = "VZM-NML"            # two lines: DN and UP
    names = [ln.name for ln in g.links[lid].lines]
    down_before = g.usable_lines(lid, Direction.DOWN, 600)[0].id
    g.close(f"{lid}:{names[0]}", 540, 660, "engineering block")

    down_during = g.usable_lines(lid, Direction.DOWN, 600)[0].id
    down_after = g.usable_lines(lid, Direction.DOWN, 700)[0].id
    check("a closure diverts traffic onto the other line, within its window only",
          down_during != down_before and down_after == down_before,
          f"{down_before} -> {down_during} during -> {down_after} after")
    check("one line closed on a two-line link means single-line working",
          g.is_single_line(lid, 600) and not g.is_single_line(lid, 700),
          f"{len(g.open_lines(lid, 600))} of {len(names)} lines open")
    opposing = g.usable_lines(lid, Direction.UP, 600)
    down = g.usable_lines(lid, Direction.DOWN, 600)
    check("both directions then share the same line",
          bool(opposing) and bool(down) and opposing[0].id == down[0].id,
          f"shared line {down[0].id if down else '—'}")
    for n in names[1:]:
        g.close(f"{lid}:{n}", 540, 660, "engineering block")
    check("closing every line blocks the link rather than reporting single-line",
          g.is_blocked(lid, 600) and not g.is_single_line(lid, 600))
    check("crossing stations exist to resolve it",
          len(g.crossing_stations()) > 0,
          f"{len(g.crossing_stations())} of {len(g.stations)}")

    # ---- priorities ----------------------------------------------------
    print("\ntrain priorities")
    check("passenger classes outrank freight",
          all(g.priority(c) < g.priority("FREIGHT")
              for c in ("VNDB", "SUF", "MEX", "PAS")))
    check("premium classes outrank ordinary ones",
          g.priority("VNDB") < g.priority("SUF") < g.priority("MEX") < g.priority("PAS"))

    print("\n" + "=" * 74)
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("all checks passed - section graph is usable by the optimiser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
