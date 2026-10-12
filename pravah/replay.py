"""
Simulation clock and train-state source.

The corridor view is driven by a server-authoritative clock rather than by each
browser interpolating on its own. That matters for a control-room tool: every
client sees the same instant, and the server is the single place the optimiser
will later write its plan into.

The state this produces is shaped like a live feed would be, so a real COA or
FOIS source can replace ReplaySource without the clock, the socket or the
viewer changing:

    {"t": 21600, "trains": [{"id": "12841", "type": "SUF", "dir": "UP",
                             "km": 43.2, "speed": 61.0, "status": "running"}]}
"""

from __future__ import annotations

import json
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REPLAY = Path(__file__).resolve().parent.parent / "web" / "data" / "replay.json"

# A train sitting on the same chainage between two timetable points is dwelling
# at a station rather than running.
DWELL_SPEED_KMPH = 0.5


@dataclass
class TrainPath:
    """One service, as chainage over time."""
    id: str
    type: str
    dir: str
    pts: list[list[float]]
    _ts: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._ts = [p[0] for p in self.pts]

    @property
    def enters(self) -> float:
        return self.pts[0][0]

    @property
    def clears(self) -> float:
        return self.pts[-1][0]

    def at(self, t: float) -> tuple[float, float] | None:
        """Chainage and speed at time t, or None if not in the section."""
        if t < self._ts[0] or t > self._ts[-1]:
            return None
        i = bisect_right(self._ts, t) - 1
        i = max(0, min(i, len(self.pts) - 2))
        t0, k0 = self.pts[i]
        t1, k1 = self.pts[i + 1]
        dt = t1 - t0
        if dt <= 0:
            return k0, 0.0
        f = (t - t0) / dt
        return k0 + f * (k1 - k0), abs(k1 - k0) / (dt / 3600.0)


class ReplaySource:
    """Train states replayed from recorded timetable data."""

    kind = "replay"

    def __init__(self, blob: dict):
        self.day: str = blob.get("day", "")
        self.corridor: str = blob.get("corridor", "")
        self.span: list[float] = blob.get("time_span", [0, 86400])
        self.paths = [TrainPath(t["id"], t["type"], t["dir"], t["pts"])
                      for t in blob["trains"]]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_REPLAY) -> "ReplaySource":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"{p} not found - run: python analysis/build_replay.py")
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def state_at(self, t: float) -> list[dict]:
        out = []
        for p in self.paths:
            r = p.at(t)
            if r is None:
                continue
            km, speed = r
            out.append({
                "id": p.id, "type": p.type, "dir": p.dir,
                "km": round(km, 3), "speed": round(speed, 1),
                "status": "dwell" if speed < DWELL_SPEED_KMPH else "running",
            })
        return out

    def __repr__(self) -> str:
        return (f"<ReplaySource {self.corridor} {self.day} "
                f"{len(self.paths)} services>")


class SimClock:
    """Wall-clock-driven simulation time with play, pause, seek and speed."""

    def __init__(self, start: float, span: list[float], speed: float = 300.0):
        self.span = span
        self.speed = speed
        self.playing = False
        self._t = float(start)
        self._last = time.monotonic()

    @property
    def t(self) -> float:
        self._advance()
        return self._t

    def _advance(self) -> None:
        now = time.monotonic()
        if self.playing:
            self._t += (now - self._last) * self.speed
            if self._t > self.span[1]:
                self._t = self.span[0]          # loop the day
        self._last = now

    def play(self) -> None:
        self._advance()
        self.playing = True

    def pause(self) -> None:
        self._advance()
        self.playing = False

    def seek(self, t: float) -> None:
        self._advance()
        self._t = max(self.span[0], min(float(t), self.span[1]))

    def set_speed(self, s: float) -> None:
        self._advance()
        self.speed = max(1.0, min(float(s), 3600.0))

    def snapshot(self) -> dict:
        return {"t": round(self.t, 1), "playing": self.playing,
                "speed": self.speed, "span": self.span}
