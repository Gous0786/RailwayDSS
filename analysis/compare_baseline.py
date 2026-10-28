"""
Greedy dispatch against CP-SAT, on identical inputs.

The question this answers is narrow and it is the one the project has to
answer: what does look-ahead buy? Comparing a plan against the recorded day
cannot say, because the record contains yard work, crew changes and waiting
for a path through the next section, none of which any scheduler here could
remove. Comparing against a dispatcher that shares everything except the
decision rule can.

Both schedulers get the same section graph, the same running times, the same
signalling rules, the same routing, the same demand and the same verifier.
Only the rule differs: greedy sends each rake as soon as the road is clear,
CP-SAT looks at the whole window first.

Two traps this avoids:

  - Totals across different rakes. If one scheduler dispatches a rake the
    other refused, its total detention rises for a good reason. Sums are
    reported, but the headline is PAIRED over the rakes both dispatched.
  - Medians over a mixed population. Rakes crossing one link and rakes
    crossing twenty-one are not comparable, so per-rake figures are quoted
    beside the hop count.

Usage:
    python analysis/compare_baseline.py
    python analysis/compare_baseline.py --limit 60
    python analysis/compare_baseline.py --closure VZM-NML:DN:480:720
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pravah.demand import DemandBook              # noqa: E402
from pravah.greedy import GreedyDispatcher        # noqa: E402
from pravah.optimise import Optimiser, OptimiserConfig  # noqa: E402
from pravah.plan import Plan, PlannedClosure      # noqa: E402
from pravah.section import SectionGraph           # noqa: E402
from pravah.timetable import scheduled_plan       # noqa: E402
from pravah.verify import Verifier, VerifierConfig  # noqa: E402

DERIVED = ROOT / "data" / "derived"


def parse_closure(text: str) -> PlannedClosure:
    """LINK:LINE:start:end, e.g. VZM-NML:DN:480:720 (minutes from midnight)."""
    link, line, start, end = text.rsplit(":", 3)
    return PlannedClosure(f"{link}:{line}", float(start), float(end),
                          "engineering block")


def check(graph, plan: Plan, fixed: Plan, tolerance: float = 0.0):
    """Violations this plan introduces, over and above the timetable's own."""
    cfg = VerifierConfig.load()
    cfg.assume_unknown_line_fits = True
    cfg.service_tolerance_min = tolerance
    v = Verifier(graph, cfg)
    base = {str(x) for x in v.verify(fixed).errors}
    combined = Plan(trains=plan.trains + fixed.trains, horizon=plan.horizon)
    result = v.verify(combined)
    return [x for x in result.errors if str(x) not in base], result.kpis


def by_id(plan: Plan) -> dict:
    return {t.id: t for t in plan.trains}


def median(xs):
    return statistics.median(xs) if xs else 0.0


def compare(graph, demand, fixed, cfg) -> dict:
    gp, gr = GreedyDispatcher(graph, cfg).solve(demand, fixed)
    op, orr = Optimiser(graph, cfg).solve(demand, fixed)

    g_bad, g_kpi = check(graph, gp, fixed, cfg.service_tolerance_min)
    o_bad, o_kpi = check(graph, op, fixed, cfg.service_tolerance_min)

    G, O = by_id(gp), by_id(op)
    both = sorted(set(G) & set(O))

    # paired: the same rake under each scheduler
    rows = []
    for rid in both:
        a, b = G[rid], O[rid]
        rows.append({
            "id": rid,
            "hops": len(a.movements),
            "greedy_detention": a.detention,
            "cpsat_detention": b.detention,
            "greedy_transit": a.transit or 0.0,
            "cpsat_transit": b.transit or 0.0,
        })

    better = sum(1 for r in rows if r["cpsat_transit"] < r["greedy_transit"] - 1e-6)
    worse = sum(1 for r in rows if r["cpsat_transit"] > r["greedy_transit"] + 1e-6)

    return {
        "offered": gr.offered,
        "greedy": {
            "dispatched": gr.dispatched, "seconds": round(gr.seconds, 2),
            "violations": len(g_bad), "kpis": g_kpi.to_dict(),
            "only_here": sorted(set(G) - set(O)),
        },
        "cpsat": {
            "dispatched": orr.dispatched, "seconds": round(orr.seconds, 2),
            "violations": len(o_bad), "kpis": o_kpi.to_dict(),
            "only_here": sorted(set(O) - set(G)),
            "gaps": [s.gap_pct for s in orr.stages],
        },
        "paired": {
            "rakes": len(rows),
            "greedy_detention_total": round(sum(r["greedy_detention"] for r in rows), 1),
            "cpsat_detention_total": round(sum(r["cpsat_detention"] for r in rows), 1),
            "greedy_detention_median": round(median([r["greedy_detention"] for r in rows]), 1),
            "cpsat_detention_median": round(median([r["cpsat_detention"] for r in rows]), 1),
            "greedy_transit_total": round(sum(r["greedy_transit"] for r in rows), 1),
            "cpsat_transit_total": round(sum(r["cpsat_transit"] for r in rows), 1),
            "cpsat_faster": better,
            "cpsat_slower": worse,
            "same": len(rows) - better - worse,
        },
        "rows": rows,
    }


def show(title: str, r: dict) -> None:
    g, o, p = r["greedy"], r["cpsat"], r["paired"]
    print()
    print("-" * 74)
    print(title)
    print("-" * 74)
    print(f"{'':<22}{'greedy':>14}{'CP-SAT':>14}{'difference':>16}")
    d = o["dispatched"] - g["dispatched"]
    print(f"{'dispatched':<22}{g['dispatched']:>9}/{r['offered']:<4}"
          f"{o['dispatched']:>9}/{r['offered']:<4}{d:>+16}")
    print(f"{'violations introduced':<22}{g['violations']:>14}{o['violations']:>14}")
    print(f"{'solve time (s)':<22}{g['seconds']:>14}{o['seconds']:>14}")
    # plain hyphen: the Windows console encodes this output as cp1252
    gaps = "/".join("-" if x is None else f"{x:.0f}%" for x in o["gaps"])
    print(f"{'CP-SAT stage gaps':<22}{'n/a':>14}{gaps:>14}")
    if o["gaps"] and o["gaps"][0]:
        print(f"  stage 1 did not close: the throughput figure above is a "
              f"floor, not a proven optimum")

    print(f"\npaired over the {p['rakes']} rakes both dispatched:")
    for label, gk, ok in (
            ("total detention (min)", "greedy_detention_total", "cpsat_detention_total"),
            ("median detention (min)", "greedy_detention_median", "cpsat_detention_median"),
            ("total transit (min)", "greedy_transit_total", "cpsat_transit_total")):
        gv, ov = p[gk], p[ok]
        pct = (ov - gv) / gv * 100 if gv else 0.0
        print(f"  {label:<24}{gv:>12.0f}{ov:>14.0f}{pct:>14.1f}%")
    print(f"  {'rakes CP-SAT runs faster':<24}{p['cpsat_faster']:>12}")
    print(f"  {'rakes CP-SAT runs slower':<24}{p['cpsat_slower']:>12}")
    print(f"  {'unchanged':<24}{p['same']:>12}")

    if g["only_here"]:
        print(f"\n  dispatched by greedy only : {len(g['only_here'])}")
    if o["only_here"]:
        print(f"  dispatched by CP-SAT only : {len(o['only_here'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--from", dest="a", type=float, default=0.0)
    ap.add_argument("--to", dest="b", type=float, default=1440.0)
    ap.add_argument("--limit", type=float, default=30.0,
                    help="seconds per CP-SAT stage")
    ap.add_argument("--closure", action="append", default=[],
                    help="LINK:LINE:start:end, repeatable")
    ap.add_argument("--json", help="write the full result here")
    args = ap.parse_args()

    graph = SectionGraph.load()
    book = DemandBook.load()
    day = book.day(args.day) if args.day else book.busiest()
    window = day.between(args.a, args.b)

    print("=" * 74)
    print("GREEDY DISPATCH vs CP-SAT")
    print("=" * 74)
    print(f"\nday            : {day.day}")
    print(f"window         : {args.a:.0f}-{args.b:.0f} min")
    print(f"rakes offered  : {len(window)}")
    print(f"CP-SAT budget  : {args.limit:.0f}s per stage")

    out = {"day": day.day, "window": [args.a, args.b], "scenarios": {}}

    scenarios = [("clean day, nothing withdrawn", [])]
    if args.closure:
        shut = [parse_closure(c) for c in args.closure]
        scenarios.append((
            "under closure: " + ", ".join(c.resource for c in shut), shut))

    for title, closures in scenarios:
        fixed = scheduled_plan(graph, closures=closures)
        cfg = OptimiserConfig(horizon_start_min=args.a, horizon_end_min=args.b,
                              time_limit_s=args.limit, closures=closures)
        r = compare(graph, window, fixed, cfg)
        show(title, r)
        out["scenarios"][title] = r

    print("\n" + "=" * 74)
    print("READING IT")
    print("=" * 74)
    clean = out["scenarios"].get("clean day, nothing withdrawn")
    if clean:
        p, o = clean["paired"], clean["cpsat"]
        drop = ((p["greedy_detention_total"] - p["cpsat_detention_total"])
                / p["greedy_detention_total"] * 100
                if p["greedy_detention_total"] else 0.0)
        print("\nOn a clean day the two dispatch almost the same number of")
        print("rakes. That is a property of the section rather than of the")
        print("schedulers: the busiest line runs near a third occupied, so")
        print("there is room for a poor plan to succeed. What look-ahead")
        print(f"buys here is {drop:.0f}% less detention over the same rakes -")
        print("the trains run, but they stop less to do it. Throughput is")
        print("not the headline on a day with this much slack; detention is.")
        if o["gaps"] and not o["gaps"][0]:
            print("Stage 1 closed, so the dispatched count IS the maximum.")

    for title, s in out["scenarios"].items():
        if "closure" not in title:
            continue
        p, o = s["paired"], s["cpsat"]
        more = o["dispatched"] - s["greedy"]["dispatched"]
        worse = p["cpsat_transit_total"] - p["greedy_transit_total"]
        print("\nUnder closure the picture inverts, and it is worth being")
        print("exact about why. CP-SAT clears " + f"{more} more rakes, and the")
        print(f"rakes they both run take {worse:.0f} more minutes in total")
        print("under CP-SAT than under greedy. That is not a defect: the")
        print("objective is lexicographic, so throughput is bought first and")
        print("transit is only minimised within whatever throughput allows.")
        print("Squeezing extra rakes through a single line costs the rest")
        print("waiting, and the optimiser is instructed to pay that price.")
        if o["gaps"] and o["gaps"][0]:
            print(f"\nBut stage 1 still shows a {o['gaps'][0]:.0f}% gap, so this")
            print("run never proved even the throughput figure. Under")
            print("disruption the solver is at its limit, and the honest")
            print("claim is 'at least this many', not 'the most possible'.")
    print()

    if args.json:
        DERIVED.mkdir(parents=True, exist_ok=True)
        path = Path(args.json)
        path.write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
