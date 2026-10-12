"""
Build a self-contained offline basemap for the KTV-PSA corridor.

Rather than ship a general-purpose tile set, this fetches just the geographic
context a railway corridor view actually needs - settlements, water, rivers,
coastline and major roads - and stores it as GeoJSON. MapLibre renders GeoJSON
sources directly, so the finished map needs no tile server and no network at
runtime, which matters when a demo runs on conference wifi.

Every response is cached under data/osm/_cache/, so a successful fetch never
has to be repeated. The public Overpass endpoints fail often.

Output: data/osm/basemap/*.geojson

Usage:  python analysis/build_basemap.py
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OSM = ROOT / "data" / "osm"
CACHE = OSM / "_cache"
BASEMAP = OSM / "basemap"

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# corridor bbox, padded for visual context
BBOX = (17.55, 82.85, 19.05, 84.80)

ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"


def overpass(query: str, key: str) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = CACHE / f"{key}.json"
    if cf.exists():
        print(f"    [cache] {key}")
        return json.loads(cf.read_text(encoding="utf-8"))

    data = urllib.parse.urlencode({"data": query}).encode()
    ctx = ssl.create_default_context()
    last = None
    for attempt in range(6):
        for ep in ENDPOINTS:
            try:
                req = urllib.request.Request(
                    ep, data=data, headers={"User-Agent": "PRAVAH-student-project/0.1"})
                with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
                    out = json.loads(r.read().decode())
                cf.write_text(json.dumps(out), encoding="utf-8")
                print(f"    [fetched] {key}  ({cf.stat().st_size // 1024} KB)")
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                print(f"      {ep.split('/')[2]:<26} try {attempt + 1}: {str(e)[:44]}")
        time.sleep(8 * (attempt + 1))
    raise SystemExit(f"Overpass failed for {key}: {last}")


# Coordinate precision: 5 dp is ~1 m, far finer than any basemap needs.
COORD_DP = 5
# Drop water bodies smaller than this; the corridor crosses irrigated farmland
# with thousands of tiny tanks that are visual noise at corridor zoom levels.
MIN_WATER_KM2 = 0.05
MIN_WATERWAY_KM = 1.0


def _round_coords(geom: dict) -> dict:
    def r(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], COORD_DP), round(c[1], COORD_DP)]
        return [r(x) for x in c]
    geom["coordinates"] = r(geom["coordinates"])
    return geom


def _ring_km2(ring: list) -> float:
    """Shoelace area of a lon/lat ring, converted to approximate km^2."""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    import math
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    a = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        a += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    return abs(a) / 2.0


def _line_km(coords: list) -> float:
    import math
    if len(coords) < 2:
        return 0.0
    lat0 = sum(p[1] for p in coords) / len(coords)
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    return sum(math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * ky)
               for a, b in zip(coords, coords[1:]))


def simplify(features: list, kind: str) -> list:
    """Drop insignificant features and shrink coordinate precision."""
    out = []
    for f in features:
        g = f["geometry"]
        if kind == "water" and g["type"] == "Polygon":
            if _ring_km2(g["coordinates"][0]) < MIN_WATER_KM2:
                continue
        if kind == "water" and g["type"] == "LineString":
            continue                       # unclosed water outlines: not useful
        if kind == "waterways" and _line_km(g["coordinates"]) < MIN_WATERWAY_KM:
            continue
        f["geometry"] = _round_coords(g)
        out.append(f)
    return out


def fc(features: list) -> dict:
    return {"type": "FeatureCollection", "attribution": ATTRIBUTION,
            "features": features}


def points(res: dict, keep: tuple[str, ...]) -> list:
    out = []
    for el in res.get("elements", []):
        t = el.get("tags", {})
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or not t.get("name"):
            continue
        out.append({"type": "Feature",
                    "properties": {k: t[k] for k in keep if k in t},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return out


def lines(res: dict, keep: tuple[str, ...], closed_as_polygon: bool = False) -> list:
    out = []
    for el in res.get("elements", []):
        g = el.get("geometry")
        if not g or len(g) < 2:
            continue
        coords = [[p["lon"], p["lat"]] for p in g]
        t = el.get("tags", {})
        props = {k: t[k] for k in keep if k in t}
        if closed_as_polygon and coords[0] == coords[-1] and len(coords) >= 4:
            geom = {"type": "Polygon", "coordinates": [coords]}
        else:
            geom = {"type": "LineString", "coordinates": coords}
        out.append({"type": "Feature", "properties": props, "geometry": geom})
    return out


def main() -> None:
    s, w, n, e = BBOX
    box = f"{s},{w},{n},{e}"
    BASEMAP.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("OFFLINE BASEMAP - KTV-PSA corridor")
    print(f"bbox {box}")
    print("=" * 72)

    layers: dict[str, list] = {}

    print("\n[1/5] settlements ...")
    res = overpass(
        f'[out:json][timeout:180];node["place"~"^(city|town)$"]({box});out;',
        "bm_places")
    layers["places"] = points(res, ("name", "place", "population"))
    print(f"      {len(layers['places'])} settlements")

    print("\n[2/5] rivers and streams ...")
    res = overpass(
        f'[out:json][timeout:240];way["waterway"~"^(river|stream)$"]({box});out geom;',
        "bm_waterways")
    layers["waterways"] = lines(res, ("name", "waterway"))
    print(f"      {len(layers['waterways'])} waterways")

    print("\n[3/5] water bodies ...")
    res = overpass(
        f'[out:json][timeout:240];way["natural"="water"]({box});out geom;',
        "bm_water")
    layers["water"] = lines(res, ("name", "water"), closed_as_polygon=True)
    print(f"      {len(layers['water'])} water bodies")

    print("\n[4/5] coastline ...")
    res = overpass(
        f'[out:json][timeout:240];way["natural"="coastline"]({box});out geom;',
        "bm_coast")
    layers["coastline"] = lines(res, ("name",))
    print(f"      {len(layers['coastline'])} coastline ways")

    print("\n[5/5] major roads ...")
    res = overpass(
        f'[out:json][timeout:300];'
        f'way["highway"~"^(motorway|trunk|primary)$"]({box});out geom;',
        "bm_roads")
    layers["roads"] = lines(res, ("name", "highway", "ref"))
    print(f"      {len(layers['roads'])} road ways")

    print("\nsimplifying and writing ...")
    total = 0
    for name, feats in layers.items():
        before = len(feats)
        feats = simplify(feats, name)
        path = BASEMAP / f"{name}.geojson"
        path.write_text(json.dumps(fc(feats)), encoding="utf-8")
        kb = path.stat().st_size // 1024
        total += kb
        drop = f"  (-{before - len(feats)})" if before != len(feats) else ""
        print(f"  {name:<12} {len(feats):>5} features   {kb:>5} KB{drop}")

    manifest = {
        "corridor": "KTV-PSA",
        "bbox": list(BBOX),
        "attribution": ATTRIBUTION,
        "layers": {k: f"basemap/{k}.geojson" for k in layers},
        "note": "Self-contained offline basemap. No tile server required.",
    }
    (OSM / "basemap_manifest.json").write_text(json.dumps(manifest, indent=2),
                                               encoding="utf-8")
    print(f"\ntotal basemap size: {total} KB")
    print(f"saved manifest: {OSM / 'basemap_manifest.json'}")
    print("=" * 72)


if __name__ == "__main__":
    main()
