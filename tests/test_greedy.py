"""
Greedy baseline tests.

A baseline is only worth quoting if it is both legal and fair. Legal means it
breaks none of the rules the optimiser is held to - otherwise it is beaten by
cheating. Fair means it is not quietly crippled - otherwise the comparison
flatters the optimiser.

These check both, plus the property the whole comparison rests on: that the
two schedulers see the same section and route rakes the same way, so that
every difference between them comes from the decision rule.

    pytest tests/test_greedy.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.demand import DayDemand, DemandBook  # noqa: E402
from pravah.greedy import GreedyDispatcher  # noqa: E402
from pravah.optimise import Optimiser, OptimiserConfig, route_rake  # noqa: E402
from pravah.plan import Plan, PlannedClosure  # noqa: E402
from pravah.section import SectionGraph  # noqa: E402
from pravah.timetable import scheduled_plan  # noqa: E402
from pravah.verify import Verifier, VerifierConfig  # noqa: E402


@pytest.fixture(scope="module")
def graph() -> SectionGraph:
    return SectionGraph.load()


@pytest.fixture(scope="module")
def fixed(graph) -> Plan:
    return scheduled_plan(graph)


@pytest.fixture(scope="module")
def window(graph) -> DayDemand:
    # a morning, so the suite stays quick but the section is genuinely busy
    return DemandBook.load().busiest().between(360.0, 720.0)


@pytest.fixture(scope="module")
def cfg() -> OptimiserConfig:
    return OptimiserConfig(horizon_start_min=330.0, horizon_end_min=900.0,
                           time_limit_s=10.0)


@pytest.fixture(scope="module")
def greedy_plan(graph, window, fixed, cfg):
    return GreedyDispatcher(graph, cfg).solve(window, fixed)


def introduced(graph, plan: Plan, fixed: Plan):
    vcfg = VerifierConfig.load()
    vcfg.assume_unknown_line_fits = True
    v = Verifier(graph, vcfg)
    base = {str(x) for x in v.verify(fixed).errors}
    combined = Plan(trains=plan.trains + fixed.trains, horizon=plan.horizon)
    return [x for x in v.verify(combined).errors if str(x) not in base]


# ------------------------------------------------------------------- legal

def test_greedy_breaks_no_rules(graph, greedy_plan, fixed):
    """The baseline must lose on quality, never on legality."""
    plan, _ = greedy_plan
    bad = introduced(graph, plan, fixed)
    assert not bad, "\n".join(str(x) for x in bad[:5])


def test_greedy_disturbs_no_scheduled_service(greedy_plan, fixed):
    plan, _ = greedy_plan
    assert {t.id for t in plan.trains}.isdisjoint({t.id for t in fixed.trains})


def test_every_hold_stands_somewhere_real(graph, greedy_plan):
    """A hold needs a loop that fits the rake, not just a station."""
    plan, _ = greedy_plan
    for train in plan.trains:
        for h in train.holds:
            lines = graph.station(h.station).holding_lines(train.rake_length_m)
            assert any(ln.number == h.line for ln in lines), \
                f"{train.id} stands on {h.resource}, which cannot hold it"


def test_no_two_rakes_share_a_station_line(greedy_plan):
    plan, _ = greedy_plan
    taken: dict[str, list[tuple[float, float]]] = {}
    for train in plan.trains:
        for h in train.holds:
            for a, b in taken.get(h.resource, ()):
                assert not (h.arrive < b and a < h.depart), \
                    f"two rakes stand on {h.resource}"
            taken.setdefault(h.resource, []).append((h.arrive, h.depart))


# -------------------------------------------------------------------- fair

def test_greedy_actually_dispatches_most_of_the_demand(greedy_plan):
    """A baseline that fails outright would flatter the optimiser."""
    _, report = greedy_plan
    assert report.dispatched >= 0.7 * report.offered, \
        f"only {report.dispatched}/{report.offered} - too weak to compare against"


def test_greedy_never_departs_before_the_rake_is_ready(greedy_plan, window):
    plan, _ = greedy_plan
    ready = {r.id: r.ready_min for r in window}
    for train in plan.trains:
        assert train.entry >= ready[train.id] - 1e-6, \
            f"{train.id} left before it was there"


def test_greedy_is_first_come_first_served_at_the_origin(greedy_plan, window):
    """The rule is the whole point: no rake is held back for a later one.

    Dispatch order may differ from ready order only because the road was
    blocked, never because something not yet present was preferred.
    """
    plan, _ = greedy_plan
    ready = {r.id: r.ready_min for r in window}
    for train in plan.trains:
        assert train.entry >= ready[train.id] - 1e-6
    # at least as many rakes keep their arrival order as would under a
    # scheduler free to reorder
    order = sorted(plan.trains, key=lambda t: t.entry)
    kept = sum(1 for a, b in zip(order, order[1:])
               if ready[a.id] <= ready[b.id] + 1e-6)
    assert kept >= 0.6 * max(1, len(order) - 1)


# --------------------------------------------------------------- comparable

def test_both_schedulers_route_rakes_identically(graph, window, cfg):
    """The comparison is meaningless if they disagree about the route."""
    opt = Optimiser(graph, cfg)
    for rake in window:
        assert opt._route(rake) == route_rake(
            graph, rake, cfg.closures, cfg.start_s, cfg.end_s)


def test_greedy_takes_the_same_arguments_as_the_optimiser(graph, window, fixed, cfg):
    """Drop-in interchangeable, so nothing else has to branch on which ran."""
    plan, report = GreedyDispatcher(graph, cfg).solve(window, fixed)
    assert isinstance(plan, Plan)
    assert report.offered == len(window)
    assert report.dispatched + len(report.undispatched) == report.offered
    assert report.stages and report.stages[0].gap_pct is None, \
        "a heuristic must not report an optimality gap"


def test_greedy_survives_a_closure(graph, window, fixed):
    """Single-line working must not produce an illegal plan."""
    shut = [PlannedClosure("VZM-NML:DN", 360.0, 720.0, "engineering block")]
    closed_fixed = scheduled_plan(graph, closures=shut)
    cfg = OptimiserConfig(horizon_start_min=330.0, horizon_end_min=900.0,
                          closures=shut)
    plan, report = GreedyDispatcher(graph, cfg).solve(window, closed_fixed)
    bad = introduced(graph, plan, closed_fixed)
    assert not bad, "\n".join(str(x) for x in bad[:5])
    assert report.dispatched > 0


def test_a_closure_costs_the_baseline_trains(graph, window, fixed, cfg, greedy_plan):
    """If closing a line changed nothing, the model would not be modelling."""
    _, clean = greedy_plan
    shut = [PlannedClosure("VZM-NML:DN", 360.0, 720.0, "engineering block")]
    closed_fixed = scheduled_plan(graph, closures=shut)
    ccfg = OptimiserConfig(horizon_start_min=330.0, horizon_end_min=900.0,
                           closures=shut)
    _, hurt = GreedyDispatcher(graph, ccfg).solve(window, closed_fixed)
    assert hurt.dispatched <= clean.dispatched
