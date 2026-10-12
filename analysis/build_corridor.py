"""
Build the KTV-PSA corridor basemap from cached OSM geometry.

A first attempt used shortest-path search between consecutive stations. It failed:
on double track the UP and DN lines are separate ways joined only at distant
crossovers, so a shortest-path walk between consecutive stations detoured tens
of kilometres down the wrong track and back.

This instead uses linear referencing, which does not care about connectivity:

  1. build a coarse chain polyline through the 22 station points
  2. project every OSM way-node onto that chain -> (along_km, offset_km)
  3. discard nodes further than BUFFER_KM from the chain (drops branch lines)
  4. per inter-station segment, order the survivors by along_km -> centreline
  5. validate each segment length against the official ICMS distance

Outputs
  data/osm/corridor_centreline.geojson   one LineString per inter-station segment
  data/osm/corridor_tracks.geojson       every running-line way near the corridor
  data/osm/linref.json                   lookup table for placing trains on the map

Usage:  python analysis/build_corridor.py
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ktv-psa"
OSM = ROOT / "data" / "osm"
CACHE = OSM / "_cache"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"

BUFFER_KM = 0.6          # how far off the chain a node may sit and still count
OFFICIAL_KM = 176.83


def haversine(a, b) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def to_xy(pt, lat0):
    """Local equirectangular projection, km. Good enough over 180 km."""
    lat, lon = pt
    return (lon * 111.320 * math.cos(math.radians(lat0)), lat * 110.574)


def project_on_segment(p, a, b):
    """Return (t in [0,1], perpendicular distance km) of p onto segment ab."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return 0.0, math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return t, math.hypot(px - cx, py - cy)


def main() -> None:
    with open(ROUTE, newline="", encoding="utf-8-sig") as f:
        route = [(r["STATION_CODE"].strip().upper(), r["STATION_NAME"].strip(),
                  float(r["CUM_DISTANCE"] or 0)) for r in csv.DictReader(f)]

    match = json.loads((OSM / "station_match.json").read_text(encoding="utf-8"))
    stpos = {m["code"]: (m["osm"]["lat"], m["osm"]["lon"])
             for m in match["matched"] if m.get("osm", {}).get("lat") is not None}

    bands = sorted(CACHE.glob("ways_band*.json"))
    ways: dict[int, dict] = {}
    for b in bands:
        for el in json.loads(b.read_text(encoding="utf-8")).get("elements", []):
            if el.get("geometry"):
                ways[el["id"]] = el
    print("=" * 72)
    print("CORRIDOR BASEMAP - linear referencing from cached OSM")
    print("=" * 72)
    print(f"\ncached bands {len(bands)} -> {len(ways)} running-line ways")
    print(f"stations located: {len(stpos)}/{len(route)}")

    lat0 = sum(p[0] for p in stpos.values()) / len(stpos)

    # --- chain through the stations, in route order ------------------------
    chain = [stpos[c] for c, _, _ in route if c in stpos]
    chain_xy = [to_xy(p, lat0) for p in chain]
    seg_len = [haversine(chain[i], chain[i + 1]) for i in range(len(chain) - 1)]
    cum = [0.0]
    for d in seg_len:
        cum.append(cum[-1] + d)
    print(f"station chain (straight hops): {cum[-1]:.2f} km")

    # --- project every way node onto the chain -----------------------------
    print(f"\nprojecting way nodes (buffer {BUFFER_KM} km) ...")
    buckets: dict[int, list[tuple[float, tuple[float, float]]]] = {
        i: [] for i in range(len(chain) - 1)}
    kept = dropped = 0
    near_ways = []
    for way in ways.values():
        pts = [(g["lat"], g["lon"]) for g in way["geometry"]]
        any_near = False
        for p in pts:
            pxy = to_xy(p, lat0)
            best = (1e9, None, None)
            for i in range(len(chain_xy) - 1):
                t, off = project_on_segment(pxy, chain_xy[i], chain_xy[i + 1])
                if off < best[0]:
                    best = (off, i, t)
            off, i, t = best
            if off <= BUFFER_KM:
                buckets[i].append((t, off, p))
                kept += 1
                any_near = True
            else:
                dropped += 1
        if any_near:
            near_ways.append(way)
    print(f"  kept {kept} nodes, dropped {dropped} (off-corridor)")
    print(f"  ways touching the corridor: {len(near_ways)}/{len(ways)}")

    # --- build one centreline per inter-station segment --------------------
    print("\nsegment validation (OSM centreline vs official ICMS distance):")
    feats, total, bad = [], 0.0, []
    for i, ((c1, _, k1), (c2, _, k2)) in enumerate(zip(route, route[1:])):
        official = k2 - k1
        # Collapse parallel tracks: bin by distance along the segment and keep
        # only the node nearest the chain in each bin. Without this the line
        # zigzags between up/down tracks and station loops, inflating length.
        raw = buckets.get(i, [])
        binsize = 0.04 / max(seg_len[i], 0.01) if i < len(seg_len) else 0.05
        best_in_bin: dict[int, tuple[float, float, tuple[float, float]]] = {}
        for t, off, p in raw:
            b = int(t / binsize) if binsize > 0 else 0
            if b not in best_in_bin or off < best_in_bin[b][1]:
                best_in_bin[b] = (t, off, p)
        nodes = [best_in_bin[b] for b in sorted(best_in_bin)]
        line = []
        for _, _, p in nodes:
            if not line or haversine(line[-1], p) > 0.02:
                line.append(p)
        if len(line) < 2:
            bad.append(f"{c1}-{c2} (too few nodes)")
            print(f"  {c1:>5} -> {c2:<5}   NO GEOMETRY        official {official:6.2f}")
            continue
        if c1 in stpos:
            line.insert(0, stpos[c1])
        if c2 in stpos:
            line.append(stpos[c2])
        d = sum(haversine(a, b) for a, b in zip(line, line[1:]))
        total += d
        err = abs(d - official) / official * 100 if official else 0
        flag = "" if err < 12 else "   <-- check"
        print(f"  {c1:>5} -> {c2:<5}   osm {d:6.2f} km   official {official:6.2f} km"
              f"   {err:4.1f}%{flag}")
        if err >= 12:
            bad.append(f"{c1}-{c2}")
        feats.append({
            "type": "Feature",
            "properties": {"from": c1, "to": c2, "osm_km": round(d, 3),
                           "official_km": round(official, 3), "err_pct": round(err, 1),
                           "from_cum_km": k1, "to_cum_km": k2},
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat] for lat, lon in line]},
        })

    print("\n" + "=" * 72)
    print(f"segments with geometry : {len(feats)}/{len(route) - 1}")
    print(f"OSM centreline total   : {total:.2f} km")
    print(f"Official ICMS total    : {OFFICIAL_KM:.2f} km")
    err = abs(total - OFFICIAL_KM) / OFFICIAL_KM * 100
    print(f"difference             : {abs(total - OFFICIAL_KM):.2f} km ({err:.1f}%)")
    if bad:
        print(f"segments to review     : {bad}")
    ok = len(feats) == len(route) - 1 and err < 10
    print("\nVERDICT:", "basemap USABLE" if ok else "basemap NEEDS REVIEW")
    print("=" * 72)

    OSM.mkdir(parents=True, exist_ok=True)
    (OSM / "corridor_centreline.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    (OSM / "corridor_tracks.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature",
                      "properties": {k: v for k, v in (w.get("tags") or {}).items()
                                     if k in ("usage", "name", "electrified", "gauge",
                                              "maxspeed", "tracks")},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[g["lon"], g["lat"]]
                                                   for g in w["geometry"]]}}
                     for w in near_ways]}), encoding="utf-8")
    (OSM / "linref.json").write_text(json.dumps({
        "corridor": "KTV-PSA", "official_km": OFFICIAL_KM,
        "stations": [{"code": c, "name": n, "cum_km": k,
                      "lat": stpos.get(c, (None, None))[0],
                      "lon": stpos.get(c, (None, None))[1]} for c, n, k in route],
    }, indent=2), encoding="utf-8")
    for f in ("corridor_centreline.geojson", "corridor_tracks.geojson", "linref.json"):
        print(f"saved: {OSM / f}  ({(OSM / f).stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
