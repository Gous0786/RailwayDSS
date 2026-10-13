"""
Operational model of the railway section.

This is the object the optimiser and the verification simulator both consume.
It is deliberately separate from pravah.corridor, which answers a geographic
question ("where is chainage 43.2 km on a map"). This answers an operational
one: which resources exist, who may occupy them, for how long, and when.

Modelling notes that matter
---------------------------
A link between two adjacent stations appears twice in the ICMS extract, once
per direction (KTV-KPL as DN, KPL-KTV as UP), but it is one physical link
carrying shared running lines - typically DN, UP and sometimes MID. The
resource is therefore the LINE, not the direction. That distinction is what
makes single-line working expressible: close the DN line and down trains must
share the UP line with opposing traffic, which is precisely when crossings
have to be decided.

Only stations flagged with precedence/crossing facility can hold a train to
let another pass. On this corridor 19 of 22 qualify, so the three that do not
are hard constraints on where trains may be held.

Usage
-----
    from pravah.section import SectionGraph

    g = SectionGraph.load()
    g.running_time("KTV-KPL", "DN", "SUF")       -> minutes
    g.headway("KTV-KPL", "DN")                   -> minutes
    g.holding_lines("VZM", rake_length_m=650)    -> usable loops/platforms
    g.close("KTV-KPL:DN", 480, 600, "engineering block")
    g.is_single_line("KTV-KPL", at=500)          -> True

Run directly for a summary:  python -m pravah.section
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "ktv-psa"
INFRA = DATA / "KTV-PSA-Infra"
SCHED = DATA / "KTV_PSA_Passenger_Schedule"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

# --- operating constants --------------------------------------------------
# Signal spacing where the extract records no signal count on an automatic
# block. 3.6 km is the conventional minimum spacing used on this division.
DEFAULT_AUTO_SPACING_KM = 3.6
BRAKING_DISTANCE_KM = 1.0
# Absolute block: the section must be proved clear before the next admission.
ABSOLUTE_CLEARANCE_MIN = 2.0
# Allowances applied when a train actually stops rather than running through.
ACCEL_ALLOWANCE_MIN = 1.5
DECEL_ALLOWANCE_MIN = 1.0

# A line length of 1 is a placeholder in the extract, not a real measurement.
PLACEHOLDER_LENGTH_M = 2

# Train precedence. Lower number takes precedence. Configurable: these follow
# conventional Indian Railways ordering, with freight last.
PRIORITY: dict[str, int] = {
    "VNDB": 1,   # Vande Bharat
    "DRNT": 2,   # Duronto
    "GBR": 3,    # Garib Rath
    "AMTB": 3,   # Amrit Bharat
    "SUF": 4,    # Superfast
    "MEX": 5,    # Mail / Express
    "PEXP": 6,
    "PAS": 7,    # Passenger
    "MEMU": 8,
    "TOD": 9,
    "TRST": 10,
    "TRSF": 10,
    "FREIGHT": 12,
}
DEFAULT_PRIORITY = 9

# Maximum planning speed by train class, km/h.
#
# These are not assumptions: they are the 90th percentile of point-to-point
# speeds actually achieved by each class in the published schedule, over 4,260
# timing records, capped at the 130 km/h line speed. Using the 90th percentile
# rather than the maximum avoids the short-link rounding artefacts that imply
# 160 km/h on a 7 km block timed to the minute.
#
# Freight is the measured median running speed from 15,449 goods movements on
# this corridor, not a permitted maximum: a loaded rake is cleared for 75 km/h
# but does not achieve it. The learned estimator will replace this per block.
CLASS_MAX_SPEED = {
    "VNDB": 125, "DRNT": 120, "GBR": 110, "AMTB": 120, "SUF": 125,
    "MEX": 120, "PEXP": 105, "PAS": 80, "MEMU": 75, "TOD": 110,
    "TRST": 105, "TRSF": 110,
    "FREIGHT": 31,
}
DEFAULT_CLASS_SPEED = 90


class LineCategory(str, Enum):
    MAIN = "MAIN"
    LOOP = "LOOP"
    SIDING = "SIDING"


class Signalling(str, Enum):
    ABSOLUTE = "AB"
    AUTOMATIC = "AT"


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DN"
    BOTH = "BOTH"


# ---------------------------------------------------------------- entities
@dataclass(frozen=True)
class StationLine:
    """A running line inside a station: platform, loop or siding."""
    station: str
    number: str
    name: str
    category: LineCategory
    length_m: float | None          # None where the extract has a placeholder
    direction: Direction
    speed_kmph: float
    is_platform: bool

    @property
    def id(self) -> str:
        return f"{self.station}:{self.number}"

    def fits(self, rake_length_m: float) -> bool:
        """Can a rake of this length stand here?"""
        if self.length_m is None:
            return False            # unknown length: never assume it fits
        return self.length_m >= rake_length_m

    def serves(self, direction: Direction) -> bool:
        return self.direction in (Direction.BOTH, direction)


@dataclass
class Station:
    code: str
    name: str
    cum_km: float
    is_junction: bool
    can_cross: bool                 # precedence / crossing facility
    lines: list[StationLine] = field(default_factory=list)

    def holding_lines(self, rake_length_m: float,
                      direction: Direction | None = None) -> list[StationLine]:
        """Lines where a rake of this length may be held to let another pass."""
        if not self.can_cross:
            return []
        # A loop is always available for holding. A main or platform line may
        # only be used if the station has a third line, so a through path
        # remains open - otherwise holding here blocks the section.
        allow_main = len(self.lines) >= 3
        out = [ln for ln in self.lines
               if (ln.category is LineCategory.LOOP
                   or (allow_main and ln.category is LineCategory.MAIN))
               and ln.fits(rake_length_m)
               and (direction is None or ln.serves(direction))]
        # prefer a loop, so the running line stays clear
        return sorted(out, key=lambda ln: (ln.category != LineCategory.LOOP,
                                           ln.length_m or 0))

    @property
    def platform_count(self) -> int:
        return sum(1 for ln in self.lines if ln.is_platform)


@dataclass(frozen=True)
class TrackLine:
    """One running line within a link between adjacent stations."""
    link: str                       # canonical "A-B", stations alphabetical
    name: str                       # DN, UP, MID, KK ...
    length_km: float
    signalling: Signalling
    max_speed_kmph: float
    n_signals: int

    @property
    def id(self) -> str:
        return f"{self.link}:{self.name}"

    @property
    def nominal_direction(self) -> Direction:
        if self.name == "UP":
            return Direction.UP
        if self.name == "DN":
            return Direction.DOWN
        return Direction.BOTH       # MID and others are reversible


@dataclass
class Link:
    """The physical connection between two adjacent stations."""
    a: str
    b: str
    length_km: float
    lines: list[TrackLine] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.a}-{self.b}"

    def usable(self, direction: Direction, closed: set[str]) -> list[TrackLine]:
        """Lines a train in this direction may take, given closures.

        Normally a train keeps to its own line. When that line is closed the
        remaining lines are worked in both directions - single-line working -
        and opposing movements must then be crossed at a station.
        """
        open_lines = [ln for ln in self.lines if ln.id not in closed]
        own = [ln for ln in open_lines
               if ln.nominal_direction in (direction, Direction.BOTH)]
        return own or open_lines


@dataclass(frozen=True)
class SpeedRestriction:
    link: str
    pass_kmph: float
    goods_kmph: float
    loss_pass_s: float
    loss_goods_s: float


@dataclass(frozen=True)
class Closure:
    """A line withdrawn from traffic: engineering block, failure, or works."""
    resource: str                   # TrackLine.id or StationLine.id
    start_min: float
    end_min: float
    reason: str = ""

    def active(self, at: float) -> bool:
        return self.start_min <= at <= self.end_min


# ------------------------------------------------------------------ loader
def _rows(path: Path) -> list[dict]:
    """Read an ICMS extract, dropping the header row repeated mid-file."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)]
    if not rows:
        return []
    key = next(iter(rows[0]))
    return [r for r in rows if r.get(key) and r[key] != key]


def _num(v: str, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class SectionGraph:
    """Blocks, lines, capacities and timings for one railway section."""

    def __init__(self, stations: list[Station], links: dict[str, Link],
                 psr: dict[str, SpeedRestriction]):
        self.stations = stations
        self.links = links
        self.psr = psr
        self.closures: list[Closure] = []
        self._by_code = {s.code: s for s in stations}
        self._order = {s.code: i for i, s in enumerate(stations)}

    # -------------------------------------------------------------- build
    @classmethod
    def load(cls) -> "SectionGraph":
        route = _rows(ROUTE)
        order = [(r["STATION_CODE"].strip().upper(), float(r["CUM_DISTANCE"] or 0))
                 for r in route]

        meta = {r["MAVSTTNCODE"].strip().upper(): r for r in _rows(INFRA / "STATION-Table 1.csv")}
        stations: list[Station] = []
        for code, km in order:
            m = meta.get(code, {})
            stations.append(Station(
                code=code,
                name=(m.get("MAVSTTNNAME") or code).strip(),
                cum_km=km,
                is_junction=(m.get("MACJUNCTION", "").upper() == "Y"),
                can_cross=(m.get("MACPRCDCROSSFCLTY", "").upper() == "Y"),
            ))
        by_code = {s.code: s for s in stations}

        # station lines: platforms, loops, sidings
        cat_map = {"M": LineCategory.MAIN, "L": LineCategory.LOOP,
                   "S": LineCategory.SIDING}
        for r in _rows(INFRA / "STTNLINE-Table 1.csv"):
            code = r["MAVSTTNCODE"].strip().upper()
            if code not in by_code:
                continue
            raw_len = _num(r.get("MANLINELENGTH"), None)
            by_code[code].lines.append(StationLine(
                station=code,
                number=r.get("MAVLINENUMB", "").strip(),
                name=(r.get("MAVLINENAME") or r.get("MAVLINENUMB") or "").strip(),
                category=cat_map.get(r.get("MACLINECATEGORY", "").upper(),
                                     LineCategory.MAIN),
                length_m=raw_len if (raw_len or 0) > PLACEHOLDER_LENGTH_M else None,
                direction=Direction(r["MAVDRTN"].strip().upper())
                          if r.get("MAVDRTN", "").strip().upper() in
                             ("UP", "DN", "BOTH") else Direction.BOTH,
                speed_kmph=_num(r.get("MANLINESPEED"), 0.0) or 0.0,
                is_platform=(r.get("MACPLATFORM", "").upper() == "Y"),
            ))

        # Where STTNLINE carries a placeholder length, try the Platform table,
        # which records real lengths for some of the same lines.
        plat_len: dict[tuple[str, str], float] = {}
        pfile = INFRA / "Platform-Table 1.csv"
        if pfile.exists():
            for r in _rows(pfile):
                code = r.get("MAVSTTNCODE", "").strip().upper()
                num = (r.get("MAVPLATFORMNUMB") or "").strip().upper()
                ln = _num(r.get("MANLENGTH"), None)
                if code and num and ln:
                    key = num.replace("PF-", "").replace("PF", "")
                    plat_len[(code, key)] = ln
        filled = 0
        for st_ in stations:
            for i, ln_ in enumerate(st_.lines):
                if ln_.length_m is not None:
                    continue
                key = ln_.name.upper().replace("PF-", "").replace("PF", "")
                got = plat_len.get((st_.code, key)) or plat_len.get((st_.code, ln_.number))
                if got:
                    st_.lines[i] = StationLine(**{**ln_.__dict__, "length_m": got})
                    filled += 1
        if filled:
            print(f"[section] filled {filled} line length(s) from the Platform table")

        # links: fold the two directional rows into one physical link
        onroute = {c for c, _ in order}
        blocks = {r["MAVBLCKSCTN"]: r for r in _rows(INFRA / "BLOCKSCTN-Table 1.csv")}
        links: dict[str, Link] = {}
        canon: dict[str, str] = {}      # "KTV-KPL" and "KPL-KTV" -> one link id
        for a, b in zip([c for c, _ in order], [c for c, _ in order][1:]):
            lid = f"{a}-{b}"
            rec = blocks.get(f"{a}-{b}") or blocks.get(f"{b}-{a}") or {}
            links[lid] = Link(a=a, b=b,
                              length_km=_num(rec.get("MANINTRDIST"), 0.0) or 0.0)
            canon[f"{a}-{b}"] = lid
            canon[f"{b}-{a}"] = lid

        # running lines within each link (deduplicated: the export repeats)
        seen: set[tuple[str, str]] = set()
        for r in _rows(INFRA / "BLOCKSCTNLINE-Table 1.csv"):
            bsec = r.get("MAVBLCKSCTN", "").strip()
            lid = canon.get(bsec)
            if not lid:
                continue
            name = (r.get("MAVLINENUMB") or "").strip().upper()
            if not name or (lid, name) in seen:
                continue
            seen.add((lid, name))
            sig = r.get("MAVSGNLTYPE", "").strip().upper()
            links[lid].lines.append(TrackLine(
                link=lid, name=name,
                length_km=_num(r.get("MANLINELENGTH"), links[lid].length_km)
                          or links[lid].length_km,
                signalling=Signalling.AUTOMATIC if sig == "AT" else Signalling.ABSOLUTE,
                max_speed_kmph=_num(r.get("MANMAXSPEED"), 0.0) or 0.0,
                n_signals=int(_num(r.get("MANNUMBSGNL"), 0) or 0),
            ))

        # fall back to the block record where no line rows exist
        for lid, link in links.items():
            if link.lines:
                continue
            rec = blocks.get(f"{link.a}-{link.b}") or blocks.get(f"{link.b}-{link.a}") or {}
            sig = rec.get("MAVSGNLTYPE", "").strip().upper()
            n = int(_num(rec.get("MANNUMBLINES"), 2) or 2)
            names = ["DN", "UP", "MID", "KK"][:max(1, n)]
            for nm in names:
                link.lines.append(TrackLine(
                    link=lid, name=nm, length_km=link.length_km,
                    signalling=Signalling.AUTOMATIC if sig == "AT" else Signalling.ABSOLUTE,
                    max_speed_kmph=_num(rec.get("MANMAXSPEED"), 100.0) or 100.0,
                    n_signals=int(_num(rec.get("MANNUMBSGNL"), 0) or 0)))

        # Permanent speed restrictions are DIRECTIONAL: the two files cover
        # the down and up directions and list different block sections. They
        # are keyed by the directional code ("KUK-VZM" is not "VZM-KUK"),
        # because applying a down restriction to an up train overstated
        # VZM->KUK by 3.6 minutes.
        psr: dict[str, SpeedRestriction] = {}
        for fn in ("PSR (KTV-PSA)-Table 1.csv", "PSR (PSA-KTV)-Table 1.csv"):
            p = SCHED / fn
            if not p.exists():
                continue
            for r in _rows(p):
                code = r.get("MAVBLCKSCTN", "").strip().upper()
                if code not in canon:
                    continue
                lid = code
                cur = psr.get(lid)
                new = SpeedRestriction(
                    link=lid,
                    pass_kmph=_num(r.get("MANPASSPEED"), 999.0) or 999.0,
                    goods_kmph=_num(r.get("MANGDSSPEED"), 999.0) or 999.0,
                    loss_pass_s=_num(r.get("MANTIMELOSSPSGR"), 0.0) or 0.0,
                    loss_goods_s=_num(r.get("MANTIMELOSSGOODS"), 0.0) or 0.0)
                # keep the most restrictive
                if cur is None or new.goods_kmph < cur.goods_kmph:
                    psr[lid] = new
        return cls(stations, links, psr)

    # ------------------------------------------------------------ queries
    def station(self, code: str) -> Station:
        return self._by_code[code.upper()]

    def link(self, a: str, b: str) -> Link:
        a, b = a.upper(), b.upper()
        return self.links.get(f"{a}-{b}") or self.links[f"{b}-{a}"]

    def line(self, link_id: str, line_name: str) -> TrackLine:
        for ln in self.links[link_id].lines:
            if ln.name == line_name.upper():
                return ln
        raise KeyError(f"no line {line_name} on {link_id}")

    def direction_of(self, a: str, b: str) -> Direction:
        """DOWN runs KTV->PSA, the direction of increasing chainage."""
        return (Direction.DOWN if self._order[a.upper()] < self._order[b.upper()]
                else Direction.UP)

    def is_freight(self, train_class: str) -> bool:
        return train_class.upper() in ("FREIGHT", "GOODS")

    def priority(self, train_class: str) -> int:
        return PRIORITY.get(train_class.upper(), DEFAULT_PRIORITY)

    def psr_for(self, link_id: str, direction: Direction) -> SpeedRestriction | None:
        """The restriction applying to travel in this direction, if any."""
        link = self.links[link_id]
        code = (f"{link.a}-{link.b}" if direction is Direction.DOWN
                else f"{link.b}-{link.a}")
        return self.psr.get(code)

    def running_time(self, link_id: str, line_name: str, train_class: str,
                     direction: Direction = Direction.DOWN,
                     stops: bool = False) -> float:
        """Minutes to traverse a line, honouring directional restrictions."""
        ln = self.line(link_id, line_name)
        freight = self.is_freight(train_class)
        speed = max(5.0, min(ln.max_speed_kmph or 999.0,
                             CLASS_MAX_SPEED.get(train_class.upper(),
                                                 DEFAULT_CLASS_SPEED)))
        minutes = ln.length_km / speed * 60.0
        # A permanent speed restriction usually covers a short feature - the
        # KUK-VZM one is a single diamond crossing - so applying its speed to
        # the whole block is wrong. The extract gives the resulting penalty
        # directly, separately for passenger and goods, and that is what to
        # add. Applying both the reduced speed and the penalty double-counts:
        # it made KUK-VZM 48 minutes against about 9 in the real timetable.
        r = self.psr_for(link_id, direction)
        if r:
            minutes += (r.loss_goods_s if freight else r.loss_pass_s) / 60.0
        if stops:
            minutes += ACCEL_ALLOWANCE_MIN + DECEL_ALLOWANCE_MIN
        return minutes

    def headway(self, link_id: str, line_name: str) -> float:
        """Minimum separation between successive trains entering a line."""
        ln = self.line(link_id, line_name)
        speed = max(5.0, ln.max_speed_kmph or 100.0)
        if ln.signalling is Signalling.AUTOMATIC:
            spacing = (ln.length_km / (ln.n_signals + 1) if ln.n_signals > 0
                       else DEFAULT_AUTO_SPACING_KM)
            return (min(spacing, ln.length_km) + BRAKING_DISTANCE_KM) / speed * 60.0
        # absolute block: the whole section must clear first
        return ln.length_km / speed * 60.0 + ABSOLUTE_CLEARANCE_MIN

    def holding_lines(self, station: str, rake_length_m: float,
                      direction: Direction | None = None,
                      at: float | None = None) -> list[StationLine]:
        lines = self.station(station).holding_lines(rake_length_m, direction)
        if at is None:
            return lines
        closed = self.closed_at(at)
        return [ln for ln in lines if ln.id not in closed]

    # ----------------------------------------------------------- closures
    def close(self, resource: str, start_min: float, end_min: float,
              reason: str = "") -> Closure:
        """Withdraw a line from traffic - engineering block, failure or works."""
        c = Closure(resource, start_min, end_min, reason)
        self.closures.append(c)
        return c

    def closed_at(self, at: float) -> set[str]:
        return {c.resource for c in self.closures if c.active(at)}

    def usable_lines(self, link_id: str, direction: Direction,
                     at: float = 0.0) -> list[TrackLine]:
        return self.links[link_id].usable(direction, self.closed_at(at))

    def open_lines(self, link_id: str, at: float = 0.0) -> list[TrackLine]:
        closed = self.closed_at(at)
        return [ln for ln in self.links[link_id].lines if ln.id not in closed]

    def is_single_line(self, link_id: str, at: float = 0.0) -> bool:
        """Exactly one line open, so opposing trains must be crossed.

        Not the same as blocked: zero open lines means no traffic at all.
        """
        return len(self.open_lines(link_id, at)) == 1

    def is_blocked(self, link_id: str, at: float = 0.0) -> bool:
        """No line open - the link cannot be traversed."""
        return not self.open_lines(link_id, at)

    def crossing_stations(self) -> list[Station]:
        """Where opposing trains can be crossed under single-line working."""
        return [s for s in self.stations if s.can_cross]

    # -------------------------------------------------------------- repr
    def __repr__(self) -> str:
        nlines = sum(len(l.lines) for l in self.links.values())
        return (f"<SectionGraph {self.stations[0].code}-{self.stations[-1].code} "
                f"{self.stations[-1].cum_km:.2f} km, {len(self.stations)} stations, "
                f"{len(self.links)} links, {nlines} running lines>")


def main() -> None:
    g = SectionGraph.load()
    print("=" * 74)
    print("SECTION GRAPH")
    print("=" * 74)
    print(f"\n{g!r}\n")

    nocross = [s.code for s in g.stations if not s.can_cross]
    print(f"crossing/precedence facility : {len(g.crossing_stations())}/{len(g.stations)}"
          f"   without: {nocross}")
    plats = sum(s.platform_count for s in g.stations)
    loops = sum(1 for s in g.stations for ln in s.lines
                if ln.category is LineCategory.LOOP)
    unknown = sum(1 for s in g.stations for ln in s.lines if ln.length_m is None)
    print(f"station lines                : {sum(len(s.lines) for s in g.stations)} "
          f"({loops} loops, {plats} platforms, {unknown} without a usable length)")
    sig = {}
    for l in g.links.values():
        for ln in l.lines:
            sig[ln.signalling.value] = sig.get(ln.signalling.value, 0) + 1
    print(f"running lines by signalling  : {sig}")
    print(f"speed restrictions applied   : {len(g.psr)} links")

    print("\nwhere a 650 m freight rake can be held:")
    tot = 0
    for s in g.stations:
        n = len(s.holding_lines(650))
        tot += n
        mark = "" if n else "   <-- cannot hold"
        print(f"  {s.code:<5} {s.name[:22]:<22} {n} line(s){mark}")
    print(f"  total holding opportunities: {tot}")

    print("\nrunning time and headway, sample links:")
    print(f"  {'link':<12}{'line':<5}{'km':>6}{'sig':>5}{'SUF':>7}{'FRT':>7}{'headway':>9}")
    for lid in list(g.links)[:6]:
        for ln in g.links[lid].lines[:1]:
            print(f"  {lid:<12}{ln.name:<5}{ln.length_km:6.2f}{ln.signalling.value:>5}"
                  f"{g.running_time(lid, ln.name, 'SUF'):7.1f}"
                  f"{g.running_time(lid, ln.name, 'FREIGHT'):7.1f}"
                  f"{g.headway(lid, ln.name):9.1f}")

    print("\nsingle-line working (the condition that forces crossings):")
    lid = "KTV-KPL"
    print(f"  {lid} normally: {[ln.name for ln in g.links[lid].lines]}"
          f"  single-line={g.is_single_line(lid)}")
    for nm in [ln.name for ln in g.links[lid].lines][:-1]:
        g.close(f"{lid}:{nm}", 0, 1440, "engineering block")
    print(f"  after closing all but one: single-line={g.is_single_line(lid, 600)}")
    print(f"  down trains must now use : "
          f"{[ln.name for ln in g.usable_lines(lid, Direction.DOWN, 600)]}")
    print("=" * 74)


if __name__ == "__main__":
    main()
