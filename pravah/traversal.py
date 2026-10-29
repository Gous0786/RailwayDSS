"""
The learned block traversal time.

`analysis/train_traversal.py` fits the model and writes its predictions out as
a table; this reads the table. Nothing here imports lightgbm, and the solver
never loads a model - the training run is offline, the artefact it leaves is
658 numbers, and a controller can read every one of them.

WHAT THE NUMBERS ARE

Minutes to cross one block, at the 20th percentile of what goods trains
actually took over twenty days in August 2025, conditioned on the block, the
direction, the wagon family and whether the rake was loaded.

The 20th percentile is the point of the whole exercise. The optimiser plans
the conflicts itself, so handing it an average - which on this corridor means
handing it the queue - makes it add the same delay twice and invent
congestion that is not there. What it needs is the clean-run time, and p20 is
that: low enough to be a technical minimum, high enough not to be an outlier.
Here is the shape of the problem on the worst block, KUK-VZM, 10.65 km:

    p10  20 min      p50  49 min      p90  154 min      max  642 min

A 10 km block does not take ten hours to run. Everything above p20 is a rake
standing somewhere inside it.

TWO WAYS TO DOUBLE-COUNT, BOTH SILENT

  1. The quantile, above.
  2. The permanent speed restriction. This table REPLACES the constant speed
     model outright, PSR penalty included - the restriction was in force when
     these trains ran, so it is already in the observations. Adding the
     recorded penalty on top of a learned time counts it twice, and it would
     land on KUK-VZM, which is both the restricted block and the one the
     plan usually turns on. It would look plausible and it would be wrong.

Neither mistake raises an error. Both make the section look fuller than it is.

WHAT IT IS WORTH

Against the constant model, 30.8% lower pinball loss on seven held-out days.
Against a plain per-block lookup, 0.8%. That second figure is the honest one
about the machine learning: nearly all of the gain is in knowing the BLOCK,
which a table knows too. Conditioning on the rake adds a little, and it is
concentrated where the physics says it should be - on KUK-VZM a loaded rake
needs 4 minutes more at p20 than an empty one, in the training days and in
the held-out week alike.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "data" / "derived" / "traversal_model.json"

# A cell seen fewer times than this during training is kept in the table but
# not trusted on its own; the block figure is used instead. The model will
# happily predict for a family it saw twice, and that prediction is mostly
# the model's own prior.
MIN_SUPPORT = 10

FAMILIES = ("BOXNHL", "BOXN", "BOBRN", "BOBYN", "BCNHL", "BCNA", "BCN",
            "BOST", "BOY", "BRN", "BFNS", "BFN", "BTAP", "BTPN", "BLCA",
            "BLCB", "BLC", "NMG", "SHRN")


def family_of(wagon_type: str) -> str:
    """The wagon family. Must match the one the training data was built with."""
    t = (wagon_type or "").strip().upper()
    for p in FAMILIES:
        if t.startswith(p):
            return p
    return t[:3] if t else "UNK"


class TraversalEstimator:
    """Learned freight running times, with a documented way of not knowing."""

    def __init__(self, data: dict):
        self.quantile = float(data.get("quantile", 0.2))
        self.cells: dict[str, float] = data.get("minutes", {})
        self.support: dict[str, int] = data.get("support", {})
        self.blocks: dict[str, float] = data.get("block_fallback", {})
        self.trained_on = data.get("trained_on", [])
        self.misses: dict[str, int] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> "TraversalEstimator | None":
        """The trained table, or None if it has not been built yet.

        Returning None rather than raising is deliberate: a clone of this
        repository that has never run the training step must still start the
        service and solve, falling back to the constant speed model. The
        learned times are an improvement on the baseline, not a dependency
        of it.
        """
        p = path or MODEL
        if not p.exists():
            return None
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def minutes(self, a: str, b: str, direction: str,
                wagon_type: str = "", loaded: bool = True) -> float | None:
        """Minutes for this rake over the block a->b, or None if unknown.

        None means the corridor has no record of this combination and the
        caller should fall back to the constant model. Misses are counted, so
        that a plan quietly running on fallbacks shows up as such instead of
        looking learned.
        """
        block = f"{a}-{b}"
        key = (f"{block}|{direction}|{family_of(wagon_type)}"
               f"|{1 if loaded else 0}")
        v = self.cells.get(key)
        if v is not None and self.support.get(key, 0) >= MIN_SUPPORT:
            return v
        v = self.blocks.get(block)
        if v is None:
            self.misses[block] = self.misses.get(block, 0) + 1
        return v

    def __repr__(self) -> str:
        span = " .. ".join(self.trained_on) if self.trained_on else "?"
        return (f"TraversalEstimator(p{self.quantile * 100:.0f}, "
                f"{len(self.cells)} cells over {len(self.blocks)} blocks, "
                f"{span})")
