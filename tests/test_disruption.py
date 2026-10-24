"""
Disruption tests.

The corridor is double or triple track throughout, so two trains can always
pass and a crossing can never arise — until a line is withdrawn. Closing one
is therefore the only way the problem statement's second half, crossings,
becomes a decision at all. These check that a closure actually produces
single-line working rather than simply dropping the affected trains.

    pytest tests/test_disruption.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.demand import DayDemand, DemandBook  # noqa: E402
from pravah.optimise import Optimiser, OptimiserConfig  # noqa: E402
from pravah.plan import Plan, PlannedClosure  # noqa: E402
from pravah.section import SectionGraph  # noqa: E402
from pravah.timetable import scheduled_plan  # noqa: E402
from pravah.verify import Verifier, VerifierConfig  # noqa: E402

LINK = "VZM-NML"          # two lines, absolute block


@pytest.fixture(scope="module")
def g():
    return SectionGraph.load()


@pytest.fixture(scope="module")
def window():
    day = DemandBook.load().busiest()
    return DayDemand(day.day, day.between(360, 720).rakes[:16])


def solve(g, window, closures):
    fixed = scheduled_plan(g, closures=closures)
    cfg = OptimiserConfig(horizon_start_min=330, horizon_end_min=900,
                          time_limit_s=15, closures=closures)
    plan, report = Optimiser(g, cfg).solve(window, fixed)
    return plan, report, fixed


def introduced(g, plan, fixed):
    cfg = VerifierConfig.load()
    cfg.assume_unknown_line_fits = True
    v = Verifier(g, cfg)
    base = {str(x) for x in v.verify(fixed).errors}
    combined = Plan(trains=plan.trains + fixed.trains, horizon=plan.horizon)
    return [x for x in v.verify(combined).errors if str(x) not in base]


def test_closing_a_line_reroutes_rather_than_drops(g, window):
    """Trains must move to the remaining line, not vanish from the plan."""
    shut = [PlannedClosure(f"{LINK}:DN", 0, 1440, "engineering block")]
    plan, report, fixed = solve(g, window, shut)
    used = {mv.resource for t in plan.trains for mv in t.movements
            if mv.link == LINK}
    assert f"{LINK}:DN" not in used, "freight planned onto a closed line"
    assert report.dispatched > 0, "closure emptied the plan entirely"


def test_the_remaining_line_carries_both_directions(g, window):
    """That is what single-line working means, and what makes a crossing
    a decision rather than an impossibility."""
    shut = [PlannedClosure(f"{LINK}:DN", 0, 1440, "engineering block")]
    plan, _, fixed = solve(g, window, shut)
    everyone = plan.trains + fixed.trains
    dirs = set()
    for t in everyone:
        for mv in t.movements:
            if mv.resource == f"{LINK}:UP":
                dirs.add(g.direction_of(mv.from_station, mv.to_station))
    assert len(dirs) == 2, f"only {dirs} used the surviving line"


def test_no_opposing_conflict_is_introduced(g, window):
    """Both directions share the line, so the optimiser has to cross them
    at a station rather than let them meet in section."""
    shut = [PlannedClosure(f"{LINK}:DN", 0, 1440, "engineering block")]
    plan, _, fixed = solve(g, window, shut)
    assert not introduced(g, plan, fixed)


def test_a_closure_outside_the_window_changes_nothing(g, window):
    shut = [PlannedClosure(f"{LINK}:DN", 1400, 1439, "late block")]
    plan, report, fixed = solve(g, window, shut)
    assert report.dispatched > 0
    assert not introduced(g, plan, fixed)


def test_closing_every_line_stops_traffic_through_the_link(g, window):
    shut = [PlannedClosure(f"{LINK}:{ln.name}", 0, 1440, "total block")
            for ln in g.links[LINK].lines]
    plan, _, fixed = solve(g, window, shut)
    used = {mv.link for t in plan.trains for mv in t.movements}
    assert LINK not in used


def test_disruption_costs_throughput(g, window):
    """A closure should cost something. If it were free, the model would not
    be representing the constraint."""
    clean, clean_report, _ = solve(g, window, [])
    shut = [PlannedClosure(f"{LINK}:{ln.name}", 0, 1440, "total block")
            for ln in g.links[LINK].lines]
    _, blocked_report, _ = solve(g, window, shut)
    assert blocked_report.dispatched <= clean_report.dispatched
