"""
Freight demand: the rakes the optimiser has to schedule.

Produced by analysis/build_freight_demand.py, which inverts the recorded goods
movements into demand. This is the read side.

    from pravah.demand import DemandBook

    book = DemandBook.load()
    day  = book.day("2025-08-01")        # 112 rakes
    day.between(360, 720)                # those presenting in the morning
    day.to_train_plans()                 # empty TrainPlans for the optimiser

A rake's `ready_min` is when it actually appeared at the section, so it already
carries whatever happened upstream. That is the right basis for planning: the
question is what to do with a rake once it is here, not why it is late.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from .plan import TrainPlan
from .section import Direction

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "derived" / "freight_demand.json"
FREIGHT_PRIORITY = 12


@dataclass(frozen=True)
class Rake:
    """One consignment presenting itself at the section."""
    id: str
    rake_id: str
    entry_station: str
    exit_station: str
    direction: str                 # "DN" or "UP"
    ready_min: float
    day: str
    wagon_type: str
    loaded: bool
    commodity: str
    length_m: float
    axle_load_t: float
    hops: int
    observed_transit_min: float

    @property
    def dir_enum(self) -> Direction:
        return Direction.DOWN if self.direction == "DN" else Direction.UP

    @property
    def end_to_end(self) -> bool:
        return self.hops >= 19        # of 21 links

    def to_train_plan(self) -> TrainPlan:
        """An empty plan for this rake; the optimiser fills in the movements."""
        return TrainPlan(id=self.id, train_class="FREIGHT",
                         direction=self.dir_enum, rake_length_m=self.length_m,
                         priority=FREIGHT_PRIORITY,
                         wagon_type=self.wagon_type, loaded=self.loaded)

    @staticmethod
    def from_dict(d: dict) -> "Rake":
        return Rake(**{k: d[k] for k in Rake.__annotations__ if k in d})


@dataclass
class DayDemand:
    """Every rake presenting on one day, in the order they arrived."""
    day: str
    rakes: list[Rake]

    def __len__(self) -> int:
        return len(self.rakes)

    def __iter__(self):
        return iter(self.rakes)

    def between(self, from_min: float, to_min: float) -> "DayDemand":
        return DayDemand(self.day, [r for r in self.rakes
                                    if from_min <= r.ready_min <= to_min])

    def direction(self, d: str | Direction) -> "DayDemand":
        want = d.value if isinstance(d, Direction) else d
        want = "DN" if want in ("DN", "DOWN") else "UP"
        return DayDemand(self.day, [r for r in self.rakes if r.direction == want])

    def end_to_end(self) -> "DayDemand":
        return DayDemand(self.day, [r for r in self.rakes if r.end_to_end])

    def to_train_plans(self) -> list[TrainPlan]:
        return [r.to_train_plan() for r in self.rakes]

    def summary(self) -> dict:
        if not self.rakes:
            return {"day": self.day, "rakes": 0}
        lengths = [r.length_m for r in self.rakes]
        return {
            "day": self.day,
            "rakes": len(self.rakes),
            "down": sum(1 for r in self.rakes if r.direction == "DN"),
            "up": sum(1 for r in self.rakes if r.direction == "UP"),
            "loaded_pct": round(100 * sum(r.loaded for r in self.rakes) / len(self.rakes)),
            "end_to_end": sum(1 for r in self.rakes if r.end_to_end),
            "longest_rake_m": max(lengths),
            "median_rake_m": statistics.median(lengths),
            "first_ready": round(min(r.ready_min for r in self.rakes)),
            "last_ready": round(max(r.ready_min for r in self.rakes)),
        }

    def __repr__(self) -> str:
        s = self.summary()
        return (f"<DayDemand {self.day}: {s.get('rakes', 0)} rakes, "
                f"{s.get('down', 0)} down / {s.get('up', 0)} up>")


class DemandBook:
    """Every day of extracted demand, plus the calibration distributions."""

    def __init__(self, blob: dict):
        self.corridor: str = blob.get("corridor", "")
        self.source: str = blob.get("source", "")
        self.calibration: dict = blob.get("calibration", {})
        self.rake_table: dict = blob.get("rake_table", {})
        self._days: dict[str, DayDemand] = {
            day: DayDemand(day, [Rake.from_dict(r) for r in rows])
            for day, rows in blob.get("days", {}).items()
        }

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "DemandBook":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"{p} not found - run: python analysis/build_freight_demand.py")
        return cls(json.loads(p.read_text(encoding="utf-8")))

    @property
    def days(self) -> list[str]:
        return sorted(self._days)

    def day(self, which: str) -> DayDemand:
        try:
            return self._days[which]
        except KeyError:
            raise KeyError(f"no demand for {which}; have {self.days[0]}..{self.days[-1]}") from None

    def busiest(self) -> DayDemand:
        return max(self._days.values(), key=len)

    def median_day(self) -> DayDemand:
        ordered = sorted(self._days.values(), key=len)
        return ordered[len(ordered) // 2]

    def all_rakes(self) -> list[Rake]:
        return [r for d in self._days.values() for r in d.rakes]

    def __repr__(self) -> str:
        n = sum(len(d) for d in self._days.values())
        return (f"<DemandBook {self.corridor}: {n} rakes over "
                f"{len(self._days)} days>")
