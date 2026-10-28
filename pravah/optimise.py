"""
Freight path optimisation with CP-SAT.

Decides which rakes run, when each departs, and where each is held, so that
the section clears as many as possible without disturbing the scheduled
services. This is the project's contribution; everything else exists to feed
it or to check it.

Modelled as a blocking job-shop. Each traversal of a running line is an
interval; a rake's presence is a literal, which is what makes throughput a
decision rather than a parameter. Scheduled services enter as fixed intervals
on the same resources, so freight has to thread around them.

The signalling distinction matters and is easy to get wrong. Absolute block
admits one train to a section at a time, so NoOverlap applies to the whole
occupation. Automatic block legitimately holds several trains in different
signal sections, so NoOverlap applies instead to headway-length stubs at the
entry: separation is enforced without forbidding co-occupancy.

The objective is lexicographic, solved in stages because CP-SAT minimises one
expression at a time:

    1  maximise  trains cleared        throughput
    2  minimise  total transit time    travel time
    3  minimise  number of holds       stop-starts, energy, wear

Each stage reports its own gap, which is the certified-quality claim.

    from pravah.optimise import Optimiser, OptimiserConfig

    plan, report = Optimiser(graph).solve(demand, fixed=scheduled_plan(graph))
    report.summary()
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from ortools.sat.python import cp_model

from .demand import DayDemand, Rake
from .plan import PRECEDENCE, Hold, Movement, Plan, PlannedClosure, TrainPlan
from .section import (ABSOLUTE_CLEARANCE_MIN, Direction, SectionGraph,
                      Signalling)

SEC = 60.0          # the model works in integer seconds


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse overlapping spans into their union.

    Scheduled services are constants. If a closure has pushed two of them
    onto one line so that they overlap, constraining them against each other
    makes the model unsatisfiable rather than merely tight. Merging means
    freight still has to avoid the whole occupied period, while the conflict
    between the two fixed services - which the optimiser cannot resolve and
    did not cause - is left to the verifier to report.
    """
    if not spans:
        return []
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def route_rake(graph: SectionGraph, rake: Rake,
               closures: tuple[PlannedClosure, ...] | list[PlannedClosure] = (),
               start_s: int = 0, end_s: int = 86400
               ) -> list[tuple[str, str, str, str]]:
    """(link, line, from, to) for each leg, or [] if not routable.

    A train normally keeps to its own direction's line. When that line is
    closed it takes whatever remains, which is single-line working: the
    remaining line then carries both directions and opposing movements have
    to be crossed at a station. That is how a closure produces a crossing
    decision on a corridor that is double track throughout.

    Module level, not a method, because the greedy baseline has to route
    identically. A comparison between two schedulers means nothing if they
    disagree about which line a rake would even take, so there is one
    definition rather than two that can drift apart.
    """
    def shut(resource: str) -> bool:
        return any(c.resource == resource
                   and c.overlaps(start_s / SEC, end_s / SEC)
                   for c in closures)

    order = [s.code for s in graph.stations]
    try:
        i, j = order.index(rake.entry_station), order.index(rake.exit_station)
    except ValueError:
        return []
    if i == j:
        return []
    step = 1 if j > i else -1
    chain = []
    for k in range(i, j, step):
        a, b = order[k], order[k + step]
        link = graph.link(a, b).id
        d = graph.direction_of(a, b)
        open_lines = [ln for ln in graph.links[link].lines
                      if not shut(f"{link}:{ln.name}")]
        if not open_lines:
            return []                       # the link is shut entirely
        own = [ln for ln in open_lines
               if ln.nominal_direction in (d, Direction.BOTH)]
        chain.append((link, (own or open_lines)[0].name, a, b))
    return chain


@dataclass
class OptimiserConfig:
    """Everything tunable about the search."""

    # Planning window, minutes from midnight. Freight may not enter before
    # the start nor still be running at the end.
    horizon_start_min: float = 0.0
    horizon_end_min: float = 1440.0

    # Seconds per lexicographic stage. Stage 1 usually finishes instantly;
    # stage 2 does the real work.
    time_limit_s: float = 30.0
    workers: int = 8

    # How far a scheduled service may move. 0 keeps the published timetable
    # untouched, which is how the section is worked today.
    service_tolerance_min: float = 0.0

    # Allow a rake onto a line other than its direction's own. Off by default:
    # trains keep to their own line in normal working, and switching it on
    # multiplies the model. Needed for three-line links and under closures.
    enable_line_choice: bool = False

    # Cap on how long a rake may stand at one station.
    max_hold_min: float = 240.0
    # A hold is either nothing at all or a real stop. Without this the solver
    # will happily insert a seventeen-second pause, which the verifier
    # correctly rejects as below the minimum dwell.
    min_dwell_min: float = 0.5

    # Lines withdrawn from traffic during the window.
    closures: list[PlannedClosure] = field(default_factory=list)

    log_search: bool = False

    @property
    def start_s(self) -> int:
        return int(self.horizon_start_min * SEC)

    @property
    def end_s(self) -> int:
        return int(self.horizon_end_min * SEC)


@dataclass
class StageResult:
    name: str
    status: str
    objective: float | None
    bound: float | None
    gap_pct: float | None
    seconds: float

    def __str__(self) -> str:
        obj = "—" if self.objective is None else f"{self.objective:.1f}"
        gap = "—" if self.gap_pct is None else f"{self.gap_pct:.1f}%"
        return (f"{self.name:<22}{self.status:<12}obj {obj:>9}  "
                f"gap {gap:>7}  {self.seconds:5.1f}s")


@dataclass
class SolveReport:
    stages: list[StageResult] = field(default_factory=list)
    offered: int = 0
    dispatched: int = 0
    variables: int = 0
    constraints: int = 0
    seconds: float = 0.0
    undispatched: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.stages) and self.stages[0].status in ("OPTIMAL", "FEASIBLE")

    @property
    def gap_pct(self) -> float | None:
        return self.stages[-1].gap_pct if self.stages else None

    def summary(self) -> str:
        lines = [f"dispatched {self.dispatched}/{self.offered} rakes "
                 f"in {self.seconds:.1f}s "
                 f"({self.variables} vars, {self.constraints} constraints)"]
        lines += ["  " + str(s) for s in self.stages]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<SolveReport {self.dispatched}/{self.offered} in {self.seconds:.1f}s>"


class Optimiser:
    """Builds and solves the freight path insertion problem."""

    def __init__(self, graph: SectionGraph, config: OptimiserConfig | None = None):
        self.g = graph
        self.cfg = config or OptimiserConfig()

    # ------------------------------------------------------------- routing
    def _route(self, rake: Rake) -> list[tuple[str, str, str, str]]:
        return route_rake(self.g, rake, self.cfg.closures,
                          self.cfg.start_s, self.cfg.end_s)

    def _closed(self, resource: str, a: int, b: int) -> bool:
        return any(c.resource == resource and c.overlaps(a / SEC, b / SEC)
                   for c in self.cfg.closures)

    # --------------------------------------------------------------- solve
    def solve(self, demand: DayDemand, fixed: Plan | None = None
              ) -> tuple[Plan, SolveReport]:
        t_start = time.perf_counter()
        cfg = self.cfg
        m = cp_model.CpModel()
        H0, H1 = cfg.start_s, cfg.end_s

        # ---- fixed traffic occupies resources before freight is placed ----
        busy: dict[str, list[tuple[int, int]]] = {}
        self._station_busy: dict[str, list[tuple[float, float]]] = {}
        if fixed:
            for t in fixed.trains:
                for mv in t.movements:
                    a, b = int(mv.enter * SEC), int(mv.exit * SEC)
                    if b < H0 or a > H1:
                        continue
                    busy.setdefault(mv.resource, []).append((a, b))
                for h in t.holds:
                    self._station_busy.setdefault(h.resource, []).append(
                        (h.arrive, h.depart))

        rakes: list[tuple[Rake, list, dict]] = []
        line_intervals: dict[str, list] = {}
        stub_intervals: dict[str, list] = {}
        bidi_intervals: dict[str, dict] = {}
        station_holds: dict[str, list] = {}
        presence: list = []
        transit_terms: list = []
        hold_flags: list = []

        for rake in demand:
            chain = self._route(rake)
            if not chain:
                continue
            ready = max(H0, int(rake.ready_min * SEC))
            if ready >= H1:
                continue

            z = m.NewBoolVar(f"z[{rake.id}]")
            enters, leaves, legs = [], [], []
            feasible = True

            for link, line, a, b in chain:
                d = self.g.direction_of(a, b)
                tau = max(1, math.ceil(
                    self.g.running_time(link, line, "FREIGHT", d) * SEC))
                resource = f"{link}:{line}"
                enter = m.NewIntVar(H0, H1, f"in[{rake.id},{link}]")
                leave = m.NewIntVar(H0, H1, f"out[{rake.id},{link}]")
                m.Add(leave == enter + tau)
                ln = self.g.line(link, line)
                # Any line that ends up carrying both directions - a MID
                # road, or an up line taking down traffic because the down
                # line is closed - cannot hold OPPOSING movements at once.
                # Which lines those are is not known until every train has
                # been routed, so record the direction here and add the
                # exclusions afterwards. Same-direction following stays legal.
                bidi_intervals.setdefault(resource, {}).setdefault(
                    d, []).append(
                    m.NewOptionalIntervalVar(enter, tau, leave, z,
                                             f"bidi[{rake.id},{link}]"))
                if ln.signalling is Signalling.ABSOLUTE:
                    # Occupy for the traversal plus the time needed to prove
                    # the section clear, so the next train cannot be admitted
                    # the instant this one leaves.
                    span = tau + int(round(ABSOLUTE_CLEARANCE_MIN * SEC))
                    blocked = m.NewOptionalIntervalVar(
                        enter, span, m.NewIntVar(H0, H1 + span,
                                                 f"blk[{rake.id},{link}]"),
                        z, f"occ[{rake.id},{link}]")
                    line_intervals.setdefault(resource, []).append(blocked)
                else:
                    # Round UP. Rounding to nearest makes the plan a
                    # fraction of a second short of the signalling minimum,
                    # which the verifier correctly rejects.
                    head = max(1, math.ceil(self.g.headway(link, line) * SEC))
                    stub = m.NewOptionalIntervalVar(
                        enter, head, m.NewIntVar(H0, H1 + head, f"h[{rake.id},{link}]"),
                        z, f"hw[{rake.id},{link}]")
                    stub_intervals.setdefault(resource, []).append(stub)
                enters.append(enter)
                leaves.append(leave)
                legs.append((link, line, a, b, enter, leave))

            if not feasible or not legs:
                continue

            m.Add(enters[0] >= ready).OnlyEnforceIf(z)
            m.Add(leaves[-1] <= H1).OnlyEnforceIf(z)

            # chain the legs; a gap is a hold, only where one is possible
            holds: dict[int, tuple] = {}
            for i in range(len(legs) - 1):
                at = legs[i][3]                     # station between the legs
                m.Add(enters[i + 1] >= leaves[i])
                station = self.g.station(at)
                fits = station.holding_lines(rake.length_m)
                if not station.can_cross or not fits:
                    m.Add(enters[i + 1] == leaves[i])
                    continue
                dur = m.NewIntVar(0, int(cfg.max_hold_min * SEC),
                                  f"hold[{rake.id},{at}]")
                m.Add(dur == enters[i + 1] - leaves[i])
                held = m.NewBoolVar(f"held[{rake.id},{at}]")
                min_dwell = max(1, math.ceil(cfg.min_dwell_min * SEC))
                m.Add(dur >= min_dwell).OnlyEnforceIf(held)
                m.Add(dur == 0).OnlyEnforceIf(held.Not())
                iv = m.NewOptionalIntervalVar(leaves[i], dur, enters[i + 1],
                                              held, f"stand[{rake.id},{at}]")
                station_holds.setdefault(at, []).append(iv)
                hold_flags.append(held)
                holds[i] = (at, dur, held)

            presence.append(z)
            transit_terms.append((z, enters[0], leaves[-1]))
            rakes.append((rake, z, legs, holds))

        # ---- resources ---------------------------------------------------
        clear_s = int(round(ABSOLUTE_CLEARANCE_MIN * SEC))
        for resource, intervals in line_intervals.items():
            padded = [(a, b + clear_s) for a, b in busy.get(resource, [])]
            for a, b in _merge(padded):
                intervals.append(m.NewIntervalVar(a, max(1, b - a), b,
                                                  f"fix[{resource},{a}]"))
            if len(intervals) > 1:
                m.AddNoOverlap(intervals)

        for resource, stubs in stub_intervals.items():
            link, _, line = resource.partition(":")
            head = max(1, math.ceil(self.g.headway(link, line) * SEC))
            for a, b in _merge([(x, x + head) for x, _y in busy.get(resource, [])]):
                stubs.append(m.NewIntervalVar(a, max(1, b - a), b,
                                              f"fixhw[{resource},{a}]"))
            if len(stubs) > 1:
                m.AddNoOverlap(stubs)

        # Bidirectional lines: forbid opposing movements only. Fixed traffic
        # is added on the direction it actually ran.
        # Scheduled services are constants, so a pair of them is never
        # constrained here: if a closure has put two opposing services on one
        # line, that is a conflict the optimiser cannot resolve and
        # constraining it would simply make the model infeasible. Only pairs
        # with at least one freight interval are excluded.
        fixed_bidi: dict[str, dict] = {}
        if fixed:
            for t in fixed.trains:
                for mv in t.movements:
                    a, b = int(mv.enter * SEC), int(mv.exit * SEC)
                    if b < H0 or a > H1:
                        continue
                    d = self.g.direction_of(mv.from_station, mv.to_station)
                    fixed_bidi.setdefault(mv.resource, {}).setdefault(
                        d, []).append(
                        m.NewIntervalVar(a, max(1, b - a), b,
                                         f"fixbidi[{mv.resource},{a}]"))

        opposing = 0
        for resource in set(bidi_intervals) | set(fixed_bidi):
            free = bidi_intervals.get(resource, {})
            fix = fixed_bidi.get(resource, {})
            for d, other in ((Direction.DOWN, Direction.UP),
                             (Direction.UP, Direction.DOWN)):
                for iv_a in free.get(d, []):
                    for iv_b in free.get(other, []) + fix.get(other, []):
                        m.AddNoOverlap([iv_a, iv_b])
                        opposing += 1
            # each unordered freight pair was added once from each side
        self.opposing_pairs = opposing

        longest = max((r.length_m for r in demand), default=686.0)
        for code, intervals in station_holds.items():
            capacity = len(self.g.station(code).holding_lines(longest))
            if capacity <= 0:
                for iv in intervals:
                    m.Add(iv.SizeExpr() == 0)
                continue
            if len(intervals) > capacity:
                m.AddCumulative(intervals, [1] * len(intervals), capacity)

        report = SolveReport(offered=len(demand))
        if not presence:
            return Plan(label="empty", horizon=(cfg.horizon_start_min,
                                                cfg.horizon_end_min)), report

        # ---- lexicographic stages ----------------------------------------
        # A fresh solver per stage. Re-solving a model that has been mutated
        # in between, using the same solver object, segfaulted intermittently
        # under parallel search - never with a single worker, which is the
        # signature of a race rather than a modelling error.
        def new_solver() -> cp_model.CpSolver:
            sv = cp_model.CpSolver()
            sv.parameters.max_time_in_seconds = cfg.time_limit_s
            sv.parameters.num_search_workers = cfg.workers
            sv.parameters.log_search_progress = cfg.log_search
            return sv

        cleared = sum(presence)
        transit = sum((leave - enter) for _z, enter, leave in transit_terms)
        stops = sum(hold_flags) if hold_flags else 0

        stages = [("1 throughput", cleared, True),
                  ("2 transit time", transit, False),
                  ("3 stop-starts", stops, False)]

        best = None
        for name, expr, maximise in stages:
            if isinstance(expr, int):          # nothing to optimise
                continue
            m.Maximize(expr) if maximise else m.Minimize(expr)
            solver = new_solver()
            t0 = time.perf_counter()
            status = solver.Solve(m)
            elapsed = time.perf_counter() - t0
            sname = solver.StatusName(status)
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                report.stages.append(StageResult(name, sname, None, None, None, elapsed))
                break
            obj, bound = solver.ObjectiveValue(), solver.BestObjectiveBound()
            gap = (abs(obj - bound) / max(abs(obj), 1e-9) * 100.0)
            report.stages.append(StageResult(name, sname, obj, bound, gap, elapsed))
            best = solver
            # freeze this stage's achievement before optimising the next
            m.Add(expr >= int(round(obj)) if maximise else expr <= int(round(obj)))

        report.variables = len(m.Proto().variables)
        report.constraints = len(m.Proto().constraints)
        report.seconds = time.perf_counter() - t_start
        if best is None:
            return Plan(label="infeasible",
                        horizon=(cfg.horizon_start_min, cfg.horizon_end_min)), report

        return self._extract(best, rakes, report), report

    def _pick_hold_line(self, station: str, rake_m: float,
                        arrive: float, depart: float) -> str:
        """A line at this station that fits the rake and is free meanwhile."""
        for ln in self.g.station(station).holding_lines(rake_m):
            key = f"{station}:{ln.number}"
            taken = self._station_busy.get(key, [])
            if not any(arrive < b and a < depart for a, b in taken):
                self._station_busy.setdefault(key, []).append((arrive, depart))
                return ln.number
        return "1"

    # ------------------------------------------------------------- extract
    def _extract(self, solver, rakes, report: SolveReport) -> Plan:
        """Read the chosen schedule back out of the solver."""
        plans: list[TrainPlan] = []
        for rake, z, legs, holds in rakes:
            if not solver.BooleanValue(z):
                report.undispatched.append(rake.id)
                continue

            movements = [Movement(rake.id, link, line, a, b,
                                  solver.Value(enter) / SEC,
                                  solver.Value(leave) / SEC)
                         for link, line, a, b, enter, leave in legs]
            arrival_at = {mv.to_station: mv.exit for mv in movements}

            held = []
            for at, dur, _flag in holds.values():
                seconds = solver.Value(dur)
                if seconds <= 0:
                    continue
                arrive = arrival_at.get(at)
                if arrive is None:
                    continue
                depart = arrive + seconds / SEC
                # Cumulative decided how many rakes may stand here, not which
                # line each takes. Pick one that fits and is not already
                # occupied by a scheduled service or an earlier rake.
                line = self._pick_hold_line(at, rake.length_m, arrive, depart)
                held.append(Hold(rake.id, at, line, arrive, depart,
                                 "precedence", PRECEDENCE))

            plans.append(TrainPlan(
                id=rake.id, train_class="FREIGHT", direction=rake.dir_enum,
                rake_length_m=rake.length_m, priority=12,
                movements=movements, holds=held))

        report.dispatched = len(plans)
        return Plan(trains=plans,
                    horizon=(self.cfg.horizon_start_min, self.cfg.horizon_end_min),
                    label="optimised freight")
