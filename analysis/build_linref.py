"""
Build the corridor linear-reference line.

Takes the 21 per-segment centrelines from build_corridor.py and produces a
single continuous polyline with a distance mapping, so that any point can be
addressed by its official chainage ("the train is at 43.2 km").

Two things happen here:

  MERGE       the 21 segments are concatenated into one polyline with
              cumulative OSM distance at every vertex.

  RUBBER-SHEET  a few station anchors sit at the wrong place along the line.
              BTVA is the clearest case: CPP->BTVA reads 48% long and
              BTVA->SGDM 25% short, yet together they are within 0.03 km of
              the official 13.27 km. The geometry is right; the anchor is
              misplaced. Stations whose segments agree with ICMS are used as
              control points, and a piecewise-linear map is fitted from
              official chainage to distance along the OSM line. Bad anchors
              are then re-placed at the position their official chainage
              implies.

Output: data/osm/linref.json  - polyline + station chainage, consumed at
runtime by pravah.corridor.

Usage:  python analysis/build_linref.py
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OSM = ROOT / "data" / "osm"
ROUTE = ROOT / "data" / "ktv-psa" / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

ERR_TOLERANCE_PCT = 12.0     # above this, a segment is treated as mis-anchored


def haversine(a, b) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def main() -> None:
    fc = json.loads((OSM / "corridor_centreline.geojson").read_text(encoding="utf-8"))
    segs = {(f["properties"]["from"], f["properties"]["to"]): f for f in fc["features"]}

    with open(ROUTE, newline="", encoding="utf-8-sig") as f:
        route = [(r["STATION_CODE"].strip().upper(), r["STATION_NAME"].strip(),
                  float(r["CUM_DISTANCE"] or 0)) for r in csv.DictReader(f)]
    codes = [c for c, _, _ in route]
    official = {c: k for c, _, k in route}

    print("=" * 72)
    print("CORRIDOR LINEAR REFERENCE")
    print("=" * 72)

    # ---- 1. merge segments into one polyline ------------------------------
    line: list[tuple[float, float]] = []
    seg_bounds: dict[str, tuple[float, float]] = {}   # code -> (start_idx, osm_km)
    for i, (c1, c2) in enumerate(zip(codes, codes[1:])):
        f = segs.get((c1, c2))
        if not f:
            raise SystemExit(f"missing segment {c1}->{c2}; run build_corridor.py first")
        pts = [(lat, lon) for lon, lat in f["geometry"]["coordinates"]]
        if line and haversine(line[-1], pts[0]) < 0.05:
            pts = pts[1:]                              # drop duplicated join point
        seg_bounds[c1] = (len(line) - 1 if line else 0, 0.0)
        line.extend(pts)
    # Drop vertices that double back on themselves. A few survive the
    # per-segment cleanup at segment joins, and each one corrupts the local
    # bearing used to orient train icons.
    def despike(pts, max_reversal_deg=120.0):
        import math
        out = [pts[0]]
        for i in range(1, len(pts) - 1):
            a, b, c = out[-1], pts[i], pts[i + 1]
            v1 = (b[0] - a[0], b[1] - a[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            n1, n2 = math.hypot(*v1), math.hypot(*v2)
            if n1 > 0 and n2 > 0:
                cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
                if cosang < math.cos(math.radians(max_reversal_deg)):
                    continue
            out.append(b)
        out.append(pts[-1])
        return out

    before = len(line)
    line = despike(line)
    print(f"\nmerged polyline: {len(line)} vertices "
          f"({before - len(line)} reversals removed)")

    # cumulative OSM distance at each vertex
    cum = [0.0]
    for a, b in zip(line, line[1:]):
        cum.append(cum[-1] + haversine(a, b))
    osm_total = cum[-1]
    print(f"OSM length     : {osm_total:.2f} km")
    print(f"Official length: {official[codes[-1]]:.2f} km")

    # ---- 2. locate each station along the merged line ---------------------
    match = json.loads((OSM / "station_match.json").read_text(encoding="utf-8"))
    stpos = {m["code"]: (m["osm"]["lat"], m["osm"]["lon"])
             for m in match["matched"] if m.get("osm", {}).get("lat") is not None}

    def nearest_on_line(pt):
        best_i, best_d = 0, 1e9
        for i, p in enumerate(line):
            d = haversine(pt, p)
            if d < best_d:
                best_i, best_d = i, d
        return best_i, best_d

    raw_along: dict[str, float] = {}
    for c in codes:
        if c in stpos:
            i, _ = nearest_on_line(stpos[c])
            raw_along[c] = cum[i]

    # ---- 3. decide which anchors to trust ---------------------------------
    bad = set()
    for (c1, c2) in zip(codes, codes[1:]):
        f = segs.get((c1, c2))
        if f and f["properties"]["err_pct"] > ERR_TOLERANCE_PCT:
            # blame the station whose two adjacent segments have opposing errors
            bad.add(c2)
    # the terminals are definitionally correct
    bad.discard(codes[0])
    bad.discard(codes[-1])
    good = [c for c in codes if c in raw_along and c not in bad]
    print(f"\ncontrol points : {len(good)}/{len(codes)} stations")
    if bad:
        print(f"re-anchoring   : {sorted(bad)}")

    # ---- 4. piecewise-linear map official chainage -> OSM distance --------
    ctrl = sorted(((official[c], raw_along[c]) for c in good), key=lambda x: x[0])
    # force monotonicity (a bad control point would break interpolation)
    mono = [ctrl[0]]
    for k, a in ctrl[1:]:
        if a > mono[-1][1] and k > mono[-1][0]:
            mono.append((k, a))
    if len(mono) < len(ctrl):
        print(f"  dropped {len(ctrl) - len(mono)} non-monotonic control point(s)")
    ctrl = mono

    def official_to_osm(km: float) -> float:
        if km <= ctrl[0][0]:
            return ctrl[0][1]
        if km >= ctrl[-1][0]:
            return ctrl[-1][1]
        for (k1, a1), (k2, a2) in zip(ctrl, ctrl[1:]):
            if k1 <= km <= k2:
                t = (km - k1) / (k2 - k1) if k2 > k1 else 0.0
                return a1 + t * (a2 - a1)
        return ctrl[-1][1]

    def point_at(osm_km: float) -> tuple[float, float]:
        if osm_km <= 0:
            return line[0]
        if osm_km >= osm_total:
            return line[-1]
        lo, hi = 0, len(cum) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if cum[mid] <= osm_km:
                lo = mid
            else:
                hi = mid
        span = cum[hi] - cum[lo]
        t = (osm_km - cum[lo]) / span if span > 0 else 0.0
        (la1, lo1), (la2, lo2) = line[lo], line[hi]
        return (la1 + t * (la2 - la1), lo1 + t * (lo2 - lo1))

    # ---- 5. final station table + validation ------------------------------
    print("\nstation placement (official chainage -> position on OSM line):")
    stations = []
    for c, n, k in route:
        a = official_to_osm(k)
        lat, lon = point_at(a)
        moved = haversine(stpos[c], (lat, lon)) if c in stpos else None
        tag = "  RE-ANCHORED" if c in bad else ""
        stations.append({"code": c, "name": n, "cum_km": round(k, 3),
                         "osm_km": round(a, 3), "lat": round(lat, 6),
                         "lon": round(lon, 6), "re_anchored": c in bad})
        if moved is not None and (moved > 0.2 or c in bad):
            print(f"  {c:<5} {k:7.2f} km   moved {moved * 1000:5.0f} m{tag}")

    print("\ninter-station check after re-anchoring:")
    worst = 0.0
    for s1, s2 in zip(stations, stations[1:]):
        off = s2["cum_km"] - s1["cum_km"]
        got = s2["osm_km"] - s1["osm_km"]
        err = abs(got - off) / off * 100 if off else 0
        worst = max(worst, err)
        if err > 8:
            print(f"  {s1['code']:>5} -> {s2['code']:<5} "
                  f"osm {got:6.2f}  official {off:6.2f}   {err:4.1f}%")
    print(f"  worst remaining error: {worst:.1f}%")

    out = {
        "corridor": "KTV-PSA",
        "official_km": official[codes[-1]],
        "osm_km": round(osm_total, 3),
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "stations": stations,
        "polyline": [[round(lon, 6), round(lat, 6)] for lat, lon in line],
        "cum_osm_km": [round(c, 4) for c in cum],
        "control_points": [[round(k, 3), round(a, 3)] for k, a in ctrl],
    }
    (OSM / "linref.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"\nsaved: {OSM / 'linref.json'} "
          f"({(OSM / 'linref.json').stat().st_size // 1024} KB)")
    print("=" * 72)


if __name__ == "__main__":
    main()
