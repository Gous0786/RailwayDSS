"""
The plan: what the optimiser produces and the verifier checks.

This is the contract between the two halves of the system, so it is kept
deliberately plain. Everything reduces to a resource occupation - a train
holding a named resource between two times - which is exactly the shape of a
CP-SAT interval variable, so the optimiser can emit this without translation.

Two kinds of occupation:

    Movement   a train on a running line between two adjacent stations
    Hold       a train standing on a station line, to be passed or crossed

Times are minutes from the start of the planning horizon. Resources are named
the same way pravah.section names them, so a violation can be traced straight
back to the infrastructure record that forbids it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .section import Direction

FREIGHT = "FREIGHT"


@dataclass(frozen=True)
class Movement:
    """A train occupying one running line between two adjacent stations."""
    train: str
    link: str                 # "KTV-KPL" as the section graph names it
    line: str                 # "DN", "UP", "MID"
    from_station: str
    to_station: str
    enter: float              # minutes
    exit: float

    @property
    def resource(self) -> str:
        return f"{self.link}:{self.line}"

    @property
    def duration(self) -> float:
        return self.exit - self.enter

    def to_dict(self) -> dict:
        return {"train": self.train, "link": self.link, "line": self.line,
                "from": self.from_station, "to": self.to_station,
                "enter": round(self.enter, 3), "exit": round(self.exit, 3)}

    @staticmethod
    def from_dict(d: dict) -> "Movement":
        return Movement(d["train"], d["link"], d["line"], d["from"], d["to"],
                        float(d["enter"]), float(d["exit"]))


# Why a train is standing. The distinction matters: a commercial stop can be
# made at any halt with a platform, but holding a train so another may pass
# needs precedence/crossing facility - a loop, so the running line stays clear.
PRECEDENCE = "precedence"
SCHEDULED_STOP = "scheduled"


@dataclass(frozen=True)
class Hold:
    """A train standing on a station line."""
    train: str
    station: str
    line: str                 # station line number, as STTNLINE records it
    arrive: float
    depart: float
    reason: str = ""          # e.g. "precedence to 12841"
    purpose: str = PRECEDENCE

    @property
    def resource(self) -> str:
        return f"{self.station}:{self.line}"

    @property
    def duration(self) -> float:
        return self.depart - self.arrive

    def to_dict(self) -> dict:
        return {"train": self.train, "station": self.station, "line": self.line,
                "arrive": round(self.arrive, 3), "depart": round(self.depart, 3),
                "reason": self.reason, "purpose": self.purpose}

    @staticmethod
    def from_dict(d: dict) -> "Hold":
        return Hold(d["train"], d["station"], d["line"], float(d["arrive"]),
                    float(d["depart"]), d.get("reason", ""),
                    d.get("purpose", PRECEDENCE))


@dataclass
class TrainPlan:
    """One train's complete path through the section."""
    id: str
    train_class: str
    direction: Direction
    rake_length_m: float = 650.0
    priority: int = 9
    movements: list[Movement] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)
    # Published timings, where the train has them. Used only to check that a
    # scheduled service has not been moved beyond the configured tolerance.
    scheduled: dict[str, float] | None = None

    @property
    def is_freight(self) -> bool:
        return self.train_class.upper() in (FREIGHT, "GOODS")

    @property
    def entry(self) -> float | None:
        return min((m.enter for m in self.movements), default=None)

    @property
    def clear(self) -> float | None:
        return max((m.exit for m in self.movements), default=None)

    @property
    def transit(self) -> float | None:
        a, b = self.entry, self.clear
        return None if a is None or b is None else b - a

    @property
    def detention(self) -> float:
        return sum(h.duration for h in self.holds)

    def ordered(self) -> list[Movement]:
        return sorted(self.movements, key=lambda m: m.enter)

    def to_dict(self) -> dict:
        return {"id": self.id, "class": self.train_class,
                "dir": self.direction.value, "rake_length_m": self.rake_length_m,
                "priority": self.priority,
                "movements": [m.to_dict() for m in self.movements],
                "holds": [h.to_dict() for h in self.holds],
                "scheduled": self.scheduled}

    @staticmethod
    def from_dict(d: dict) -> "TrainPlan":
        return TrainPlan(
            id=d["id"], train_class=d["class"],
            direction=Direction(d["dir"]),
            rake_length_m=float(d.get("rake_length_m", 650.0)),
            priority=int(d.get("priority", 9)),
            movements=[Movement.from_dict(x) for x in d.get("movements", [])],
            holds=[Hold.from_dict(x) for x in d.get("holds", [])],
            scheduled=d.get("scheduled"))


@dataclass(frozen=True)
class PlannedClosure:
    """A resource withdrawn from traffic for part of the horizon."""
    resource: str
    start: float
    end: float
    reason: str = ""

    def active(self, at: float) -> bool:
        return self.start <= at <= self.end

    def overlaps(self, a: float, b: float) -> bool:
        return a < self.end and self.start < b

    def to_dict(self) -> dict:
        return {"resource": self.resource, "start": self.start,
                "end": self.end, "reason": self.reason}

    @staticmethod
    def from_dict(d: dict) -> "PlannedClosure":
        return PlannedClosure(d["resource"], float(d["start"]), float(d["end"]),
                              d.get("reason", ""))


@dataclass
class Plan:
    """A complete proposal for the section over a planning horizon."""
    trains: list[TrainPlan] = field(default_factory=list)
    closures: list[PlannedClosure] = field(default_factory=list)
    horizon: tuple[float, float] = (0.0, 1440.0)
    label: str = ""

    def train(self, train_id: str) -> TrainPlan:
        for t in self.trains:
            if t.id == train_id:
                return t
        raise KeyError(train_id)

    @property
    def freight(self) -> list[TrainPlan]:
        return [t for t in self.trains if t.is_freight]

    @property
    def scheduled_services(self) -> list[TrainPlan]:
        return [t for t in self.trains if not t.is_freight]

    def closed_resources(self, a: float, b: float) -> set[str]:
        return {c.resource for c in self.closures if c.overlaps(a, b)}

    def to_dict(self) -> dict:
        return {"label": self.label, "horizon": list(self.horizon),
                "trains": [t.to_dict() for t in self.trains],
                "closures": [c.to_dict() for c in self.closures]}

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        h = d.get("horizon", [0.0, 1440.0])
        return Plan(trains=[TrainPlan.from_dict(x) for x in d.get("trains", [])],
                    closures=[PlannedClosure.from_dict(x) for x in d.get("closures", [])],
                    horizon=(float(h[0]), float(h[1])),
                    label=d.get("label", ""))

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @staticmethod
    def load(path: Path | str) -> "Plan":
        return Plan.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def __repr__(self) -> str:
        return (f"<Plan {self.label or 'unnamed'}: {len(self.trains)} trains "
                f"({len(self.freight)} freight), {len(self.closures)} closures, "
                f"{self.horizon[0]:.0f}-{self.horizon[1]:.0f} min>")


def plan_to_paths(graph, plan: "Plan") -> list[dict]:
    """A plan as chainage-over-time, the shape the train graph draws.

    Each movement contributes its two endpoints; a hold shows up on its own,
    as the horizontal run between one movement's exit and the next one's
    entry at the same chainage. Times are seconds, matching the replay feed,
    so the graph needs no special case for freight.
    """
    km = {s.code: s.cum_km for s in graph.stations}
    out: list[dict] = []
    for t in plan.trains:
        moves = t.ordered()
        if not moves:
            continue
        pts: list[list[float]] = []

        def add(seconds: float, chainage: float) -> None:
            if pts and pts[-1][0] == seconds and pts[-1][1] == chainage:
                return
            pts.append([round(seconds, 1), round(chainage, 3)])

        for mv in moves:
            a, b = km.get(mv.from_station), km.get(mv.to_station)
            if a is None or b is None:
                continue
            add(mv.enter * 60.0, a)
            add(mv.exit * 60.0, b)
        if len(pts) < 2:
            continue
        out.append({
            "id": t.id,
            "type": t.train_class,
            "dir": t.direction.value,
            "pts": pts,
            "holds": [{"station": h.station, "line": h.line,
                       "from": round(h.arrive * 60.0, 1),
                       "to": round(h.depart * 60.0, 1),
                       "km": km.get(h.station), "reason": h.reason}
                      for h in t.holds if h.station in km],
        })
    return out
