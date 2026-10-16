"""
Independent verification of a plan.

Nothing the optimiser produces should be believed until something else has
checked it. A solver can only violate constraints it was given, so a plan that
CP-SAT reports as feasible may still be impossible on the ground. This module
re-derives everything from the plan's times alone and reports what breaks.

It shares no machinery with the solver. The only import outside the standard
library is pravah.section, because both must agree on what the railway
physically is. That is a real shared dependency and worth stating: if the
section graph has a wrong headway, both inherit it. The mitigation is that the
section graph is itself validated against the published timetable.

The sweep also produces the KPIs, since it already walks every occupation:
throughput, average delay, punctuality, utilization, detention.

Usage
-----
    from pravah.verify import Verifier, VerifierConfig

    report = Verifier(graph).verify(plan)
    report.ok            -> False if any ERROR-level violation
    report.violations    -> what broke, where, when
    report.kpis          -> throughput, delay, punctuality, utilization

Every threshold is configurable; see VerifierConfig or config/verifier.toml.
"""

from __future__ import annotations

import statistics
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from itertools import pairwise
from pathlib import Path

from .plan import PRECEDENCE, Hold, Movement, Plan, TrainPlan
from .section import (ABSOLUTE_CLEARANCE_MIN, Direction, SectionGraph,
                      Signalling)

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "verifier.toml"


class Severity(str, Enum):
    ERROR = "error"        # the plan is not valid
    WARNING = "warning"    # legal but worth flagging
    IGNORE = "ignore"      # check disabled


class Kind(str, Enum):
    BLOCK_CONFLICT = "block_conflict"
    HEADWAY = "headway"
    CROSSING_CONFLICT = "crossing_conflict"
    CONTINUITY = "continuity"
    BLOCKING = "blocking"
    CLOSURE = "closure"
    RUNNING_TIME = "running_time"
    HOLD_INFEASIBLE = "hold_infeasible"
    STATION_CAPACITY = "station_capacity"
    SERVICE_INTEGRITY = "service_integrity"
    LONG_HOLD = "long_hold"


@dataclass(frozen=True)
class Violation:
    kind: Kind
    severity: Severity
    detail: str
    trains: tuple[str, ...] = ()
    resource: str = ""
    at: float | None = None

    def __str__(self) -> str:
        when = f" @{self.at:.1f}min" if self.at is not None else ""
        who = f" [{', '.join(self.trains)}]" if self.trains else ""
        where = f" {self.resource}" if self.resource else ""
        return f"{self.severity.value.upper():7} {self.kind.value}{where}{when}{who}: {self.detail}"


@dataclass
class VerifierConfig:
    """Every threshold the verifier applies. Defaults encode today's working."""

    # --- service integrity ------------------------------------------------
    # How far a scheduled service may be moved. 0 means immovable, which is
    # how the section is worked today. Raise it to explore the trade-off
    # between freight throughput and passenger punctuality.
    service_tolerance_min: float = 0.0
    punctuality_threshold_min: float = 5.0

    # --- safety -----------------------------------------------------------
    headway_margin_min: float = 0.0
    running_time_tolerance_pct: float = 2.0
    # Published schedules are recorded to the whole minute, so a run can appear
    # fractionally faster than the technical minimum through rounding alone. A
    # percentage alone cannot absorb that on a short link - 2% of a 4 minute
    # block is 5 seconds - so an absolute floor is applied as well.
    running_time_tolerance_min: float = 1.0
    min_dwell_min: float = 0.5
    # Under absolute block the section must be proved clear before the next
    # train is admitted, so the gap is measured from the previous train's
    # exit, not from its entry. A line-speed headway is too lax here: a slow
    # freight occupies the section far longer than the headway implies.
    absolute_clearance_min: float = ABSOLUTE_CLEARANCE_MIN
    enforce_blocking: bool = True
    blocking_gap_tolerance_min: float = 0.5

    # --- holding ----------------------------------------------------------
    allow_hold_on_main: bool = True
    assume_unknown_line_fits: bool = False
    warn_hold_over_min: float = 60.0

    # --- severities (promote or demote any check) -------------------------
    severity: dict[str, str] = field(default_factory=dict)

    def severity_of(self, kind: Kind) -> Severity:
        raw = self.severity.get(kind.value)
        if raw:
            return Severity(raw)
        return (Severity.WARNING if kind is Kind.LONG_HOLD else Severity.ERROR)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "VerifierConfig":
        p = Path(path) if path else DEFAULT_CONFIG
        if not p.exists():
            return cls()
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        svc, saf, hold = raw.get("service", {}), raw.get("safety", {}), raw.get("holding", {})
        return cls(
            service_tolerance_min=float(svc.get("tolerance_min", 0.0)),
            punctuality_threshold_min=float(svc.get("punctuality_threshold_min", 5.0)),
            headway_margin_min=float(saf.get("headway_margin_min", 0.0)),
            running_time_tolerance_pct=float(saf.get("running_time_tolerance_pct", 2.0)),
            running_time_tolerance_min=float(saf.get("running_time_tolerance_min", 1.0)),
            min_dwell_min=float(saf.get("min_dwell_min", 0.5)),
            absolute_clearance_min=float(saf.get("absolute_clearance_min",
                                                 ABSOLUTE_CLEARANCE_MIN)),
            enforce_blocking=bool(saf.get("enforce_blocking", True)),
            blocking_gap_tolerance_min=float(saf.get("blocking_gap_tolerance_min", 0.5)),
            allow_hold_on_main=bool(hold.get("allow_hold_on_main", True)),
            assume_unknown_line_fits=bool(hold.get("assume_unknown_line_fits", False)),
            warn_hold_over_min=float(hold.get("warn_hold_over_min", 60.0)),
            severity={k: str(v) for k, v in raw.get("severity", {}).items()},
        )


@dataclass
class KPIs:
    """The four measures the problem statement names, plus detention."""
    trains_cleared: int = 0
    freight_cleared: int = 0
    horizon_hours: float = 0.0
    throughput_per_day: float = 0.0
    freight_per_day: float = 0.0
    mean_transit_min: float = 0.0
    mean_freight_transit_min: float = 0.0
    mean_delay_min: float = 0.0
    punctuality_pct: float = 100.0
    detention_min: float = 0.0
    detention_pct: float = 0.0
    utilization_pct: float = 0.0
    busiest_line: str = ""
    busiest_line_pct: float = 0.0

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    kpis: KPIs = field(default_factory=KPIs)
    checked: int = 0

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for v in self.violations:
            out[v.kind.value] += 1
        return dict(out)

    def summary(self) -> str:
        if self.ok:
            head = f"VALID — {self.checked} occupations checked"
        else:
            head = f"INVALID — {len(self.errors)} error(s)"
        if self.warnings:
            head += f", {len(self.warnings)} warning(s)"
        return head

    def __repr__(self) -> str:
        return f"<Report {self.summary()}>"


class Verifier:
    """Checks a plan against the infrastructure, by discrete-event sweep."""

    def __init__(self, graph: SectionGraph, config: VerifierConfig | None = None):
        self.g = graph
        self.cfg = config or VerifierConfig()

    # ------------------------------------------------------------- helpers
    def _add(self, out: list[Violation], kind: Kind, detail: str,
             trains: tuple[str, ...] = (), resource: str = "",
             at: float | None = None) -> None:
        sev = self.cfg.severity_of(kind)
        if sev is Severity.IGNORE:
            return
        out.append(Violation(kind, sev, detail, trains, resource, at))

    def _line_of(self, mv: Movement):
        try:
            return self.g.line(mv.link, mv.line)
        except KeyError:
            return None

    # -------------------------------------------------------------- verify
    def verify(self, plan: Plan) -> Report:
        rep = Report()
        v = rep.violations

        occupancy: dict[str, list[tuple[float, float, TrainPlan, Movement]]] = defaultdict(list)
        for t in plan.trains:
            for mv in t.movements:
                occupancy[mv.resource].append((mv.enter, mv.exit, t, mv))
                rep.checked += 1
            for h in t.holds:
                rep.checked += 1

        self._check_resources(occupancy, v)
        self._check_closures(plan, v)
        for t in plan.trains:
            self._check_train(t, v)
        self._check_station_capacity(plan, v)
        self._check_service_integrity(plan, v)

        rep.kpis = self._kpis(plan, occupancy)
        return rep

    # ---------------------------------------------- running line occupancy
    def _check_resources(self, occupancy, v: list[Violation]) -> None:
        """Overlap, headway and opposing movements, one resource at a time."""
        for resource, items in occupancy.items():
            items.sort(key=lambda x: x[0])
            link_id, _, line_name = resource.partition(":")
            try:
                line = self.g.line(link_id, line_name)
            except (KeyError, ValueError):
                line = None
            required = (self.g.headway(link_id, line_name) + self.cfg.headway_margin_min
                        if line else self.cfg.headway_margin_min)

            for (a_in, a_out, ta, ma), (b_in, b_out, tb, mb) in pairwise(items):
                overlapping = b_in < a_out
                opposing = ma.from_station != mb.from_station

                if overlapping and opposing:
                    self._add(v, Kind.CROSSING_CONFLICT,
                              f"{ta.id} and {tb.id} occupy the same line in opposite "
                              f"directions ({a_in:.1f}-{a_out:.1f} vs {b_in:.1f}-{b_out:.1f})",
                              (ta.id, tb.id), resource, b_in)
                    continue

                # Absolute block admits one train at a time and the section
                # must be proved clear afterwards. Automatic block allows
                # several trains in different signal sections, so there only
                # the separation between entries matters.
                if line and line.signalling is Signalling.ABSOLUTE:
                    need = a_out + self.cfg.absolute_clearance_min
                    if b_in + 1e-9 < need:
                        kind = (Kind.BLOCK_CONFLICT if overlapping
                                else Kind.HEADWAY)
                        self._add(v, kind,
                                  f"{tb.id} enters at {b_in:.2f}; {ta.id} clears at "
                                  f"{a_out:.2f} and the section must be proved "
                                  f"clear until {need:.2f} (absolute block)",
                                  (ta.id, tb.id), resource, b_in)
                    continue

                gap = b_in - a_in
                if gap + 1e-9 < required:
                    self._add(v, Kind.HEADWAY,
                              f"{tb.id} follows {ta.id} after {gap:.2f} min, "
                              f"minimum is {required:.2f}",
                              (ta.id, tb.id), resource, b_in)

    # ------------------------------------------------------------ closures
    def _check_closures(self, plan: Plan, v: list[Violation]) -> None:
        if not plan.closures:
            return
        for t in plan.trains:
            for mv in t.movements:
                for c in plan.closures:
                    if c.resource == mv.resource and c.overlaps(mv.enter, mv.exit):
                        self._add(v, Kind.CLOSURE,
                                  f"{t.id} uses {mv.resource} during a closure "
                                  f"({c.start:.0f}-{c.end:.0f}"
                                  f"{', ' + c.reason if c.reason else ''})",
                                  (t.id,), mv.resource, mv.enter)
            for h in t.holds:
                for c in plan.closures:
                    if c.resource == h.resource and c.overlaps(h.arrive, h.depart):
                        self._add(v, Kind.CLOSURE,
                                  f"{t.id} stands on {h.resource} during a closure",
                                  (t.id,), h.resource, h.arrive)

    # --------------------------------------------------- per-train checks
    def _check_train(self, t: TrainPlan, v: list[Violation]) -> None:
        moves = t.ordered()
        if not moves:
            return

        for mv in moves:
            line = self._line_of(mv)
            if line is None:
                self._add(v, Kind.CONTINUITY,
                          f"{t.id} uses {mv.resource}, which does not exist",
                          (t.id,), mv.resource, mv.enter)
                continue
            if mv.duration <= 0:
                self._add(v, Kind.RUNNING_TIME,
                          f"{t.id} occupies {mv.resource} for {mv.duration:.2f} min",
                          (t.id,), mv.resource, mv.enter)
                continue
            direction = self.g.direction_of(mv.from_station, mv.to_station)
            minimum = self.g.running_time(mv.link, mv.line, t.train_class, direction)
            allowance = max(minimum * self.cfg.running_time_tolerance_pct / 100.0,
                            self.cfg.running_time_tolerance_min)
            floor = minimum - allowance
            if mv.duration + 1e-9 < floor:
                self._add(v, Kind.RUNNING_TIME,
                          f"{t.id} crosses {mv.resource} in {mv.duration:.2f} min; "
                          f"the technical minimum is {minimum:.2f}",
                          (t.id,), mv.resource, mv.enter)

        # the route must be a connected chain, travelled forwards in time
        holds_at = {h.station: h for h in t.holds}
        for a, b in pairwise(moves):
            if a.to_station != b.from_station:
                self._add(v, Kind.CONTINUITY,
                          f"{t.id} leaves {a.to_station} but next occupies a line "
                          f"from {b.from_station}",
                          (t.id,), b.resource, b.enter)
                continue
            gap = b.enter - a.exit
            if gap < -1e-9:
                self._add(v, Kind.CONTINUITY,
                          f"{t.id} enters {b.resource} at {b.enter:.2f} before "
                          f"clearing {a.resource} at {a.exit:.2f}",
                          (t.id,), b.resource, b.enter)
                continue
            if not self.cfg.enforce_blocking:
                continue
            if gap <= self.cfg.blocking_gap_tolerance_min:
                continue
            h = holds_at.get(a.to_station)
            covered = (h is not None
                       and h.arrive <= a.exit + self.cfg.blocking_gap_tolerance_min
                       and h.depart >= b.enter - self.cfg.blocking_gap_tolerance_min)
            if not covered:
                self._add(v, Kind.BLOCKING,
                          f"{t.id} waits {gap:.2f} min at {a.to_station} with no "
                          f"hold recorded; a train must stand on a line",
                          (t.id,), f"{a.to_station}:?", a.exit)

        for h in t.holds:
            self._check_hold(t, h, v)

    def _check_hold(self, t: TrainPlan, h: Hold, v: list[Violation]) -> None:
        if h.duration < 0:
            self._add(v, Kind.HOLD_INFEASIBLE,
                      f"{t.id} departs {h.station} before arriving",
                      (t.id,), h.resource, h.arrive)
            return
        if 0 < h.duration < self.cfg.min_dwell_min:
            self._add(v, Kind.HOLD_INFEASIBLE,
                      f"{t.id} stands at {h.station} for {h.duration:.2f} min, "
                      f"below the {self.cfg.min_dwell_min} min minimum",
                      (t.id,), h.resource, h.arrive)
        try:
            station = self.g.station(h.station)
        except KeyError:
            self._add(v, Kind.HOLD_INFEASIBLE,
                      f"{t.id} held at {h.station}, which is not on the section",
                      (t.id,), h.resource, h.arrive)
            return
        # A commercial stop may be made at any halt with a platform. Being
        # held so another train can pass is different: it needs a loop, or the
        # running line is blocked for the duration.
        if h.purpose == PRECEDENCE and not station.can_cross:
            self._add(v, Kind.HOLD_INFEASIBLE,
                      f"{t.id} held at {h.station} to be passed, but it has no "
                      f"precedence or crossing facility",
                      (t.id,), h.resource, h.arrive)
            return
        line = next((ln for ln in station.lines if ln.number == h.line), None)
        if line is None:
            self._add(v, Kind.HOLD_INFEASIBLE,
                      f"{t.id} held on line {h.line} at {h.station}, which does "
                      f"not exist",
                      (t.id,), h.resource, h.arrive)
            return
        if line.length_m is None:
            if not self.cfg.assume_unknown_line_fits:
                self._add(v, Kind.HOLD_INFEASIBLE,
                          f"{t.id} ({t.rake_length_m:.0f} m) held on {h.resource}, "
                          f"whose length is not recorded",
                          (t.id,), h.resource, h.arrive)
        elif line.length_m < t.rake_length_m:
            self._add(v, Kind.HOLD_INFEASIBLE,
                      f"{t.id} is {t.rake_length_m:.0f} m but {h.resource} is only "
                      f"{line.length_m:.0f} m",
                      (t.id,), h.resource, h.arrive)
        if h.duration > self.cfg.warn_hold_over_min:
            self._add(v, Kind.LONG_HOLD,
                      f"{t.id} stands at {h.station} for {h.duration:.0f} min",
                      (t.id,), h.resource, h.arrive)

    # -------------------------------------------------- station occupancy
    def _check_station_capacity(self, plan: Plan, v: list[Violation]) -> None:
        by_station: dict[str, list[tuple[float, int, str, str]]] = defaultdict(list)
        for t in plan.trains:
            for h in t.holds:
                by_station[h.station].append((h.arrive, +1, t.id, h.line))
                by_station[h.station].append((h.depart, -1, t.id, h.line))
        for code, events in by_station.items():
            try:
                capacity = len(self.g.station(code).lines)
            except KeyError:
                continue
            if capacity == 0:
                continue
            events.sort(key=lambda e: (e[0], e[1]))
            standing, here = 0, []
            for at, delta, train, _line in events:
                standing += delta
                if delta > 0:
                    here.append(train)
                elif train in here:
                    here.remove(train)
                if standing > capacity:
                    self._add(v, Kind.STATION_CAPACITY,
                              f"{standing} trains standing at {code}, which has "
                              f"{capacity} line(s)",
                              tuple(here), code, at)
                    break

        # two trains cannot stand on the same line at once
        per_line: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
        for t in plan.trains:
            for h in t.holds:
                per_line[h.resource].append((h.arrive, h.depart, t.id))
        for resource, items in per_line.items():
            items.sort()
            for (a0, a1, ta), (b0, b1, tb) in pairwise(items):
                if b0 < a1 - 1e-9:
                    self._add(v, Kind.STATION_CAPACITY,
                              f"{ta} and {tb} both stand on {resource}",
                              (ta, tb), resource, b0)

    # --------------------------------------------------- scheduled services
    def _check_service_integrity(self, plan: Plan, v: list[Violation]) -> None:
        tol = self.cfg.service_tolerance_min
        for t in plan.trains:
            if not t.scheduled:
                continue
            for mv in t.ordered():
                want = t.scheduled.get(mv.from_station)
                if want is None:
                    continue
                drift = mv.enter - float(want)
                if abs(drift) > tol + 1e-9:
                    self._add(v, Kind.SERVICE_INTEGRITY,
                              f"{t.id} leaves {mv.from_station} {drift:+.1f} min from "
                              f"its published time; tolerance is {tol:.0f}",
                              (t.id,), mv.resource, mv.enter)

    # ----------------------------------------------------------------- KPIs
    def _kpis(self, plan: Plan, occupancy) -> KPIs:
        k = KPIs()
        span = max(1e-9, plan.horizon[1] - plan.horizon[0])
        k.horizon_hours = span / 60.0

        done = [t for t in plan.trains if t.movements]
        freight = [t for t in done if t.is_freight]
        k.trains_cleared = len(done)
        k.freight_cleared = len(freight)
        days = max(span / 1440.0, 1e-9)
        k.throughput_per_day = len(done) / days
        k.freight_per_day = len(freight) / days

        transits = [t.transit for t in done if t.transit]
        if transits:
            k.mean_transit_min = statistics.mean(transits)
        f_transits = [t.transit for t in freight if t.transit]
        if f_transits:
            k.mean_freight_transit_min = statistics.mean(f_transits)

        drifts = []
        for t in plan.trains:
            if not t.scheduled:
                continue
            for mv in t.ordered():
                want = t.scheduled.get(mv.from_station)
                if want is not None:
                    drifts.append(mv.enter - float(want))
        if drifts:
            k.mean_delay_min = statistics.mean(drifts)
            on_time = sum(1 for d in drifts
                          if abs(d) <= self.cfg.punctuality_threshold_min)
            k.punctuality_pct = 100.0 * on_time / len(drifts)

        k.detention_min = sum(t.detention for t in done)
        moving = sum(t.transit for t in done if t.transit) or 0.0
        if moving:
            k.detention_pct = 100.0 * k.detention_min / moving

        all_lines = [ln.id for l in self.g.links.values() for ln in l.lines]
        if all_lines:
            busy = {r: sum(b - a for a, b, _, _ in items)
                    for r, items in occupancy.items()}
            total = sum(busy.values())
            k.utilization_pct = 100.0 * total / (len(all_lines) * span)
            if busy:
                top = max(busy, key=busy.get)
                k.busiest_line = top
                k.busiest_line_pct = 100.0 * busy[top] / span
        return k
