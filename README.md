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
  build_corridor.py      builds the map basemap from cached OSM geometry
data/
  ktv-psa/               official ICMS infrastructure and schedule extracts
  osm/                   derived corridor geometry (see Licensing)
docs/
  make_synopsis.py       generates the project synopsis
  PRAVAH_Synopsis.docx   generated output
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

## Licensing

Corridor geometry under `data/osm/` is derived from OpenStreetMap and is licensed
**ODbL**. Any map rendered from it must display *"© OpenStreetMap contributors"*.

The ICMS extracts under `data/ktv-psa/` are Indian Railways operational records for
the Kottavalasa–Palasa section; confirm redistribution terms before publishing.
