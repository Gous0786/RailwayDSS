"""
Giving every standing train a road of its own.

Three defects lived here, all of them invisible on a clean day and
intermittent under closure, which is the worst combination: a run would come
back clean and the next would not, and the plan looked the same either way.

  - the capacity constraint reserved the whole station for freight and did
    not notice scheduled services standing in it
  - the road for each hold was chosen one rake at a time, so a short stop
    could fragment a road and leave a long hold nowhere to go while a road
    stood empty
  - a dwell constrained to exactly the minimum failed an exact float compare
    about half the time

These test the assignment directly rather than through a solve, because a
solve under a closure reproduces the problem in roughly one run of two and a
test that fails half the time is not a test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pravah.optimise import Optimiser, OptimiserConfig, SolveReport  # noqa: E402
from pravah.plan import Hold, Movement, Plan, TrainPlan              # noqa: E402
from pravah.section import Direction, SectionGraph                   # noqa: E402
from pravah.verify import Verifier, VerifierConfig                   # noqa: E402


@pytest.fixture(scope="module")
def g() -> SectionGraph:
    return SectionGraph.load()


def _opt(g, busy: dict | None = None) -> Optimiser:
    o = Optimiser(g, OptimiserConfig())
    o._station_busy = dict(busy or {})
    return o


def _items(rows):
    """(arrive, depart, rake_id, station, length_m), as _extract builds them."""
    return [(a, b, rid, at, m) for a, b, rid, at, m in rows]


def test_a_long_hold_survives_two_short_scheduled_stops(g):
    """The case that was failing at Nellimarla.

    A rake wants three hours. One road is taken by another rake, and the
    other two each carry a one-minute passenger stop, placed so that no road
    is free for the whole window. There IS a valid assignment - it needs the
    other rake moved - and first-fit cannot find it in any order.
    """
    o = _opt(g, {"NML:2": [(438.0, 439.0)], "NML:1": [(582.0, 583.0)]})
    report = SolveReport()
    out = o._assign_lines("NML", _items([
        (411.0, 584.0, "long", "NML", 600.0),
        (308.0, 462.0, "other", "NML", 600.0),
    ]), report)

    assert report.crowded_holds == []
    lines = {rid: line for rid, line, _a, _b in out}
    assert lines["long"] != lines["other"]
    # the long hold has to take the only road with no scheduled stop in it
    assert lines["long"] == "3"


def test_no_two_rakes_are_put_on_one_road(g):
    o = _opt(g)
    report = SolveReport()
    out = o._assign_lines("NML", _items([
        (100.0, 200.0, "a", "NML", 600.0),
        (150.0, 250.0, "b", "NML", 600.0),
        (180.0, 260.0, "c", "NML", 600.0),
    ]), report)
    assert report.crowded_holds == []
    assert len({line for _r, line, _a, _b in out}) == 3


def test_a_station_that_really_is_full_is_reported(g):
    """Four overlapping holds at a three-road station cannot be placed.

    The point is that it SAYS so. Placing the fourth on an occupied road and
    leaving the verifier to find it is what the fallback used to do.
    """
    o = _opt(g)
    report = SolveReport()
    o._assign_lines("NML", _items([
        (100.0, 200.0, "a", "NML", 600.0),
        (110.0, 210.0, "b", "NML", 600.0),
        (120.0, 220.0, "c", "NML", 600.0),
        (130.0, 230.0, "d", "NML", 600.0),
    ]), report)
    assert len(report.crowded_holds) == 1


def test_a_scheduled_service_is_not_overwritten(g):
    """A road occupied by a passenger train is not free, whatever else."""
    o = _opt(g, {"NML:3": [(100.0, 300.0)],
                 "NML:2": [(100.0, 300.0)],
                 "NML:1": [(100.0, 300.0)]})
    report = SolveReport()
    o._assign_lines("NML", _items([(150.0, 250.0, "a", "NML", 600.0)]), report)
    assert report.crowded_holds == ["a@NML"]


def test_the_search_is_bounded(g):
    """A pathological station degrades into a report, not a hang."""
    o = _opt(g)
    report = SolveReport()
    rows = [(100.0 + i * 0.01, 300.0, f"r{i}", "NML", 600.0) for i in range(40)]
    out = o._assign_lines("NML", _items(rows), report)
    assert len(out) == 40
    assert report.crowded_holds          # it cannot succeed, and says so


def test_holds_that_do_not_overlap_may_share_a_road(g):
    o = _opt(g)
    report = SolveReport()
    out = o._assign_lines("NML", _items([
        (100.0, 200.0, "a", "NML", 600.0),
        (200.0, 300.0, "b", "NML", 600.0),
    ]), report)
    assert report.crowded_holds == []
    assert len({line for _r, line, _a, _b in out}) == 1


def _standing_at_nml(g, dwell: float) -> TrainPlan:
    """A rake that has just arrived at NML and stands there for `dwell`.

    The movement is not decoration. The verifier returns from a train with
    no movements before it looks at the holds, so a plan built from holds
    alone passes every dwell test ever written against it.
    """
    arrive = 100.0
    run = g.running_time("VZM-NML", "DN", "FREIGHT", Direction.DOWN)
    return TrainPlan(
        id="x", train_class="FREIGHT", direction=Direction.DOWN,
        movements=[Movement("x", "VZM-NML", "DN", "VZM", "NML",
                            arrive - run, arrive)],
        holds=[Hold("x", "NML", "1", arrive, arrive + dwell)])


def test_a_dwell_of_exactly_the_minimum_is_legal(g):
    """Hold times are solver seconds over 60, so a dwell pinned to the
    minimum lands either side of it by about 5e-13. An exact compare
    reported a violation on roughly half of them."""
    cfg = VerifierConfig.load()
    t = _standing_at_nml(g, cfg.min_dwell_min * (1 - 1e-12))
    report = Verifier(g, cfg).verify(Plan(trains=[t]))
    assert "hold_infeasible" not in [v.kind.value for v in report.errors]


def test_a_dwell_genuinely_below_the_minimum_still_fails(g):
    cfg = VerifierConfig.load()
    t = _standing_at_nml(g, cfg.min_dwell_min * 0.5)
    report = Verifier(g, cfg).verify(Plan(trains=[t]))
    assert "hold_infeasible" in [v.kind.value for v in report.errors]
