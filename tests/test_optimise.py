"""
Optimiser tests.

The decisive property is not that it finds a plan but that the plan survives
independent verification, so most of these solve a small instance and hand the
result to the verifier.

    pytest tests/test_optimise.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.demand import DayDemand, DemandBook  # noqa: E402
from pravah.optimise import Optimiser, OptimiserConfig  # noqa: E402
from pravah.plan import Plan  # noqa: E402
from pravah.section import SectionGraph  # noqa: E402
from pravah.timetable import scheduled_plan  # noqa: E402
from pravah.verify import Verifier, VerifierConfig  # noqa: E402


@pytest.fixture(scope="module")
def g():
    return SectionGraph.load()


@pytest.fixture(scope="module")
def fixed(g):
    return scheduled_plan(g)


@pytest.fixture(scope="module")
def small(g):
    day = DemandBook.load().busiest()
    return DayDemand(day.day, day.between(360, 540).rakes[:12])


@pytest.fixture(scope="module")
def cfg():
    return OptimiserConfig(horizon_start_min=330, horizon_end_min=780,
                           time_limit_s=20)


@pytest.fixture(scope="module")
def solved(g, small, fixed, cfg):
    return Optimiser(g, cfg).solve(small, fixed)


def test_it_dispatches_something(solved, small):
    plan, report = solved
    assert report.ok
    assert 0 < report.dispatched <= len(small)
    assert len(plan.trains) == report.dispatched


def test_every_stage_reports_a_gap(solved):
    _, report = solved
    assert len(report.stages) == 3
    assert [s.name for s in report.stages] == ["1 throughput", "2 transit time",
                                               "3 stop-starts"]
    assert all(s.gap_pct is not None for s in report.stages)


def test_the_plan_passes_independent_verification(g, solved, fixed):
    """The property that matters: the verifier shares no logic with the
    solver, so this is a real check rather than a restatement."""
    plan, _ = solved
    cfg = VerifierConfig.load()
    cfg.assume_unknown_line_fits = True
    report = Verifier(g, cfg).verify(plan)
    assert report.ok, [str(v) for v in report.errors]


def test_freight_does_not_conflict_with_scheduled_services(g, solved, fixed):
    plan, _ = solved
    cfg = VerifierConfig.load()
    cfg.assume_unknown_line_fits = True
    baseline = {str(v) for v in Verifier(g, cfg).verify(fixed).errors}
    combined = Plan(trains=plan.trains + fixed.trains, horizon=plan.horizon)
    introduced = [v for v in Verifier(g, cfg).verify(combined).errors
                  if str(v) not in baseline]
    assert not introduced, [str(v) for v in introduced]


def test_movements_respect_the_technical_minimum(g, solved):
    plan, _ = solved
    for t in plan.trains:
        for mv in t.movements:
            d = g.direction_of(mv.from_station, mv.to_station)
            minimum = g.running_time(mv.link, mv.line, "FREIGHT", d)
            assert mv.duration >= minimum - 1e-6


def test_no_rake_starts_before_it_is_ready(solved, small):
    plan, _ = solved
    ready = {r.id: r.ready_min for r in small}
    for t in plan.trains:
        assert t.entry >= ready[t.id] - 1e-6


def test_holds_are_real_stops_not_rounding(solved):
    """A seventeen-second pause is not a hold; the model must choose
    between no stop at all and a genuine one."""
    plan, _ = solved
    for t in plan.trains:
        for h in t.holds:
            assert h.duration >= 0.5 - 1e-6


def test_a_closure_is_respected(g, small, fixed):
    from pravah.plan import PlannedClosure
    link = "VZM-NML"
    shut = [PlannedClosure(f"{link}:{ln.name}", 0, 2000, "engineering block")
            for ln in g.links[link].lines]
    cfg = OptimiserConfig(horizon_start_min=330, horizon_end_min=780,
                          time_limit_s=20, closures=shut)
    plan, _ = Optimiser(g, cfg).solve(small, fixed)
    used = {mv.resource for t in plan.trains for mv in t.movements}
    assert not (used & {c.resource for c in shut})
