"""
Build the KTV-PSA corridor basemap from cached OSM geometry.

The difficulty is selecting which of the ~460 rail ways in the bounding box
belong to this corridor. Two earlier approaches failed:

  shortest-path search between consecutive stations
      On double track the up and down lines are separate ways joined only at
      distant crossovers, so a walk detoured hundreds of kilometres.

  buffering around the straight line between stations
      The real track bows away from the chord - the median rail node sits
      1.29 km off it - so any way lying entirely beyond the buffer was dropped,
      leaving visible breaks between widely-spaced stations. Widening the
      buffer instead swallowed the branch lines at the junctions.

What works is iterative refinement. Start from the straight station chain,
select generously, build a centreline from what survives, then reselect
against that centreline with a tighter buffer and repeat. The reference
converges onto the real alignment, after which a tight buffer keeps parallel
tracks and station loops while rejecting branches.

Display geometry is clipped rather than filtered: a way is cut into the runs
of consecutive nodes lying within the buffer. A branch therefore shows only
its turnout stub at the junction, which is what is actually there.

Outputs
  data/osm/corridor_centreline.geojson   one LineString per inter-station leg
  data/osm/corridor_tracks.geojson       running lines clipped to the corridor

Usage:  python analysis/build_corridor.py
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ktv-psa"
OSM = ROOT / "data" / "osm"
CACHE = OSM / "_cache"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

# OSM names this corridor, which is a far better anchor than any geometric
# heuristic: these ways pass within 110 m of all 22 stations, and the branch
# lines at the junctions do not carry the name. The reference is built from
# them alone; the drawn track layer still uses every way near the result, so
# parallel tracks and station loops are kept.
MAINLINE_NAME = "Howrah - Chennai Main Line"

# Buffer per refinement pass, narrowing onto the real alignment.
PASSES = [2.5, 0.8, 0.45]
TRACK_BUFFER_KM = 0.40      # for the drawn track layer
BIN_KM = 0.04               # centreline resampling interval
OFFICIAL_KM = 176.83


def haversine(a, b) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class Reference:
    """A polyline that points can be projected onto, with a grid index."""

    def __init__(self, pts: list[tuple[float, float]], lat0: float):
        self.pts = pts
        self.lat0 = lat0
        self.kx = 111.320 * math.cos(math.radians(lat0))
        self.ky = 110.574
        self.xy = [(lon * self.kx, lat * self.ky) for lat, lon in pts]
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + haversine(a, b))
        # bucket segment indices by km cell so projection is not O(n*m)
        self.cell = 1.0
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in range(len(self.xy) - 1):
            (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
            for cx in range(int(min(x1, x2) // self.cell),
                            int(max(x1, x2) // self.cell) + 1):
                for cy in range(int(min(y1, y2) // self.cell),
                                int(max(y1, y2) // self.cell) + 1):
                    self.grid[(cx, cy)].append(i)

    @property
    def length(self) -> float:
        return self.cum[-1]

    def project(self, pt: tuple[float, float], max_off: float):
        """Return (along_km, offset_km), or None if further than max_off."""
        px, py = pt[1] * self.kx, pt[0] * self.ky
        reach = int(max_off // self.cell) + 1
        cx0, cy0 = int(px // self.cell), int(py // self.cell)
        best = None
        seen: set[int] = set()
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for i in self.grid.get((cx0 + dx, cy0 + dy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
                    ddx, ddy = bx - ax, by - ay
                    L2 = ddx * ddx + ddy * ddy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0,
                        ((px - ax) * ddx + (py - ay) * ddy) / L2))
                    off = math.hypot(px - (ax + t * ddx), py - (ay + t * ddy))
                    if best is None or off < best[1]:
                        best = (self.cum[i] + t * (self.cum[i + 1] - self.cum[i]), off)
        if best is None or best[1] > max_off:
            return None
        return best


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)]


def load_ways() -> list[dict]:
    ways: dict[int, dict] = {}
    for b in sorted(CACHE.glob("ways_band*.json")):
        for el in json.loads(b.read_text(encoding="utf-8")).get("elements", []):
            if el.get("geometry"):
                ways[el["id"]] = el
    if not ways:
        raise SystemExit("no cached OSM ways - run analysis/check_osm_coverage.py first")
    return list(ways.values())


def build_reference(ways, ref: Reference, buffer_km: float, lat0: float) -> Reference:
    """Reselect nodes near `ref` and resample them into a new centreline."""
    bins: dict[int, tuple[float, tuple[float, float]]] = {}
    for w in ways:
        for g in w["geometry"]:
            p = (g["lat"], g["lon"])
            r = ref.project(p, buffer_km)
            if r is None:
                continue
            along, off = r
            b = int(along / BIN_KM)
            if b not in bins or off < bins[b][0]:
                bins[b] = (off, p)
    pts = [bins[b][1] for b in sorted(bins)]
    thinned: list[tuple[float, float]] = []
    for p in pts:
        if not thinned or haversine(thinned[-1], p) > 0.015:
            thinned.append(p)
    return Reference(thinned, lat0)


def main() -> None:
    route = [(r["STATION_CODE"].strip().upper(), r["STATION_NAME"].strip(),
              float(r["CUM_DISTANCE"] or 0)) for r in read_csv(ROUTE)]
    match = json.loads((OSM / "station_match.json").read_text(encoding="utf-8"))
    stpos = {m["code"]: (m["osm"]["lat"], m["osm"]["lon"])
             for m in match["matched"] if m.get("osm", {}).get("lat") is not None}
    ways = load_ways()

    print("=" * 72)
    print("CORRIDOR BASEMAP - iterative reference refinement")
    print("=" * 72)
    print(f"\ncached ways {len(ways)}   stations located {len(stpos)}/{len(route)}")

    spine = [w for w in ways if (w.get("tags") or {}).get("name") == MAINLINE_NAME]
    if spine:
        print(f'reference built from {len(spine)} ways named "{MAINLINE_NAME}"')
    else:
        spine = ways
        print("WARNING: main line name not found; falling back to all ways")

    lat0 = sum(p[0] for p in stpos.values()) / len(stpos)
    ref = Reference([stpos[c] for c, _, _ in route if c in stpos], lat0)
    print(f"\npass 0  straight station chain   {ref.length:7.2f} km, "
          f"{len(ref.pts):5d} vertices")
    for i, buf in enumerate(PASSES, 1):
        ref = build_reference(spine, ref, buf, lat0)
        print(f"pass {i}  buffer {buf:4.2f} km             {ref.length:7.2f} km, "
              f"{len(ref.pts):5d} vertices")

    # --- anchor stations onto the converged reference ---------------------
    anchors: dict[str, float] = {}
    for code, _, _ in route:
        if code in stpos:
            r = ref.project(stpos[code], 5.0)
            if r:
                anchors[code] = r[0]
    order_ok = all(anchors[a] <= anchors[b] + 1e-6
                   for (a, _, _), (b, _, _) in zip(route, route[1:])
                   if a in anchors and b in anchors)
    print(f"\nstations anchored {len(anchors)}/{len(route)}   monotonic: {order_ok}")

    # --- split the reference at the stations ------------------------------
    print("\nsegment validation (OSM centreline vs official ICMS distance):")
    feats, total, flagged = [], 0.0, []
    for (c1, _, k1), (c2, _, k2) in zip(route, route[1:]):
        official = k2 - k1
        if c1 not in anchors or c2 not in anchors:
            flagged.append(f"{c1}-{c2} (not anchored)")
            continue
        a0, a1 = anchors[c1], anchors[c2]
        line = [stpos[c1]]
        line += [p for p, cm in zip(ref.pts, ref.cum) if a0 < cm < a1]
        line.append(stpos[c2])
        d = sum(haversine(x, y) for x, y in zip(line, line[1:]))
        total += d
        err = abs(d - official) / official * 100 if official else 0.0
        flag = "   <-- check" if err >= 12 else ""
        if err >= 12:
            flagged.append(f"{c1}-{c2}")
        print(f"  {c1:>5} -> {c2:<5}   osm {d:6.2f} km   official {official:6.2f} km"
              f"   {err:4.1f}%{flag}")
        feats.append({"type": "Feature",
                      "properties": {"from": c1, "to": c2, "osm_km": round(d, 3),
                                     "official_km": round(official, 3),
                                     "err_pct": round(err, 1),
                                     "from_cum_km": k1, "to_cum_km": k2},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[lo, la] for la, lo in line]}})

    # --- clip running lines to the corridor -------------------------------
    print(f"\nclipping track geometry to {TRACK_BUFFER_KM} km of the centreline ...")
    track_feats = []
    kept_ways = 0
    for w in ways:
        pts = [(g["lat"], g["lon"]) for g in w["geometry"]]
        inside = [ref.project(p, TRACK_BUFFER_KM) is not None for p in pts]
        if not any(inside):
            continue
        kept_ways += 1
        tags = w.get("tags") or {}
        props = {k: tags[k] for k in ("usage", "name", "electrified", "gauge")
                 if k in tags}
        run: list[tuple[float, float]] = []

        def flush(r):
            if len(r) >= 2:
                track_feats.append({"type": "Feature", "properties": props,
                                    "geometry": {"type": "LineString",
                                                 "coordinates": [[lo, la] for la, lo in r]}})
        for p, ok in zip(pts, inside):
            if ok:
                run.append(p)
            else:
                flush(run)
                run = []
        flush(run)

    print("\n" + "=" * 72)
    print(f"segments with geometry : {len(feats)}/{len(route) - 1}")
    print(f"OSM centreline total   : {total:.2f} km")
    print(f"Official ICMS total    : {OFFICIAL_KM:.2f} km")
    err = abs(total - OFFICIAL_KM) / OFFICIAL_KM * 100
    print(f"difference             : {abs(total - OFFICIAL_KM):.2f} km ({err:.1f}%)")
    print(f"track ways kept        : {kept_ways}/{len(ways)} "
          f"-> {len(track_feats)} clipped pieces")
    if flagged:
        print(f"segments to review     : {flagged}")
    ok = len(feats) == len(route) - 1 and err < 10
    print("\nVERDICT:", "basemap USABLE" if ok else "basemap NEEDS REVIEW")
    print("=" * 72)

    OSM.mkdir(parents=True, exist_ok=True)
    (OSM / "corridor_centreline.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    (OSM / "corridor_tracks.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": track_feats}),
        encoding="utf-8")
    for f in ("corridor_centreline.geojson", "corridor_tracks.geojson"):
        print(f"saved: {OSM / f}  ({(OSM / f).stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
