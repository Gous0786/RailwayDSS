# PRAVAH

**Precision Rail Allocation for Vacant Headways** — a decision support system for
maximising freight throughput on a railway section.

Study corridor: **Kottavalasa Junction (KTV) → Palasa (PSA)**, 176.83 km, 22 stations,
Waltair Division, East Coast Railway — on the Howrah–Chennai trunk route.

## The problem, measured

Freight performance on this corridor, computed from 95,219 real block-section-level
goods movement records covering 1–27 August 2025:

| | |
|---|---|
| Freight trains cleared per day (end to end) | **31.2** |
| Median corridor transit time | **10.52 h** |
| Effective end-to-end speed | **16.8 km/h** |
| Median running speed *while in motion* | 30.8 km/h |
| **Share of transit time spent standing still** | **~45%** |

Throughput here is limited not by the track, but by how the track is used. Reproduce
these figures with `python analysis/baseline.py`.

## Repository layout

```
analysis/
  baseline.py            historical throughput baseline from goods movement records
  scenario_demo.py       worked example: greedy dispatch vs look-ahead planning
  check_osm_coverage.py  verifies OpenStreetMap coverage of the corridor
  build_corridor.py      extracts track geometry from cached OSM data
  build_linref.py        merges it into one chainage-addressable reference line
  build_basemap.py       fetches offline basemap context layers
  build_replay.py        turns the timetable into a chainage-over-time feed
pravah/
  corridor.py            linear referencing: chainage -> geographic position
web/
  index.html             offline corridor viewer (MapLibre)
  vendor/                MapLibre GL, vendored so nothing loads from a CDN
  data/replay.json       one day of train movements
data/
  ktv-psa/               official ICMS infrastructure and schedule extracts
  osm/                   derived corridor geometry and basemap (see Licensing)
docs/
  make_synopsis.py       generates the project synopsis
  PRAVAH_Synopsis.docx   generated output
```

## The corridor viewer

A map of the section with the day's trains moving along it, driven by the real
timetable. It runs **entirely offline** — no tile server, no API key, no CDN.

```bash
python -m http.server 8000      # serve from the repository root
# then open http://localhost:8000/web/
```

Play, scrub, or change replay speed; click a train for its type, chainage,
speed and current block. Train positions come from `pravah/corridor.py`'s
linear referencing, reimplemented identically in the browser — both produce the
same coordinates to five decimal places.

The basemap is purpose-built rather than general-purpose: settlements, water,
rivers, coastline and major roads for this corridor only, about 4.9 MB of
GeoJSON that MapLibre renders directly. A full tile basemap would be far larger
and would need a server.

To rebuild everything from the cached OSM responses:

```bash
python analysis/build_corridor.py   # track geometry
python analysis/build_linref.py     # chainage reference line
python analysis/build_basemap.py    # offline context layers
python analysis/build_replay.py     # one day of movements
```

## Running

```bash
pip install -r requirements.txt

python analysis/baseline.py         # measured throughput baseline
python analysis/scenario_demo.py    # greedy vs planned dispatch, real Monday data
python analysis/build_corridor.py   # rebuild map geometry (offline, from cache)
python docs/make_synopsis.py        # regenerate the synopsis
```

## Data

**ICMS extracts** (`data/ktv-psa/`) — 63 block sections, 22 stations, 103 station
lines with individual loop lengths, 231 scheduled services across 3,901 timing
records, 9 permanent speed restrictions, and 95,219 goods movements of which 15,449
fall on this corridor.

Three parsing traps, all handled in the loaders:

- `BLOCKSCTN` and `BLOCKSCTNLINE` contain a **repeated header row mid-file**, and are
  **duplicated exports** — 125 rows for 63 distinct block sections.
- `SCHEDULE` times are **seconds since midnight of day 1 of the journey**, not of the
  day. Time of day is `ARRIVAL % 86400`; journey day is `ARRIVAL // 86400 + 1`.
- `Block Hrs` in the goods log is a **day fraction, not hours** — multiply by 24.

**Corridor geometry** (`data/osm/`) — derived from OpenStreetMap. All 22 stations
match on `railway:ref` (the official IR station code). The extracted centreline
measures 178.99 km against the official 176.83 km, a 1.2% difference.

Three stations (BTVA, SGDM, NWP) sit at the wrong place along the OSM line; BTVA
is out by 2.1 km. The geometry either side of them is correct — CPP→BTVA→SGDM
measures 13.24 km against an official 13.27 km — so `build_linref.py` keeps the
well-matched stations as control points and re-places the others at the position
their official chainage implies. Worst inter-station error afterwards is 5.5%.

Cached Overpass responses live in `data/osm/_cache/` and are committed
deliberately: the public endpoints fail often, and the cache is what makes the
pipeline reproducible offline.

**Timetable quirk** worth knowing: UP services are stored along the KTV→PSA route
axis exactly like DN ones, but with their times counting *backwards*, because the
train physically runs PSA→KTV. They must be reversed at stop level — reversing a
flattened point list also swaps arrival before departure at each stop.

## Licensing

Corridor geometry under `data/osm/` is derived from OpenStreetMap and is licensed
**ODbL**. Any map rendered from it must display *"© OpenStreetMap contributors"*.

The ICMS extracts under `data/ktv-psa/` are Indian Railways operational records for
the Kottavalasa–Palasa section; confirm redistribution terms before publishing.
