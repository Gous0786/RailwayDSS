"""
What the learned traversal times are worth in trains.

A 30% lower pinball loss is a claim about a held-out week of goods records.
It is not a claim about the railway. This runs the same demand through the
same optimiser twice - once on the constant speed model, once on the learned
table - and reports what changed in the plan.

READ THE TRANSIT FIGURES CAREFULLY

The two runs do not share a definition of how long a block takes, so a
shorter total transit under the learned model is not an improvement: it is
mostly the model saying the same journey takes less time. That number is
reported because leaving it out would be worse, but it is not evidence.

The figures that ARE comparable:

  dispatched   how many rakes got through. The learned times are lower than
               the constant on most blocks, so the section should hold more.
               If it does not, the binding constraint was never the running
               time and the estimator has bought nothing operationally.

  detention    minutes spent standing. Waiting is waiting under either model.

  violations   the plan still has to pass the verifier, which is now asking
               the estimator the same question the planner asked it.

Usage:
    python analysis/estimator_impact.py
    python analysis/estimator_impact.py --limit 60
    python analysis/estimator_impact.py --closure VZM-NML:DN:480:720
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pravah.demand import DemandBook                     # noqa: E402
from pravah.greedy import GreedyDispatcher               # noqa: E402
from pravah.optimise import Optimiser, OptimiserConfig   # noqa: E402
from pravah.section import SectionGraph                  # noqa: E402
from pravah.timetable import scheduled_plan              # noqa: E402

from compare_baseline import check, parse_closure        # noqa: E402

DERIVED = ROOT / "data" / "derived"


def graphs() -> dict[str, SectionGraph]:
    """The same corridor under each model of how long a block takes."""
    learned = SectionGraph.load()
    if learned.estimator is None:
        raise SystemExit("no trained estimator; run analysis/train_traversal.py")
    constant = SectionGraph(learned.stations, learned.links, learned.psr,
                            estimator=None)
    return {"constant": constant, "learned": learned}


def run(graph: SectionGraph, window, cfg, closures) -> dict:
    fixed = scheduled_plan(graph, closures=closures)
    out = {}
    for name, solver in (("greedy", GreedyDispatcher), ("cpsat", Optimiser)):
        plan, report = solver(graph, cfg).solve(window, fixed)
        bad, _ = check(graph, plan, fixed, cfg.service_tolerance_min)
        out[name] = {
            "dispatched": report.dispatched,
            "offered": report.offered,
            "violations": len(bad),
            "detention": round(sum(t.detention for t in plan.trains), 1),
            "transit": round(sum(t.transit or 0.0 for t in plan.trains), 1),
            "seconds": round(report.seconds, 2),
            "ids": sorted(t.id for t in plan.trains),
            "gaps": [s.gap_pct for s in report.stages],
        }
    return out


def show(title: str, res: dict) -> None:
    print()
    print("-" * 74)
    print(title)
    print("-" * 74)
    for solver in ("greedy", "cpsat"):
        c, l = res["constant"][solver], res["learned"][solver]
        print(f"\n{solver}")
        print(f"{'':<22}{'constant':>12}{'learned':>12}{'change':>12}")
        for label, key in (("dispatched", "dispatched"),
                           ("detention (min)", "detention"),
                           ("transit (min)", "transit"),
                           ("violations", "violations")):
            d = l[key] - c[key]
            print(f"  {label:<20}{c[key]:>12.0f}{l[key]:>12.0f}{d:>+12.0f}")
        gained = sorted(set(l["ids"]) - set(c["ids"]))
        lost = sorted(set(c["ids"]) - set(l["ids"]))
        if gained:
            print(f"  got through only on the learned times : {len(gained)}")
        if lost:
            print(f"  lost on the learned times             : {len(lost)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--from", dest="a", type=float, default=0.0)
    ap.add_argument("--to", dest="b", type=float, default=1440.0)
    ap.add_argument("--limit", type=float, default=30.0)
    ap.add_argument("--closure", action="append", default=[])
    ap.add_argument("--json", help="write the full result here")
    args = ap.parse_args()

    gs = graphs()
    book = DemandBook.load()
    day = book.day(args.day) if args.day else book.busiest()
    window = day.between(args.a, args.b)

    print("=" * 74)
    print("WHAT THE LEARNED TRAVERSAL TIMES ARE WORTH")
    print("=" * 74)
    print(f"\nday           : {day.day}")
    print(f"rakes offered : {len(window)}")
    print(f"estimator     : {gs['learned'].estimator}")

    scenarios = [("clean day, nothing withdrawn", [])]
    if args.closure:
        shut = [parse_closure(c) for c in args.closure]
        scenarios.append(("under closure: "
                          + ", ".join(c.resource for c in shut), shut))

    out = {"day": day.day, "scenarios": {}}
    for title, closures in scenarios:
        cfg = OptimiserConfig(horizon_start_min=args.a, horizon_end_min=args.b,
                              time_limit_s=args.limit, closures=closures)
        res = {k: run(g, window, cfg, closures) for k, g in gs.items()}
        show(title, res)
        out["scenarios"][title] = res

    print()
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
