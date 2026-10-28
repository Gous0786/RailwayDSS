"""
The baseline the optimiser has to beat: dispatch as soon as the road is clear.

This is what a section controller does with a telephone and no look-ahead.
Rakes are taken first-come-first-served; each is sent as early as the
signalling allows and, once moving, is pushed as far as the next free block
permits. Nothing is ever held back so that a train which has not yet arrived
may pass, because without look-ahead there is nothing to hold it for.

Why this exists. Comparing a plan against what the railway recorded cannot
isolate the scheduler's contribution: the record contains yard work, crew
changes and waiting for a path through the next section, none of which any
scheduler on this section could remove. Comparing against this dispatcher
can. It shares the section graph, the running times, the signalling rules,
the routing and the verifier with the optimiser, so the ONLY thing that
differs is the decision rule, and every minute of difference is attributable
to look-ahead.

A baseline is only worth quoting if it is a fair one, so this is a competent
controller rather than a deliberately bad one: it respects priority, it holds
rakes only where a loop long enough actually exists, and it never breaks a
rule the optimiser is held to. A greedy plan should verify just as clean as
an optimised one. What it should not do is sequence.

    from pravah.greedy import GreedyDispatcher

    plan, report = GreedyDispatcher(graph).solve(demand, fixed)
    report.summary()
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .demand import DayDemand, Rake
from .optimise import (SEC, OptimiserConfig, SolveReport, StageResult,
                       route_rake)
from .plan import PRECEDENCE, Hold, Movement, Plan, TrainPlan
from .section import ABSOLUTE_CLEARANCE_MIN, Direction, SectionGraph, Signalling

FREIGHT_PRIORITY = 12

# A rake that cannot be placed is pushed later and retried. Deficits are
# usually tens of minutes, so this converges well before the bound; the bound
# is only here so a pathological case ends as "not dispatched" rather than as
# a hang.
MAX_RESTARTS = 80
MIN_STEP_S = 60.0


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


@dataclass
class _Leg:
    link: str
    line: str
    a: str
    b: str
    direction: Direction
    tau: float            # running time, seconds
    headway: float        # entry separation on automatic block, seconds
    absolute: bool        # absolute block, so the whole section is exclusive


class GreedyDispatcher:
    """First-come-first-served dispatch, under the optimiser's own rules."""

    def __init__(self, graph: SectionGraph, config: OptimiserConfig | None = None):
        self.g = graph
        self.cfg = config or OptimiserConfig()
        self.clear_s = ABSOLUTE_CLEARANCE_MIN * SEC

    # ------------------------------------------------------------ occupancy
    def _reset(self) -> None:
        # Physical occupation, with direction, so opposing movements on a
        # line carrying both ways can be excluded while following moves in
        # the same direction stay legal.
        self.occupy: dict[str, list[tuple[float, float, Direction]]] = {}
        # Exclusive windows on absolute block: traversal plus the time to
        # prove the section clear.
        self.block: dict[str, list[tuple[float, float]]] = {}
        # Entry stubs on automatic block.
        self.stub: dict[str, list[tuple[float, float]]] = {}
        # Which station lines are standing a train.
        self.station_busy: dict[str, list[tuple[float, float]]] = {}

    def _seed(self, fixed: Plan | None) -> None:
        """Scheduled services occupy the section before freight is placed."""
        if not fixed:
            return
        for t in fixed.trains:
            for mv in t.movements:
                try:
                    ln = self.g.line(mv.link, mv.line)
                except (KeyError, AttributeError):
                    continue
                d = self.g.direction_of(mv.from_station, mv.to_station)
                enter, leave = mv.enter * SEC, mv.exit * SEC
                res = mv.resource
                self.occupy.setdefault(res, []).append((enter, leave, d))
                if ln.signalling is Signalling.ABSOLUTE:
                    self.block.setdefault(res, []).append(
                        (enter, leave + self.clear_s))
                else:
                    head = self.g.headway(mv.link, mv.line) * SEC
                    self.stub.setdefault(res, []).append((enter, enter + head))
            for h in t.holds:
                self.station_busy.setdefault(h.resource, []).append(
                    (h.arrive, h.depart))

    def _commit(self, leg: _Leg, enter: float) -> None:
        res = f"{leg.link}:{leg.line}"
        leave = enter + leg.tau
        self.occupy.setdefault(res, []).append((enter, leave, leg.direction))
        if leg.absolute:
            self.block.setdefault(res, []).append((enter, leave + self.clear_s))
        else:
            self.stub.setdefault(res, []).append((enter, enter + leg.headway))

    # ----------------------------------------------------------- feasibility
    def _earliest(self, leg: _Leg, t: float) -> float | None:
        """Earliest entry at or after t that breaks nothing already committed.

        Pushed forward past each conflict in turn. Because every push is to
        the end of a committed span and those are finite, this terminates.
        """
        res = f"{leg.link}:{leg.line}"
        for _ in range(256):
            push = t
            if leg.absolute:
                mine_end = t + leg.tau + self.clear_s
                for a, b in self.block.get(res, ()):
                    if _overlaps(t, mine_end, a, b):
                        push = max(push, b)
            else:
                for a, b in self.stub.get(res, ()):
                    if _overlaps(t, t + leg.headway, a, b):
                        push = max(push, b)
                # a line carrying both directions cannot hold opposing moves
                for a, b, d in self.occupy.get(res, ()):
                    if d is not leg.direction and _overlaps(t, t + leg.tau, a, b):
                        push = max(push, b)
            if push <= t:
                return t
            t = push
        return None

    def _free_hold_line(self, station: str, rake_m: float,
                        arrive: float, depart: float) -> str | None:
        """A line here that fits the rake and is empty for the whole wait.

        Deliberately pure: the run is speculative and may be retried at a
        later departure, so nothing is marked occupied until the rake is
        actually committed. Returning None means the rake cannot stand here,
        which is a real operational limit and not a detail to paper over -
        assuming a free loop is how a plan ends up standing two trains on
        the same platform.
        """
        try:
            st = self.g.station(station)
        except KeyError:
            return None
        if not st.can_cross:
            return None
        for ln in st.holding_lines(rake_m):
            key = f"{station}:{ln.number}"
            if not any(_overlaps(arrive, depart, a, b)
                       for a, b in self.station_busy.get(key, ())):
                return ln.number
        return None

    # -------------------------------------------------------------- the run
    def _legs(self, chain) -> list[_Leg]:
        out = []
        for link, line, a, b in chain:
            d = self.g.direction_of(a, b)
            ln = self.g.line(link, line)
            out.append(_Leg(
                link=link, line=line, a=a, b=b, direction=d,
                tau=max(1.0, math.ceil(
                    self.g.running_time(link, line, "FREIGHT", d) * SEC)),
                headway=max(1.0, math.ceil(self.g.headway(link, line) * SEC)),
                absolute=ln.signalling is Signalling.ABSOLUTE))
        return out

    def _attempt(self, legs: list[_Leg], rake_m: float, start: float,
                 end_s: float) -> tuple[list[float] | None, float | None]:
        """One run at departing at `start`.

        Returns the entry time of each leg, or (None, deficit) where deficit
        is how much later the rake would have had to arrive for a wait it
        cannot take. Waiting is only possible where the railway can actually
        stand the rake, so a rake blocked at a station with no loop has to
        leave its origin later instead - which is exactly what a controller
        is forced to do.
        """
        min_dwell = self.cfg.min_dwell_min * SEC
        entries: list[float] = []
        t = start
        for i, leg in enumerate(legs):
            e = self._earliest(leg, t)
            if e is None:
                return None, None
            if e > t and i > 0:
                # standing here needs a loop that fits AND is free for the
                # whole wait, not merely a loop that exists
                if self._free_hold_line(leg.a, rake_m, t, e) is None:
                    return None, e - t
                # a hold is a real stop or no stop at all; a few seconds of
                # pause is something the verifier rightly rejects
                if e - t < min_dwell:
                    e = self._earliest(leg, t + min_dwell)
                    if e is None:
                        return None, None
                    if self._free_hold_line(leg.a, rake_m, t, e) is None:
                        return None, e - t
            if e + leg.tau > end_s:
                return None, None
            entries.append(e)
            t = e + leg.tau
        return entries, None

    def _place(self, rake: Rake, legs: list[_Leg], ready: float,
               end_s: float) -> list[float] | None:
        start = ready
        for _ in range(MAX_RESTARTS):
            entries, deficit = self._attempt(legs, rake.length_m, start, end_s)
            if entries is not None:
                return entries
            if deficit is None:
                return None
            start += max(deficit, MIN_STEP_S)
            if start >= end_s:
                return None
        return None

    # --------------------------------------------------------------- solve
    def solve(self, demand: DayDemand, fixed: Plan | None = None
              ) -> tuple[Plan, SolveReport]:
        """Dispatch every rake first-come-first-served. Same shape as the
        optimiser's solve, so the two are interchangeable."""
        t_start = time.perf_counter()
        cfg = self.cfg
        H0, H1 = float(cfg.start_s), float(cfg.end_s)
        self._reset()
        self._seed(fixed)

        report = SolveReport()
        plans: list[TrainPlan] = []

        # first come, first served - the whole of the decision rule
        offered = sorted(demand, key=lambda r: (r.ready_min, r.id))
        report.offered = len(offered)

        for rake in offered:
            chain = route_rake(self.g, rake, cfg.closures, cfg.start_s, cfg.end_s)
            if not chain:
                report.undispatched.append(rake.id)
                continue
            ready = max(H0, rake.ready_min * SEC)
            if ready >= H1:
                report.undispatched.append(rake.id)
                continue

            legs = self._legs(chain)
            entries = self._place(rake, legs, ready, H1)
            if entries is None:
                report.undispatched.append(rake.id)
                continue

            # Work out where it stands BEFORE committing anything, so a rake
            # that cannot be stood somewhere is left undispatched rather than
            # half-placed with its movements already occupying the section.
            holds, placeable = [], True
            for i in range(len(legs) - 1):
                arrive = (entries[i] + legs[i].tau) / SEC
                depart = entries[i + 1] / SEC
                if depart - arrive <= 0:
                    continue
                at = legs[i].b
                line = self._free_hold_line(at, rake.length_m, arrive, depart)
                if line is None:
                    placeable = False
                    break
                holds.append(Hold(rake.id, at, line, arrive, depart,
                                  "waiting for the road", PRECEDENCE))
            if not placeable:
                report.undispatched.append(rake.id)
                continue

            movements = []
            for leg, enter in zip(legs, entries):
                self._commit(leg, enter)
                movements.append(Movement(rake.id, leg.link, leg.line, leg.a,
                                          leg.b, enter / SEC,
                                          (enter + leg.tau) / SEC))
            for h in holds:
                self.station_busy.setdefault(h.resource, []).append(
                    (h.arrive, h.depart))

            plans.append(TrainPlan(
                id=rake.id, train_class="FREIGHT", direction=rake.dir_enum,
                rake_length_m=rake.length_m, priority=FREIGHT_PRIORITY,
                movements=movements, holds=holds))

        report.dispatched = len(plans)
        report.seconds = time.perf_counter() - t_start
        # One stage, and no gap: there is no optimality claim to make here.
        # Reporting it in the optimiser's shape keeps the two comparable
        # without pretending this one proved anything.
        report.stages.append(StageResult(
            name="greedy dispatch", status="HEURISTIC", objective=None,
            bound=None, gap_pct=None, seconds=report.seconds))
        return Plan(trains=plans,
                    horizon=(cfg.horizon_start_min, cfg.horizon_end_min),
                    label="greedy freight"), report
