"""
Verifier tests.

A verifier that passes everything is worthless, so most of these build a
deliberately illegal plan and assert that the right violation comes back -
and that nothing else does, which catches over-reporting just as well as
under-reporting.

    pytest tests/test_verify.py -q
"""

from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.plan import Hold, Movement, Plan, PlannedClosure, TrainPlan  # noqa: E402
from pravah.section import Direction, SectionGraph  # noqa: E402
from pravah.verify import Kind, Severity, Verifier, VerifierConfig  # noqa: E402


@pytest.fixture(scope="module")
def g() -> SectionGraph:
    return SectionGraph.load()


def legs(g, train, cls, stations, line, t0, hold_lines=None):
    """A legal run along consecutive stations at the technical minimum."""
    movements, t = [], t0
    for a, b in pairwise(stations):
        link = g.link(a, b).id
        d = g.direction_of(a, b)
        rt = g.running_time(link, line, cls, d)
        movements.append(Movement(train, link, line, a, b, t, t + rt))
        t += rt
    return movements


def plan_of(*trains, closures=(), horizon=(0.0, 1440.0), label="test") -> Plan:
    return Plan(trains=list(trains), closures=list(closures),
                horizon=horizon, label=label)


def freight(g, train="FR1", stations=("KTV", "KPL", "ALM"), line="DN",
            t0=600.0, rake=650.0, holds=()):
    return TrainPlan(id=train, train_class="FREIGHT", direction=Direction.DOWN,
                     rake_length_m=rake,
                     movements=legs(g, train, "FREIGHT", stations, line, t0),
                     holds=list(holds))


def kinds(report) -> set[str]:
    return {v.kind.value for v in report.violations}


def error_kinds(report) -> set[str]:
    return {v.kind.value for v in report.violations if v.severity is Severity.ERROR}


# ---------------------------------------------------------------- baseline
def test_legal_plan_verifies_clean(g):
    rep = Verifier(g).verify(plan_of(freight(g)))
    assert rep.ok, [str(v) for v in rep.violations]
    assert not rep.violations


def test_kpis_are_computed(g):
    rep = Verifier(g).verify(plan_of(freight(g)))
    k = rep.kpis
    assert k.trains_cleared == 1 and k.freight_cleared == 1
    assert k.mean_transit_min > 0
    assert k.busiest_line.startswith("KTV-KPL") or k.busiest_line.startswith("KPL-ALM")


# ------------------------------------------------------- resource conflicts
def test_absolute_block_rejects_two_trains_in_one_section(g):
    """VZM-NML is absolute block: one train at a time, no exceptions."""
    a = freight(g, "FR1", ("VZM", "NML"), t0=600.0)
    b = freight(g, "FR2", ("VZM", "NML"), t0=602.0)   # enters while FR1 inside
    rep = Verifier(g).verify(plan_of(a, b))
    assert Kind.BLOCK_CONFLICT.value in error_kinds(rep)
    assert not rep.ok


def test_automatic_block_allows_following_train_with_headway(g):
    """KTV-KPL is automatic: a second train may be in the section."""
    required = g.headway("KTV-KPL", "DN")
    a = freight(g, "FR1", ("KTV", "KPL"), t0=600.0)
    b = freight(g, "FR2", ("KTV", "KPL"), t0=600.0 + required + 0.5)
    rep = Verifier(g).verify(plan_of(a, b))
    assert rep.ok, [str(v) for v in rep.violations]


def test_headway_breach_is_caught(g):
    a = freight(g, "FR1", ("KTV", "KPL"), t0=600.0)
    b = freight(g, "FR2", ("KTV", "KPL"), t0=600.5)   # 30 s behind
    rep = Verifier(g).verify(plan_of(a, b))
    assert Kind.HEADWAY.value in error_kinds(rep)


def test_opposing_trains_on_one_line_is_a_crossing_conflict(g):
    """The check the problem statement asks for, and the reason single-line
    working matters: two trains meeting head-on on a shared line."""
    down = freight(g, "FR1", ("KTV", "KPL"), line="DN", t0=600.0)
    up = TrainPlan(id="FR2", train_class="FREIGHT", direction=Direction.UP,
                   movements=legs(g, "FR2", "FREIGHT", ("KPL", "KTV"), "DN", 605.0))
    rep = Verifier(g).verify(plan_of(down, up))
    assert Kind.CROSSING_CONFLICT.value in error_kinds(rep)


# ------------------------------------------------------------- per-train
def test_running_faster_than_possible_is_caught(g):
    t = freight(g, "FR1", ("KUK", "VZM"), t0=600.0)
    quick = t.movements[0]
    t.movements = [Movement(quick.train, quick.link, quick.line,
                            quick.from_station, quick.to_station,
                            quick.enter, quick.enter + 2.0)]
    rep = Verifier(g).verify(plan_of(t))
    assert Kind.RUNNING_TIME.value in error_kinds(rep)


def test_skipping_a_block_breaks_continuity(g):
    t = freight(g, "FR1", ("KTV", "KPL", "ALM"), t0=600.0)
    t.movements = [t.movements[0],
                   legs(g, "FR1", "FREIGHT", ("ALM", "KUK"), "DN", 700.0)[0]]
    rep = Verifier(g).verify(plan_of(t))
    assert Kind.CONTINUITY.value in error_kinds(rep)


def test_waiting_without_a_hold_breaks_blocking_discipline(g):
    """A train cannot hover between blocks; it must stand on a line."""
    t = freight(g, "FR1", ("KTV", "KPL", "ALM"), t0=600.0)
    first, second = t.movements
    t.movements = [first,
                   Movement(second.train, second.link, second.line,
                            second.from_station, second.to_station,
                            second.enter + 20, second.exit + 20)]
    rep = Verifier(g).verify(plan_of(t))
    assert Kind.BLOCKING.value in error_kinds(rep)


def test_recording_the_hold_makes_the_same_wait_legal(g):
    t = freight(g, "FR1", ("KTV", "KPL", "ALM"), t0=600.0)
    first, second = t.movements
    t.movements = [first,
                   Movement(second.train, second.link, second.line,
                            second.from_station, second.to_station,
                            second.enter + 20, second.exit + 20)]
    t.holds = [Hold("FR1", "KPL", "1", first.exit, second.enter + 20,
                    "precedence")]
    rep = Verifier(g).verify(plan_of(t))
    assert rep.ok, [str(v) for v in rep.violations]


# ---------------------------------------------------------------- holding
def test_cannot_hold_where_there_is_no_crossing_facility(g):
    """Batuva, Dandu Gopalapuram and Routhpuram are halts with no facility."""
    t = freight(g, "FR1", ("CPP", "BTVA", "SGDM"), t0=600.0)
    first = t.movements[0]
    t.holds = [Hold("FR1", "BTVA", "1", first.exit, first.exit + 10)]
    rep = Verifier(g).verify(plan_of(t))
    assert Kind.HOLD_INFEASIBLE.value in error_kinds(rep)


def test_rake_longer_than_the_loop_is_caught(g):
    t = freight(g, "FR1", ("KTV", "KPL", "ALM"), t0=600.0, rake=1500.0)
    first, second = t.movements
    t.holds = [Hold("FR1", "KPL", "1", first.exit, second.enter)]
    rep = Verifier(g).verify(plan_of(t))
    assert Kind.HOLD_INFEASIBLE.value in error_kinds(rep)


def test_two_trains_cannot_stand_on_one_line(g):
    a = freight(g, "FR1", ("KTV", "KPL", "ALM"), t0=600.0)
    b = freight(g, "FR2", ("KTV", "KPL", "ALM"), t0=640.0)
    a.holds = [Hold("FR1", "KPL", "1", 620.0, 700.0)]
    b.holds = [Hold("FR2", "KPL", "1", 660.0, 720.0)]
    rep = Verifier(g).verify(plan_of(a, b))
    assert Kind.STATION_CAPACITY.value in error_kinds(rep)


def test_long_hold_is_a_warning_not_an_error(g):
    t = freight(g, "FR1", ("KTV", "KPL", "ALM"), t0=600.0)
    first, second = t.movements
    t.movements = [first,
                   Movement(second.train, second.link, second.line,
                            second.from_station, second.to_station,
                            second.enter + 120, second.exit + 120)]
    t.holds = [Hold("FR1", "KPL", "1", first.exit, second.enter + 120)]
    rep = Verifier(g).verify(plan_of(t))
    assert Kind.LONG_HOLD.value in kinds(rep)
    assert rep.ok, "a long hold is legal, just notable"


# --------------------------------------------------------------- closures
def test_using_a_closed_line_is_caught(g):
    t = freight(g, "FR1", ("KTV", "KPL"), t0=600.0)
    c = PlannedClosure("KTV-KPL:DN", 540.0, 660.0, "engineering block")
    rep = Verifier(g).verify(plan_of(t, closures=[c]))
    assert Kind.CLOSURE.value in error_kinds(rep)


def test_the_same_movement_outside_the_window_is_legal(g):
    t = freight(g, "FR1", ("KTV", "KPL"), t0=700.0)
    c = PlannedClosure("KTV-KPL:DN", 540.0, 660.0, "engineering block")
    rep = Verifier(g).verify(plan_of(t, closures=[c]))
    assert rep.ok, [str(v) for v in rep.violations]


# ------------------------------------------------------- service integrity
def test_moving_a_scheduled_service_is_caught_at_zero_tolerance(g):
    t = freight(g, "EXP1", ("KTV", "KPL"), t0=610.0)
    t.train_class = "SUF"
    t.scheduled = {"KTV": 600.0}
    rep = Verifier(g, VerifierConfig(service_tolerance_min=0)).verify(plan_of(t))
    assert Kind.SERVICE_INTEGRITY.value in error_kinds(rep)


def test_tolerance_is_configurable(g):
    t = freight(g, "EXP1", ("KTV", "KPL"), t0=610.0)
    t.train_class = "SUF"
    t.scheduled = {"KTV": 600.0}
    strict = Verifier(g, VerifierConfig(service_tolerance_min=0)).verify(plan_of(t))
    loose = Verifier(g, VerifierConfig(service_tolerance_min=15)).verify(plan_of(t))
    assert not strict.ok
    assert loose.ok, "15 min tolerance should admit a 10 min shift"


def test_severity_can_be_demoted(g):
    a = freight(g, "FR1", ("KTV", "KPL"), t0=600.0)
    b = freight(g, "FR2", ("KTV", "KPL"), t0=600.5)
    cfg = VerifierConfig(severity={"headway": "warning"})
    rep = Verifier(g, cfg).verify(plan_of(a, b))
    assert Kind.HEADWAY.value in kinds(rep)
    assert rep.ok, "demoted to a warning, the plan is still valid"


def test_severity_can_be_ignored(g):
    a = freight(g, "FR1", ("KTV", "KPL"), t0=600.0)
    b = freight(g, "FR2", ("KTV", "KPL"), t0=600.5)
    cfg = VerifierConfig(severity={"headway": "ignore"})
    rep = Verifier(g, cfg).verify(plan_of(a, b))
    assert Kind.HEADWAY.value not in kinds(rep)


def test_config_loads_from_toml():
    cfg = VerifierConfig.load()
    assert cfg.service_tolerance_min == 0
    assert cfg.punctuality_threshold_min == 5
    assert cfg.severity_of(Kind.LONG_HOLD) is Severity.WARNING
    assert cfg.severity_of(Kind.BLOCK_CONFLICT) is Severity.ERROR


# --------------------------------------- precedence vs commercial stop
def test_commercial_stop_is_allowed_at_a_halt(g):
    """Routhpuram has no crossing facility, but trains stop there daily.
    Stopping to pick up passengers is not the same as being held to be
    passed — only the latter needs a loop."""
    from pravah.plan import SCHEDULED_STOP
    t = freight(g, "P1", ("NWP", "RMZ", "PUN"), t0=600.0)
    t.train_class = "PAS"
    first, second = t.movements
    t.movements = [first,
                   Movement(second.train, second.link, second.line,
                            second.from_station, second.to_station,
                            second.enter + 2, second.exit + 2)]
    t.holds = [Hold("P1", "RMZ", "1", first.exit, second.enter + 2,
                    "scheduled stop", SCHEDULED_STOP)]
    rep = Verifier(g, VerifierConfig(assume_unknown_line_fits=True)).verify(plan_of(t))
    assert rep.ok, [str(v) for v in rep.violations]


def test_being_held_to_be_passed_still_needs_a_loop(g):
    """The same stop, for precedence, at the same halt — not permitted."""
    from pravah.plan import PRECEDENCE
    t = freight(g, "FR1", ("NWP", "RMZ", "PUN"), t0=600.0)
    first, second = t.movements
    t.movements = [first,
                   Movement(second.train, second.link, second.line,
                            second.from_station, second.to_station,
                            second.enter + 20, second.exit + 20)]
    t.holds = [Hold("FR1", "RMZ", "1", first.exit, second.enter + 20,
                    "precedence", PRECEDENCE)]
    rep = Verifier(g, VerifierConfig(assume_unknown_line_fits=True)).verify(plan_of(t))
    assert Kind.HOLD_INFEASIBLE.value in error_kinds(rep)


# ------------------------------------------------------ freight demand
def test_demand_loads_and_converts_to_plans():
    from pravah.demand import DemandBook
    book = DemandBook.load()
    assert len(book.days) == 27
    day = book.busiest()
    assert len(day) > 50
    plans = day.to_train_plans()
    assert len(plans) == len(day)
    assert all(p.train_class == "FREIGHT" for p in plans)
    assert all(p.rake_length_m > 400 for p in plans)


def test_demand_filters_are_consistent(g):
    from pravah.demand import DemandBook
    day = DemandBook.load().busiest()
    dn, up = day.direction("DN"), day.direction("UP")
    assert len(dn) + len(up) == len(day)
    morning = day.between(360, 720)
    assert all(360 <= r.ready_min <= 720 for r in morning)
    assert len(day.end_to_end()) <= len(day)


def test_every_rake_can_be_held_somewhere(g):
    """A rake the section cannot hold anywhere is unschedulable — the
    optimiser would have no legal way to let a passenger train pass it."""
    from pravah.demand import DemandBook
    stranded = []
    for r in DemandBook.load().busiest():
        if not any(s.holding_lines(r.length_m) for s in g.stations):
            stranded.append((r.id, r.length_m))
    assert not stranded, f"{len(stranded)} rake(s) cannot be held anywhere"
