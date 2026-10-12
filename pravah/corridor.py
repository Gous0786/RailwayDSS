"""
Linear referencing for the railway corridor.

Everything that draws a train on a map goes through here. The optimiser and
the simulator both reason in official chainage - "this train is 43.2 km along
the section" - and this module turns that into a geographic position.

    from pravah.corridor import Corridor

    c = Corridor.load()
    c.position(43.2)              -> (18.2614, 83.5107)
    c.bearing(43.2)               -> 41.3        degrees, for rotating an icon
    c.between("KTV", "VZM")       -> [(lat, lon), ...]   geometry for one leg
    c.locate(18.26, 83.51)        -> 43.07       nearest chainage to a point

Chainage is always the official ICMS value, never the OSM measurement. The
mapping between the two is fitted in analysis/build_linref.py.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "osm" / "linref.json"
EARTH_KM = 6371.0088


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    cum_km: float
    lat: float
    lon: float
    re_anchored: bool = False


class Corridor:
    """A railway section addressable by chainage."""

    def __init__(self, blob: dict):
        self.name: str = blob["corridor"]
        self.official_km: float = blob["official_km"]
        self.osm_km: float = blob["osm_km"]
        self.attribution: str = blob.get("attribution", "© OpenStreetMap contributors")
        self._pts: list[tuple[float, float]] = [(lat, lon) for lon, lat in blob["polyline"]]
        self._cum: list[float] = blob["cum_osm_km"]
        self._ctrl: list[tuple[float, float]] = [tuple(c) for c in blob["control_points"]]
        self.stations: list[Station] = [
            Station(s["code"], s["name"], s["cum_km"], s["lat"], s["lon"],
                    s.get("re_anchored", False))
            for s in blob["stations"]
        ]
        self._by_code = {s.code: s for s in self.stations}

    # ---------------------------------------------------------------- load
    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "Corridor":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"{p} not found - run: python analysis/build_linref.py")
        return cls(json.loads(p.read_text(encoding="utf-8")))

    # ------------------------------------------------------- chainage math
    def _to_osm_km(self, cum_km: float) -> float:
        """Map official chainage onto distance along the OSM polyline."""
        ctrl = self._ctrl
        if cum_km <= ctrl[0][0]:
            return ctrl[0][1]
        if cum_km >= ctrl[-1][0]:
            return ctrl[-1][1]
        i = bisect_right([k for k, _ in ctrl], cum_km) - 1
        k1, a1 = ctrl[i]
        k2, a2 = ctrl[min(i + 1, len(ctrl) - 1)]
        if k2 == k1:
            return a1
        return a1 + (cum_km - k1) / (k2 - k1) * (a2 - a1)

    def _vertex_at(self, osm_km: float) -> tuple[int, float]:
        """Return (index of vertex before osm_km, interpolation fraction)."""
        if osm_km <= 0:
            return 0, 0.0
        if osm_km >= self._cum[-1]:
            return len(self._pts) - 2, 1.0
        i = bisect_right(self._cum, osm_km) - 1
        i = max(0, min(i, len(self._pts) - 2))
        span = self._cum[i + 1] - self._cum[i]
        return i, (osm_km - self._cum[i]) / span if span > 0 else 0.0

    # ------------------------------------------------------------- queries
    def position(self, cum_km: float) -> tuple[float, float]:
        """Geographic position at an official chainage."""
        i, t = self._vertex_at(self._to_osm_km(cum_km))
        (la1, lo1), (la2, lo2) = self._pts[i], self._pts[i + 1]
        return (la1 + t * (la2 - la1), lo1 + t * (lo2 - lo1))

    def bearing(self, cum_km: float, lookahead_km: float = 0.2) -> float:
        """Compass bearing of the track, for orienting a train icon."""
        a = self.position(max(0.0, cum_km - lookahead_km))
        b = self.position(min(self.official_km, cum_km + lookahead_km))
        lat1, lon1 = math.radians(a[0]), math.radians(a[1])
        lat2, lon2 = math.radians(b[0]), math.radians(b[1])
        dl = lon2 - lon1
        y = math.sin(dl) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def station(self, code: str) -> Station:
        try:
            return self._by_code[code.upper()]
        except KeyError:
            raise KeyError(f"no station {code!r} on {self.name}") from None

    def between(self, from_code: str, to_code: str) -> list[tuple[float, float]]:
        """Track geometry between two stations, in travel order."""
        a, b = self.station(from_code), self.station(to_code)
        lo, hi = sorted((self._to_osm_km(a.cum_km), self._to_osm_km(b.cum_km)))
        i0, _ = self._vertex_at(lo)
        i1, _ = self._vertex_at(hi)
        pts = [self.position(a.cum_km)] + self._pts[i0 + 1:i1 + 1] + [self.position(b.cum_km)]
        return pts if a.cum_km <= b.cum_km else pts[::-1]

    def locate(self, lat: float, lon: float) -> float:
        """Nearest official chainage to a geographic point."""
        best_i, best_d = 0, float("inf")
        for i, p in enumerate(self._pts):
            d = haversine((lat, lon), p)
            if d < best_d:
                best_i, best_d = i, d
        osm_km = self._cum[best_i]
        # invert the control-point map
        ctrl = self._ctrl
        if osm_km <= ctrl[0][1]:
            return ctrl[0][0]
        if osm_km >= ctrl[-1][1]:
            return ctrl[-1][0]
        for (k1, a1), (k2, a2) in zip(ctrl, ctrl[1:]):
            if a1 <= osm_km <= a2:
                t = (osm_km - a1) / (a2 - a1) if a2 > a1 else 0.0
                return k1 + t * (k2 - k1)
        return ctrl[-1][0]

    def segment_for(self, cum_km: float) -> tuple[Station, Station] | None:
        """Which inter-station block a chainage falls in."""
        for a, b in zip(self.stations, self.stations[1:]):
            if a.cum_km <= cum_km <= b.cum_km:
                return a, b
        return None

    def __repr__(self) -> str:
        return (f"<Corridor {self.name} {self.official_km:.2f} km, "
                f"{len(self.stations)} stations, {len(self._pts)} vertices>")


@lru_cache(maxsize=1)
def default() -> Corridor:
    """Process-wide shared instance."""
    return Corridor.load()
