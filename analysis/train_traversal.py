"""
Learn the block traversal time, and find out whether learning it was worth it.

The optimiser currently crosses every freight block at one speed: 31 km/h,
the measured median over the whole corridor, plus the permanent speed
restriction where there is one. That is a single number standing in for 24
blocks, two directions, nineteen wagon families and a loaded/empty flag. This
asks what conditioning on those actually buys, and it is set up so that the
answer is allowed to be "nothing".

WHAT IS PREDICTED, AND WHY IT IS A LOW QUANTILE

Not the mean. The optimiser schedules conflicts itself - that is the whole
job - so if it is told a block takes its average 49 minutes when 25 is the
clean-run time, it counts the queue twice: once in the running time it was
handed, once in the plan it builds. The number the solver needs is the
technical minimum, near the bottom of the distribution. p20 is that, and it
is not a free knob: the existing constant model already lands around p20-p30
on most blocks, so p20 keeps the new estimator on the same footing as the one
it replaces and the comparison stays about CONDITIONING rather than about
shifting every block down.

p50 and p80 come out of the same fit for the controller's benefit - what to
expect, and what to allow - and never reach the solver.

FOUR CONTENDERS, ONE METRIC

  constant   what the model does today: corridor median speed + PSR
  block      the training days' own quantile for that block, no model
  cells      the same, cut further by wagon family and load state
  cell       LightGBM on block, direction, load state, wagon family
  full       cell, plus hour of entry, commodity, exact type, length, axle

Pinball loss at the quantile, on days no model ever saw. `block` is the
contender that matters: it is what you get by looking the answer up in a
table, and a gradient booster that cannot beat a lookup table has not earned
its dependency. `cell` against `full` decides something else - whether the
estimator can ship AS a table or needs the model loaded at runtime.

THREE WAYS OF SPLITTING, AND ONLY ONE OF THEM IS SAFE

Not by row. One consignment crosses up to 21 blocks in an afternoon; shuffle
rows and the same rake's other blocks sit in train and test together, the
score measures memory rather than generalisation, and it looks wonderful.

So by day, into three: days 1-15 fit, 16-20 choose, 21-27 report. The middle
block exists because the round count matters more than anything else here -
at 400 rounds the booster loses to the lookup table, at 150 it beats it - and
picking that count by watching the test week would be fitting to the thing
being reported. Every tuning decision is made on days 16-20. The held-out
week is touched once, at the end, by all five contenders at once.

Output: data/derived/traversal_model.json
        data/derived/traversal_report.json

Usage:  python analysis/train_traversal.py
        python analysis/train_traversal.py --test-days 7
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pravah.section import Direction, SectionGraph   # noqa: E402

DERIVED = ROOT / "data" / "derived"
DATASET = DERIVED / "traversal_dataset.json"

QUANTILES = (0.2, 0.5, 0.8)
SOLVER_Q = 0.2          # the only one the optimiser ever sees
MIN_CELL = 30           # below this the empirical quantile is noise
SEED = 20250801

# Searched on the validation days only. The grid is small on purpose: four
# settings spanning deep-and-loose to shallow-and-heavily-regularised. They
# landed within 0.003 of each other, which is the useful finding - the round
# count is what matters, not the shape of the trees.
GRID = (
    {"num_leaves": 31, "min_data_in_leaf": 40, "lambda_l2": 1.0},
    {"num_leaves": 15, "min_data_in_leaf": 80, "lambda_l2": 5.0},
    {"num_leaves": 7, "min_data_in_leaf": 150, "lambda_l2": 10.0},
    {"num_leaves": 4, "min_data_in_leaf": 200, "lambda_l2": 20.0},
)
ROUND_STEPS = tuple(range(25, 801, 25))

CAT = ("block", "direction", "wagon_type", "family", "commodity")
NUM = ("block_km", "loaded", "length_m", "axle_load_t", "hour")

CELL_FEATURES = ("block", "direction", "family", "loaded", "block_km")
FULL_FEATURES = CELL_FEATURES + ("wagon_type", "commodity", "length_m",
                                 "axle_load_t", "hour")


def pinball(y: np.ndarray, pred: np.ndarray, q: float) -> float:
    """Mean pinball loss. Under-predicting costs q, over-predicting 1-q."""
    d = y - pred
    return float(np.mean(np.maximum(q * d, (q - 1.0) * d)))


class Data:
    """The dataset, encoded once, split by day."""

    def __init__(self, raw: dict, test_days: int, val_days: int):
        self.idx = {c: i for i, c in enumerate(raw["columns"])}
        rows, days = raw["rows"], raw["days"]
        self.train_days = days[:-test_days]
        self.test_days = days[-test_days:]
        self.fit_days = self.train_days[:-val_days]
        self.val_days = self.train_days[-val_days:]

        di = self.idx["date"]
        train_set, test_set = set(self.train_days), set(self.test_days)
        tr = [r for r in rows if r[di] in train_set]
        te = [r for r in rows if r[di] in test_set]
        fit_set, val_set = set(self.fit_days), set(self.val_days)
        self.rows_fit = [r for r in tr if r[di] in fit_set]
        self.rows_val = [r for r in tr if r[di] in val_set]

        # Levels are fitted on TRAIN only. A wagon type that first appears in
        # the held-out week has to arrive as unknown, exactly as it would in
        # service; fitting them on everything tells the test about its future.
        self.levels = {c: {v: i for i, v in enumerate(
            sorted({r[self.idx[c]] for r in tr}))} for c in CAT}

        self.train, self.y_train = self._encode(tr)
        self.test, self.y_test = self._encode(te)
        self.rows_train, self.rows_test = tr, te
        self.fit, self.y_fit = self._encode(self.rows_fit)
        self.val, self.y_val = self._encode(self.rows_val)

    def _encode(self, rows: list[list]):
        out = {}
        for c in CAT:
            lv = self.levels[c]
            out[c] = np.array([lv.get(r[self.idx[c]], -1) for r in rows],
                              dtype=np.int32)
        for c in NUM:
            out[c] = np.array([r[self.idx[c]] for r in rows], dtype=np.float64)
        y = np.array([r[self.idx["minutes"]] for r in rows], dtype=np.float64)
        return out, y

    def matrix(self, which: str, names: tuple[str, ...]) -> np.ndarray:
        src = {"train": self.train, "test": self.test,
               "fit": self.fit, "val": self.val}[which]
        return np.column_stack([src[c] for c in names])

    def cat_flags(self, names: tuple[str, ...]) -> list[int]:
        return [i for i, c in enumerate(names) if c in CAT]


# -------------------------------------------------------------- contenders

def constant_table() -> dict[str, float]:
    """What the optimiser uses today, per block."""
    g = SectionGraph.load()
    order = [s.code for s in g.stations]
    out = {}
    for a, b in zip(order, order[1:]):
        for x, y in ((a, b), (b, a)):
            link = g.link(x, y)
            d = g.direction_of(x, y)
            lines = [ln for ln in link.lines
                     if ln.nominal_direction in (d, Direction.BOTH)] or link.lines
            out[f"{x}-{y}"] = min(
                g.running_time(link.id, ln.name, "FREIGHT", d) for ln in lines)
    return out


def constant_model(data: Data, table: dict[str, float]) -> np.ndarray:
    bi = data.idx["block"]
    return np.array([table[r[bi]] for r in data.rows_test], dtype=np.float64)


def empirical_table(data: Data, q: float) -> tuple[dict[str, float], float]:
    """The training days' own quantile per block. No model involved."""
    bi, mi = data.idx["block"], data.idx["minutes"]
    per: dict[str, list[float]] = defaultdict(list)
    for r in data.rows_train:
        per[r[bi]].append(r[mi])
    glob = float(np.quantile([r[mi] for r in data.rows_train], q))
    return ({k: float(np.quantile(v, q))
             for k, v in per.items() if len(v) >= MIN_CELL}, glob)


def empirical_model(data: Data, q: float) -> np.ndarray:
    table, glob = empirical_table(data, q)
    bi = data.idx["block"]
    return np.array([table.get(r[bi], glob) for r in data.rows_test],
                    dtype=np.float64)


def cells_model(data: Data, q: float) -> np.ndarray:
    """The block quantile, cut further by wagon family and load state.

    The direct test of whether the rake matters, with no model in the way. A
    cell needs MIN_CELL samples to be used at all; below that it falls back
    to the block, because a quantile over nine observations is a rumour.
    """
    bi, mi = data.idx["block"], data.idx["minutes"]
    fi, li = data.idx["family"], data.idx["loaded"]
    block, glob = empirical_table(data, q)
    per: dict[tuple, list[float]] = defaultdict(list)
    for r in data.rows_train:
        per[(r[bi], r[fi], r[li])].append(r[mi])
    cell = {k: float(np.quantile(v, q))
            for k, v in per.items() if len(v) >= MIN_CELL}
    return np.array(
        [cell.get((r[bi], r[fi], r[li]), block.get(r[bi], glob))
         for r in data.rows_test], dtype=np.float64)


def _params(q: float, extra: dict) -> dict:
    return {"objective": "quantile", "alpha": q, "metric": "quantile",
            "learning_rate": 0.05, "feature_fraction": 1.0,
            "verbose": -1, "seed": SEED, "deterministic": True,
            "num_threads": 4, **extra}


def lgbm_tuned(data: Data, q: float, names: tuple[str, ...]):
    """Choose the shape and the round count on the validation days, then
    refit on every training day and predict the held-out week once.

    The refit matters. Having spent days 16-20 deciding how long to train,
    throwing them away would cost a third of the data for no reason; keeping
    them in the final fit is standard and the test week is still untouched.
    """
    import lightgbm as lgb

    cats = data.cat_flags(names)
    xfit, xval = data.matrix("fit", names), data.matrix("val", names)

    best = None
    for extra in GRID:
        ds = lgb.Dataset(xfit, label=data.y_fit, categorical_feature=cats,
                         free_raw_data=False)
        b = lgb.train(_params(q, extra), ds, num_boost_round=max(ROUND_STEPS))
        for n in ROUND_STEPS:
            loss = pinball(data.y_val, b.predict(xval, num_iteration=n), q)
            if best is None or loss < best[0]:
                best = (loss, extra, n)

    val_loss, extra, rounds = best
    ds = lgb.Dataset(data.matrix("train", names), label=data.y_train,
                     categorical_feature=cats, free_raw_data=False)
    booster = lgb.train(_params(q, extra), ds, num_boost_round=rounds)
    chosen = {"rounds": rounds, "val_pinball": round(val_loss, 4), **extra}
    return booster, booster.predict(data.matrix("test", names)), chosen


def importances(booster, names) -> list[tuple[str, float]]:
    gain = booster.feature_importance("gain")
    total = float(gain.sum()) or 1.0
    return sorted(((n, float(g) / total * 100.0) for n, g in zip(names, gain)),
                  key=lambda x: -x[1])


# ------------------------------------------------------------------ export

def export_table(data: Data, booster, names: tuple[str, ...]):
    """The chosen model as a lookup the optimiser reads without lightgbm.

    Every block / direction / load state / wagon family the training days
    actually contain, with the model's prediction for it. Combinations the
    corridor never ran are not invented - the loader falls back instead, and
    says so.
    """
    bi, di = data.idx["block"], data.idx["direction"]
    fi, li, kmi = data.idx["family"], data.idx["loaded"], data.idx["block_km"]

    km_of, counts = {}, defaultdict(int)
    for r in data.rows_train:
        key = (r[bi], r[di], r[fi], r[li])
        km_of[key] = r[kmi]
        counts[key] += 1

    keys = sorted(km_of)
    cols = {
        "block": np.array([data.levels["block"].get(k[0], -1) for k in keys],
                          dtype=np.int32),
        "direction": np.array([data.levels["direction"].get(k[1], -1)
                               for k in keys], dtype=np.int32),
        "family": np.array([data.levels["family"].get(k[2], -1) for k in keys],
                           dtype=np.int32),
        "loaded": np.array([k[3] for k in keys], dtype=np.float64),
        "block_km": np.array([km_of[k] for k in keys], dtype=np.float64),
    }
    pred = booster.predict(np.column_stack([cols[c] for c in names]))

    table, support = {}, {}
    for k, p in zip(keys, pred):
        name = f"{k[0]}|{k[1]}|{k[2]}|{k[3]}"
        table[name] = round(float(p), 2)
        support[name] = counts[k]
    return table, support


def block_fallback(table: dict[str, float]) -> dict[str, float]:
    """One figure per block, for a rake whose family the table has never seen.

    The median over that block's known cells rather than a fresh prediction:
    a prediction would need a family code to feed the model, and inventing
    one is how a fallback quietly becomes a guess about a specific wagon.
    """
    per: dict[str, list[float]] = defaultdict(list)
    for name, v in table.items():
        per[name.split("|")[0]].append(v)
    return {k: round(float(np.median(v)), 2) for k, v in per.items()}


# --------------------------------------------------------------------- run

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--val-days", type=int, default=5)
    args = ap.parse_args()

    raw = json.loads(DATASET.read_text(encoding="utf-8"))
    data = Data(raw, args.test_days, args.val_days)

    print("=" * 76)
    print("BLOCK TRAVERSAL ESTIMATOR")
    print("=" * 76)
    print(f"\nfit    : {len(data.rows_fit):>6} rows  "
          f"{data.fit_days[0]} .. {data.fit_days[-1]}")
    print(f"choose : {len(data.rows_val):>6} rows  "
          f"{data.val_days[0]} .. {data.val_days[-1]}   (tuning only)")
    print(f"report : {len(data.rows_test):>6} rows  "
          f"{data.test_days[0]} .. {data.test_days[-1]}   (never seen)")

    const_tbl = constant_table()
    const = constant_model(data, const_tbl)
    y = data.y_test

    order = ("constant", "block", "cells", "cell", "full")
    scores: dict[str, dict[str, float]] = {}
    keep, tuning = {}, {}
    for q in QUANTILES:
        row = {"constant": pinball(y, const, q),
               "block": pinball(y, empirical_model(data, q), q),
               "cells": pinball(y, cells_model(data, q), q)}
        for tag, names in (("cell", CELL_FEATURES), ("full", FULL_FEATURES)):
            b, pred, chosen = lgbm_tuned(data, q, names)
            row[tag] = pinball(y, pred, q)
            keep[(q, tag)] = (b, names)
            tuning[f"p{int(q * 100)}/{tag}"] = chosen
        scores[f"p{int(q * 100)}"] = row

    print("\n" + "-" * 76)
    print("PINBALL LOSS ON THE HELD-OUT WEEK  (lower is better)")
    print("-" * 76)
    print(f"{'':<7}" + "".join(f"{c:>12}" for c in order) + f"{'best':>11}")
    for name, row in scores.items():
        best = min(row, key=row.get)
        print(f"{name:<7}" + "".join(f"{row[c]:>12.3f}" for c in order)
              + f"{best:>11}")

    p20 = scores["p20"]
    win = min(("cell", "full"), key=lambda t: p20[t])
    vs_const = (p20["constant"] - p20[win]) / p20["constant"] * 100
    vs_block = (p20["block"] - p20[win]) / p20["block"] * 100

    print("\n" + "-" * 76)
    print("WHAT THE SOLVER CARES ABOUT  (p20)")
    print("-" * 76)
    print(f"against the constant model : {vs_const:+5.1f}%   "
          f"({p20['constant']:.3f} -> {p20[win]:.3f})")
    print(f"against a plain lookup     : {vs_block:+5.1f}%   "
          f"({p20['block']:.3f} -> {p20[win]:.3f})")

    print("\n" + "-" * 76)
    print("WHAT TUNING CHOSE, ON THE VALIDATION DAYS")
    print("-" * 76)
    for k, v in tuning.items():
        print(f"  {k:<10} rounds {v['rounds']:<5} leaves {v['num_leaves']:<4}"
              f" min_leaf {v['min_data_in_leaf']:<5} l2 {v['lambda_l2']:<6}"
              f" val {v['val_pinball']}")

    print("\n" + "-" * 76)
    print("WHERE THE SIGNAL IS  (p20, share of split gain)")
    print("-" * 76)
    for tag in ("cell", "full"):
        b, names = keep[(SOLVER_Q, tag)]
        parts = ", ".join(f"{n} {v:.0f}%" for n, v in importances(b, names)
                          if v >= 1.0)
        print(f"  {tag:<6}: {parts}")

    # Ship the simpler model unless the richer one is clearly better. Clearly
    # is 2%: below that the gap is smaller than the spread between settings
    # in the grid above, and buying it costs the hour-of-entry feature, which
    # is a congestion proxy the optimiser must not be handed.
    use = "full" if p20["full"] < p20["cell"] * 0.98 else "cell"
    booster, _ = keep[(SOLVER_Q, use)]
    print(f"\nshipping   : {use}")
    if use == "cell":
        print("the richer features did not pay for themselves, so the")
        print("estimator ships as a lookup table and the solver loads no model")

    table, support = export_table(data, booster, CELL_FEATURES)
    fallback = block_fallback(table)

    model = {
        "quantile": SOLVER_Q,
        "trained_on": [data.train_days[0], data.train_days[-1]],
        "rows": len(data.rows_train),
        "key": "block|direction|family|loaded",
        "minutes": table,
        "support": support,
        "block_fallback": fallback,
        "note": ("Minutes to cross the block, at the 20th percentile of what "
                 "goods trains actually took. This REPLACES the constant "
                 "speed model INCLUDING its permanent speed restriction "
                 "penalty: the restriction is already in these observations, "
                 "so adding the penalty on top would count it twice."),
    }
    (DERIVED / "traversal_model.json").write_text(
        json.dumps(model, indent=1), encoding="utf-8")

    report = {
        "scores": scores,
        "shipped": use,
        "tuning": tuning,
        "fit_days": data.fit_days,
        "val_days": data.val_days,
        "test_days": data.test_days,
        "rows": {"fit": len(data.rows_fit), "val": len(data.rows_val),
                 "test": len(data.rows_test)},
        "p20_vs_constant_pct": round(vs_const, 1),
        "p20_vs_block_lookup_pct": round(vs_block, 1),
        "importance": {tag: importances(keep[(SOLVER_Q, tag)][0],
                                        keep[(SOLVER_Q, tag)][1])
                       for tag in ("cell", "full")},
        "constant_model": {k: round(v, 2) for k, v in sorted(const_tbl.items())},
    }
    (DERIVED / "traversal_report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")

    print(f"\ncells      : {len(table)} over {len(fallback)} blocks")
    print(f"wrote {DERIVED / 'traversal_model.json'}")
    print(f"wrote {DERIVED / 'traversal_report.json'}")


if __name__ == "__main__":
    main()
