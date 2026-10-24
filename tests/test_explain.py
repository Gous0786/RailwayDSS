"""
Explanation tests.

The mechanism is a counterfactual: remove a hold, pull the later legs
earlier, re-verify, and whatever breaks is the reason. These check that the
counterfactual actually fires and names a real train, rather than producing a
plausible-sounding sentence with nothing behind it.

    pytest tests/test_explain.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.demand import DayDemand, DemandBook  # noqa: E402
from pravah.explain import explain  # noqa: E402
from pravah.optimise import Optimiser, OptimiserConfig  # noqa: E402
from pravah.section import SectionGraph  # noqa: E402
from pravah.timetable import scheduled_plan  # noqa: E402
from pravah.verify import VerifierConfig  # noqa: E402


@pytest.fixture(scope="module")
def g():
    return SectionGraph.load()


@pytest.fixture(scope="module")
def pieces(g):
    fixed = scheduled_plan(g)
    day = DemandBook.load().busiest()
    window = day.between(360, 720)
    cfg = OptimiserConfig(horizon_start_min=330, horizon_end_min=900,
                          time_limit_s=15)
    plan, _ = Optimiser(g, cfg).solve(window, fixed)
    vc = VerifierConfig.load()
    vc.assume_unknown_line_fits = True
    return plan, fixed, window, vc


@pytest.fixture(scope="module")
def reasons(g, pieces):
    plan, fixed, window, vc = pieces
    return explain(g, plan, fixed, vc, demand=window)


def test_every_offered_rake_is_accounted_for(reasons, pieces):
    _, _, window, _ = pieces
    assert len(reasons) == len(window)


def test_dispatched_rakes_carry_their_journey(reasons):
    ran = [e for e in reasons.values() if e.dispatched_min is not None]
    assert ran
    for e in ran:
        assert e.legs
        assert e.cleared_min > e.dispatched_min
        assert e.transit_min > 0


def test_undispatched_rakes_say_so_rather_than_guessing(reasons):
    """There is no counterfactual to run when there is no plan, so the
    honest answer is that it needs a re-solve."""
    idle = [e for e in reasons.values() if e.dispatched_min is None]
    for e in idle:
        assert not e.holds
        assert "re-solve" in e.note


def test_each_hold_is_explained(reasons):
    held = [e for e in reasons.values() if e.holds]
    assert held, "no rake was held; nothing to explain"
    for e in held:
        for h in e.holds:
            assert h.duration_min > 0
            assert h.detail
            assert h.station


def test_the_counterfactual_names_a_real_train(reasons, pieces):
    """A reason with no blocker behind it would be a sentence, not an
    explanation. At least one hold must name a train that exists."""
    plan, fixed, _, _ = pieces
    known = {t.id for t in plan.trains} | {t.id for t in fixed.trains}
    named = [b for e in reasons.values() for h in e.holds for b in h.blockers]
    assert named, "no hold produced a blocker"
    for b in named:
        assert b.train in known
        assert b.detail
        assert b.kind


def test_blockers_are_usually_higher_priority(reasons):
    """Freight is held for trains that outrank it. Not a hard rule - a rake
    can also be held behind another freight - so this checks the tendency."""
    pairs = [(b.priority, 12) for e in reasons.values()
             for h in e.holds for b in h.blockers]
    assert pairs
    outranking = sum(1 for p, f in pairs if p < f)
    assert outranking >= len(pairs) / 2


def test_detention_matches_the_holds(reasons):
    for e in reasons.values():
        if e.dispatched_min is None:
            continue
        total = sum(h.duration_min for h in e.holds)
        assert abs(total - e.detention_min) < 0.1


def test_it_serialises_for_the_api(reasons):
    for e in reasons.values():
        d = e.to_dict()
        assert set(d) >= {"train_id", "legs", "holds", "detention_min"}
        for h in d["holds"]:
            assert "detail" in h and "blockers" in h
