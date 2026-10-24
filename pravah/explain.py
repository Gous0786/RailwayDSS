"""
Why the plan holds a rake where it does.

A controller will not act on a recommendation they cannot interrogate, and the
problem statement asks for explanations and override, so every hold has to
justify itself.

No separate reasoning engine is needed. A hold exists precisely because
running through would break something, so the explanation is the counter-
factual: remove the hold, pull the later legs earlier by its duration, and
re-verify. Whatever the verifier then reports IS the reason, already worded,
already naming the train that would have been blocked and the rule that would
have been broken.

    rake PLINBGDS29072025214106 held 18.0 min at Garividi
      remove the hold ->
      block_conflict GVI-CPP:DN @538.0  [PLINBGDS..., 17016]
        17016 enters at 538.00; the rake clears at 541.72 and the section
        must be proved clear until 543.72 (absolute block)

What this does and does not establish: it answers "what breaks if this rake
does not wait here", not "this hold was unavoidable". A different plan might
avoid it altogether, and only re-solving with the hold forbidden can say so.
The wording throughout keeps that distinction.

    from pravah.explain import explain
    reasons = explain(graph, plan, fixed)
    reasons["MGPVBPSL31072025175229"].holds[0].detail
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .plan import Movement, Plan, TrainPlan
from .section import SectionGraph
from .verify import Verifier, VerifierConfig


@dataclass
class Blocker:
    """A train that would have been obstructed had the rake not waited."""
    train: str
    train_class: str
    priority: int
    kind: str
    resource: str
    at: float | None
    detail: str

    def to_dict(self) -> dict:
        return {"train": self.train, "class": self.train_class,
                "priority": self.priority, "kind": self.kind,
                "resource": self.resource, "at": self.at, "detail": self.detail}


@dataclass
class HoldReason:
    station: str
    line: str
    from_min: float
    to_min: float
    duration_min: float
    blockers: list[Blocker] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {"station": self.station, "line": self.line,
                "from_min": round(self.from_min, 1),
                "to_min": round(self.to_min, 1),
                "duration_min": round(self.duration_min, 1),
                "blockers": [b.to_dict() for b in self.blockers],
                "detail": self.detail}


@dataclass
class Leg:
    from_station: str
    to_station: str
    line: str
    enter_min: float
    exit_min: float

    def to_dict(self) -> dict:
        return {"from": self.from_station, "to": self.to_station,
                "line": self.line, "enter_min": round(self.enter_min, 1),
                "exit_min": round(self.exit_min, 1)}


@dataclass
class Explanation:
    train_id: str
    train_class: str
    direction: str
    length_m: float
    entry_station: str
    exit_station: str
    dispatched_min: float | None
    cleared_min: float | None
    transit_min: float | None
    detention_min: float
    ready_min: float | None = None
    legs: list[Leg] = field(default_factory=list)
    holds: list[HoldReason] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "train_id": self.train_id, "class": self.train_class,
            "dir": self.direction, "length_m": self.length_m,
            "entry": self.entry_station, "exit": self.exit_station,
            "ready_min": None if self.ready_min is None else round(self.ready_min, 1),
            "dispatched_min": None if self.dispatched_min is None else round(self.dispatched_min, 1),
            "cleared_min": None if self.cleared_min is None else round(self.cleared_min, 1),
            "transit_min": None if self.transit_min is None else round(self.transit_min, 1),
            "detention_min": round(self.detention_min, 1),
            "legs": [l.to_dict() for l in self.legs],
            "holds": [h.to_dict() for h in self.holds],
            "note": self.note,
        }


def _without_hold(train: TrainPlan, hold) -> TrainPlan:
    """The same rake, running through instead of standing aside."""
    shift = hold.duration
    moves = []
    for mv in train.ordered():
        if mv.enter >= hold.depart - 1e-9:
            moves.append(Movement(mv.train, mv.link, mv.line, mv.from_station,
                                  mv.to_station, mv.enter - shift, mv.exit - shift))
        else:
            moves.append(mv)
    return TrainPlan(id=train.id, train_class=train.train_class,
                     direction=train.direction, rake_length_m=train.rake_length_m,
                     priority=train.priority, movements=moves,
                     holds=[h for h in train.holds if h is not hold])


def _describe(hold, blockers: list[Blocker]) -> str:
    if not blockers:
        return (f"Held {hold.duration:.0f} min at {hold.station}. Running "
                f"through does not conflict with anything in this plan, so the "
                f"wait comes from the schedule the optimiser chose rather than "
                f"from a specific train.")
    first = min(blockers, key=lambda b: (b.at if b.at is not None else 1e9))
    others = {b.train for b in blockers} - {first.train}
    who = f"{first.train} ({first.train_class}, priority {first.priority})"
    extra = f" and {len(others)} other service(s)" if others else ""
    return (f"Held {hold.duration:.0f} min at {hold.station}. Running through "
            f"would have obstructed {who}{extra} on {first.resource}"
            + (f" at {first.at:.0f} min" if first.at is not None else "")
            + f" — {first.kind.replace('_', ' ')}.")


def explain(graph: SectionGraph, plan: Plan, fixed: Plan | None = None,
            config: VerifierConfig | None = None,
            demand=None) -> dict[str, Explanation]:
    """Explain every hold in the plan, and note anything not dispatched."""
    cfg = config or VerifierConfig.load()
    verifier = Verifier(graph, cfg)

    others = list(fixed.trains) if fixed else []
    lookup: dict[str, TrainPlan] = {t.id: t for t in list(plan.trains) + others}
    ready = {}
    if demand is not None:
        ready = {r.id: r.ready_min for r in demand}
    entry_exit = {}
    if demand is not None:
        entry_exit = {r.id: (r.entry_station, r.exit_station) for r in demand}

    out: dict[str, Explanation] = {}
    for train in plan.trains:
        moves = train.ordered()
        ent, ext = entry_exit.get(
            train.id,
            (moves[0].from_station if moves else "", moves[-1].to_station if moves else ""))
        ex = Explanation(
            train_id=train.id, train_class=train.train_class,
            direction=train.direction.value, length_m=train.rake_length_m,
            entry_station=ent, exit_station=ext,
            dispatched_min=train.entry, cleared_min=train.clear,
            transit_min=train.transit, detention_min=train.detention,
            ready_min=ready.get(train.id),
            legs=[Leg(m.from_station, m.to_station, m.line, m.enter, m.exit)
                  for m in moves])

        for hold in sorted(train.holds, key=lambda h: h.arrive):
            # everything except this rake stays exactly as planned
            rest = [t for t in plan.trains if t.id != train.id] + others
            base = {str(v) for v in
                    verifier.verify(Plan(trains=rest + [train],
                                         horizon=plan.horizon)).errors}
            counter = Plan(trains=rest + [_without_hold(train, hold)],
                           horizon=plan.horizon)
            blockers = []
            for v in verifier.verify(counter).errors:
                if str(v) in base or train.id not in v.trains:
                    continue
                other = next((t for t in v.trains if t != train.id), None)
                if other is None:
                    continue
                tp = lookup.get(other)
                blockers.append(Blocker(
                    train=other,
                    train_class=tp.train_class if tp else "?",
                    priority=tp.priority if tp else graph.priority("FREIGHT"),
                    kind=v.kind.value, resource=v.resource, at=v.at,
                    detail=v.detail))
            reason = HoldReason(hold.station, hold.line, hold.arrive,
                                hold.depart, hold.duration, blockers)
            reason.detail = _describe(hold, blockers)
            ex.holds.append(reason)

        out[train.id] = ex

    # rakes that were offered but never ran
    if demand is not None:
        planned = {t.id for t in plan.trains}
        for rake in demand:
            if rake.id in planned:
                continue
            out[rake.id] = Explanation(
                train_id=rake.id, train_class="FREIGHT",
                direction=rake.direction, length_m=rake.length_m,
                entry_station=rake.entry_station, exit_station=rake.exit_station,
                dispatched_min=None, cleared_min=None, transit_min=None,
                detention_min=0.0, ready_min=rake.ready_min,
                note=("Offered but not dispatched within the planning horizon. "
                      "Why it was left out cannot be read off this plan — it "
                      "needs a re-solve with this rake forced in, to see what "
                      "it would displace."))
    return out
