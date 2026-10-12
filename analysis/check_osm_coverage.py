"""
Verify OpenStreetMap coverage of the KTV-PSA corridor before committing to a
geographic (Tier 2) view.

Checks three things:
  1. Are the 22 route stations present in OSM as railway nodes?
  2. Is there continuous railway=rail geometry along the corridor?
  3. Are loops / sidings mapped, or only the running lines?

Usage:  python analysis/check_osm_coverage.py
"""

from __future__ import annotations

import csv
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "ktv-psa"
ROUTE = DATA / "RouteSttnInfo" / "KTV-PSA-Table 1.csv"
STATION = DATA / "KTV-PSA-Infra" / "STATION-Table 1.csv"
OUT = Path(__file__).resolve().parent.parent / "data" / "osm"

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Generous box around the corridor (stations span 17.89-18.76 N, 83.19-84.42 E)
BBOX = (17.80, 83.10, 18.85, 84.55)


def overpass(query: str, retries: int = 2) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    ctx = ssl.create_default_context()
    last = None
    for endpoint in ENDPOINTS:
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    endpoint, data=data,
                    headers={"User-Agent": "PRAVAH-student-project/0.1 (coverage check)"})
                with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
                    return json.loads(r.read().decode())
            except Exception as e:  # noqa: BLE001
                last = e
                print(f"    {endpoint.split('/')[2]} attempt {attempt + 1}: {e}")
                time.sleep(5)
    raise SystemExit(f"all Overpass endpoints failed: {last}")


def load_route() -> list[tuple[str, str]]:
    with open(ROUTE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [(r["STATION_CODE"].strip().upper(), r["STATION_NAME"].strip()) for r in rows]


def norm(s: str) -> str:
    s = s.upper()
    for junk in (" JN.", " JN", " P.H.", ".", "-", "  "):
        s = s.replace(junk, " ")
    return " ".join(s.split())


def main() -> None:
    route = load_route()
    s, w, n, e = BBOX
    box = f"{s},{w},{n},{e}"

    print("=" * 70)
    print("OSM COVERAGE CHECK - KTV-PSA corridor")
    print("=" * 70)

    # ---- 1. stations ------------------------------------------------------
    print("\n[1/3] railway stations and halts in bbox ...")
    q = f"""[out:json][timeout:120];
(
  node["railway"~"^(station|halt)$"]({box});
  way["railway"~"^(station|halt)$"]({box});
);
out center tags;"""
    res = overpass(q)
    osm_st = []
    for el in res.get("elements", []):
        t = el.get("tags", {})
        name = t.get("name") or t.get("name:en") or ""
        ref = (t.get("railway:ref") or t.get("ref") or "").upper()
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        osm_st.append({"name": name, "ref": ref, "lat": lat, "lon": lon,
                       "railway": t.get("railway")})
    print(f"      found {len(osm_st)} station/halt features")

    by_ref = {o["ref"]: o for o in osm_st if o["ref"]}
    by_name = {norm(o["name"]): o for o in osm_st if o["name"]}

    matched, missing = [], []
    for code, name in route:
        hit = by_ref.get(code) or by_name.get(norm(name))
        if not hit:
            for k, v in by_name.items():
                if norm(name) and (norm(name) in k or k in norm(name)):
                    hit = v
                    break
        (matched if hit else missing).append((code, name, hit))

    print(f"\n      matched {len(matched)}/{len(route)} route stations")
    for code, name, hit in matched:
        how = "ref" if hit.get("ref") == code else "name"
        print(f"        OK   {code:<5} {name:<22} -> {hit['name'][:28]:<28} ({how}, "
              f"{hit['lat']:.4f},{hit['lon']:.4f})")
    for code, name, _ in missing:
        print(f"        MISS {code:<5} {name}")

    # ---- 2. rail geometry -------------------------------------------------
    print("\n[2/3] railway=rail way geometry in bbox ...")
    q = f"""[out:json][timeout:180];
way["railway"="rail"]({box});
out count;"""
    res = overpass(q)
    cnt = res.get("elements", [{}])[0].get("tags", {})
    print(f"      rail ways: {cnt.get('ways', '?')}  nodes: {cnt.get('nodes', '?')}")

    # ---- 3. usage breakdown (main vs branch vs siding) --------------------
    print("\n[3/3] service / usage tag breakdown ...")
    q = f"""[out:json][timeout:180];
way["railway"="rail"]({box});
out tags;"""
    res = overpass(q)
    from collections import Counter
    usage = Counter()
    service = Counter()
    gauge = Counter()
    electrified = Counter()
    named = 0
    total_ways = 0
    for el in res.get("elements", []):
        t = el.get("tags", {})
        total_ways += 1
        usage[t.get("usage", "(none)")] += 1
        service[t.get("service", "(running line)")] += 1
        gauge[t.get("gauge", "(none)")] += 1
        electrified[t.get("electrified", "(none)")] += 1
        if t.get("name"):
            named += 1

    print(f"      total rail ways: {total_ways}  ({named} carry a name tag)")
    print(f"      usage:       {dict(usage.most_common(5))}")
    print(f"      service:     {dict(service.most_common(6))}")
    print(f"      gauge:       {dict(gauge.most_common(3))}")
    print(f"      electrified: {dict(electrified.most_common(3))}")

    sidings = sum(v for k, v in service.items() if k != "(running line)")
    print(f"\n      running lines: {service.get('(running line)', 0)}   "
          f"sidings/spurs/yard: {sidings}")

    # ---- verdict ----------------------------------------------------------
    print("\n" + "=" * 70)
    pct = 100 * len(matched) / len(route)
    print(f"VERDICT: {len(matched)}/{len(route)} stations ({pct:.0f}%), "
          f"{total_ways} rail ways, {sidings} non-running-line segments")
    if pct >= 80 and total_ways > 50:
        print("  -> Tier 2 geographic view is VIABLE")
    elif pct >= 50:
        print("  -> Tier 2 PARTIALLY viable; some stations need manual placement")
    else:
        print("  -> Tier 2 NOT viable from OSM alone")
    print("=" * 70)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "station_match.json").write_text(json.dumps(
        {"matched": [{"code": c, "name": n, "osm": h} for c, n, h in matched],
         "missing": [{"code": c, "name": n} for c, n, _ in missing]},
        indent=2), encoding="utf-8")
    print(f"\nsaved: {OUT / 'station_match.json'}")


if __name__ == "__main__":
    main()
