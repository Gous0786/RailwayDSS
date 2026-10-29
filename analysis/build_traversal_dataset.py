"""
Training data for the block traversal estimator.

One row per goods movement over one corridor block: what the rake was, which
block it crossed, which way, when - and how long it took. 15k of them over
27 days.

Three things here decide whether the model that follows is honest.

  1. The target is NOT the time the rake should take. It is the time the rake
     DID take, delay and all, and on this corridor that tail is enormous: a
     4.77 km block with a recorded 143 minutes. The optimiser schedules its
     own conflicts, so a model trained to predict the mean would hand it
     delay it is about to add again. What the optimiser needs from this file
     is a LOW quantile - near the clean run. The tail is kept anyway, because
     the same rows answer a different question for the controller ("what
     should I actually expect"), and quantile regression needs the shape.

  2. The `Speed` column is dropped. It is exactly Block Km / (Block Hrs x 24)
     - verified on 79,408 rows, 4 disagreements, all rounding - so it is the
     target wearing a hat. Training on it would score beautifully and predict
     nothing.

  3. Rows are tagged with their date so the split downstream can be by DAY.
     One consignment crosses up to 21 blocks in an afternoon; split those
     rows at random and the same rake's other blocks sit in train and test at
     once. The score would be measuring memory, not generalisation.

Block Hrs is a day fraction, not hours. It is the single most expensive
misreading available in this dataset.

Output: data/derived/traversal_dataset.json

Usage:  python analysis/build_traversal_dataset.py
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pravah.section import Direction, SectionGraph   # noqa: E402

sys.path.insert(0, str(ROOT / "analysis"))
from build_freight_demand import rake_for            # noqa: E402

GOODS = (ROOT / "data" / "ktv-psa" / "KTV-PSA-Infra" /
         "WAT_GOODS_TRAIN_AUG25_01082025_27082025.csv")
DERIVED = ROOT / "data" / "derived"

# A block crossing under half a minute is a recording artefact, not a move.
# The upper bound is deliberately generous: 691 minutes on a 10 km block is
# not a typo, it is a rake that stood for most of a shift, and the p80 model
# has to see those to be worth anything. Only beyond a day does the record
# stop describing one crossing.
MIN_MIN = 0.5
MAX_MIN = 1440.0

COLUMNS = ["block", "direction", "block_km", "loaded", "wagon_type",
           "family", "commodity", "length_m", "axle_load_t", "hour",
           "date", "minutes"]


def rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        out = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
               for r in csv.DictReader(f)]
    key = next(iter(out[0]))
    return [r for r in out if r.get(key) and r[key] != key]


def family_of(wagon: str) -> str:
    """The wagon family, which is what actually varies the running time.

    The extract carries BCNAHSM1, BRN22.9, BFNSM1 and dozens more variants
    that differ in brake gear, not in how fast the rake gets over a bank.
    Leaving them as distinct levels would scatter a block's samples across
    tiny cells; the family keeps them together. The exact type is kept too,
    and the model is free to use either.
    """
    t = (wagon or "").strip().upper()
    for p in ("BOXNHL", "BOXN", "BOBRN", "BOBYN", "BCNHL", "BCNA", "BCN",
              "BOST", "BOY", "BRN", "BFNS", "BFN", "BTAP", "BTPN", "BLCA",
              "BLCB", "BLC", "NMG", "SHRN"):
        if t.startswith(p):
            return p
    return t[:3] if t else "UNK"


def build() -> dict:
    g = SectionGraph.load()
    order = [s.code for s in g.stations]
    pairs: dict[str, tuple[str, str]] = {}
    for a, b in zip(order, order[1:]):
        pairs[f"{a}-{b}"] = (a, b)
        pairs[f"{b}-{a}"] = (b, a)

    drop: Counter = Counter()
    out: list[list] = []
    per_block: dict[str, list[float]] = defaultdict(list)

    for r in rows(GOODS):
        bs = (r.get("Block Section") or "").strip().upper()
        if bs not in pairs:
            drop["not a corridor block"] += 1
            continue
        try:
            minutes = float(r.get("Block Hrs") or 0) * 24 * 60
        except ValueError:
            drop["unreadable Block Hrs"] += 1
            continue
        if not (MIN_MIN < minutes < MAX_MIN):
            drop["implausible duration"] += 1
            continue
        try:
            arrived = datetime.strptime(r.get("Arrival Time", ""),
                                        "%d/%m/%Y %H:%M")
        except ValueError:
            drop["unreadable arrival"] += 1
            continue

        a, b = pairs[bs]
        link = g.link(a, b)
        # Arrival Time is arrival at the FAR end of the block, so the rake
        # entered it `minutes` earlier. Time of day is only meaningful at
        # entry: that is when the rake met whatever traffic was about.
        entered = arrived - timedelta(minutes=minutes)
        wagon = (r.get("Load Type") or "").strip().upper()
        _, length_m, axle = rake_for(wagon)
        # Block length comes from the infrastructure, not the extract. The
        # two agree to within 1% on every block that has samples, but the
        # extract leaves it blank on 1,825 rows - 12% of the data, and not
        # at random: it clusters on particular blocks. Taking the graph's
        # figure keeps those rows and removes a second-order nuisance, the
        # per-row rounding wobble in the extract, which is noise the model
        # would otherwise be free to fit.
        km = max(ln.length_km for ln in link.lines)

        out.append([
            bs,
            "DN" if g.direction_of(a, b) is Direction.DOWN else "UP",
            round(km, 2),
            1 if (r.get("LE") or "").strip().upper() == "L" else 0,
            wagon or "UNK",
            family_of(wagon),
            (r.get("Commodity") or "").strip().upper() or "UNK",
            length_m,
            axle,
            entered.hour,
            entered.date().isoformat(),
            round(minutes, 2),
        ])
        per_block[bs].append(minutes)

    days = sorted({row[COLUMNS.index("date")] for row in out})
    return {
        "source": GOODS.name,
        "columns": COLUMNS,
        "rows": out,
        "days": days,
        "dropped": dict(drop),
        "blocks": {k: {"n": len(v), "median_min": round(statistics.median(v), 1)}
                   for k, v in sorted(per_block.items())},
    }


def main() -> None:
    d = build()
    DERIVED.mkdir(parents=True, exist_ok=True)
    path = DERIVED / "traversal_dataset.json"
    path.write_text(json.dumps(d), encoding="utf-8")

    y = [r[-1] for r in d["rows"]]
    q = statistics.quantiles(y, n=100)
    print("=" * 66)
    print("BLOCK TRAVERSAL DATASET")
    print("=" * 66)
    print(f"\nrows        : {len(d['rows'])}")
    print(f"blocks      : {len(d['blocks'])}")
    print(f"days        : {len(d['days'])}  {d['days'][0]} .. {d['days'][-1]}")
    print(f"\ndropped     :")
    for k, v in sorted(d["dropped"].items(), key=lambda x: -x[1]):
        print(f"  {k:<24}{v:>8}")
    print(f"\ntarget (min): p10 {q[9]:.0f}  p20 {q[19]:.0f}  p50 {q[49]:.0f}"
          f"  p80 {q[79]:.0f}  p90 {q[89]:.0f}  max {max(y):.0f}")
    print(f"\nthe tail is delay, not running: the longest crossing is "
          f"{max(y):.0f} min")
    print(f"on a block of "
          f"{d['rows'][y.index(max(y))][COLUMNS.index('block_km')]} km.")
    print(f"\nwrote {path}  ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
