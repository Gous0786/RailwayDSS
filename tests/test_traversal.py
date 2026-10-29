"""
Guards on the learned traversal estimator.

Most of these exist because the ways this can go wrong are silent. A
double-counted speed restriction, a quantile that drifted up towards the
mean, or a family string that stopped matching the one the training data was
built with all produce a working system that quietly believes the section is
fuller than it is. None of them raise.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))

from pravah.section import Direction, SectionGraph       # noqa: E402
from pravah.traversal import (MIN_SUPPORT, TraversalEstimator,  # noqa: E402
                              family_of)

MODEL = ROOT / "data" / "derived" / "traversal_model.json"
DATASET = ROOT / "data" / "derived" / "traversal_dataset.json"

pytestmark = pytest.mark.skipif(
    not MODEL.exists(),
    reason="estimator not trained; run analysis/train_traversal.py")


@pytest.fixture(scope="module")
def est() -> TraversalEstimator:
    return TraversalEstimator.load()


@pytest.fixture(scope="module")
def g() -> SectionGraph:
    return SectionGraph.load()


@pytest.fixture(scope="module")
def observed() -> dict[str, list[float]]:
    if not DATASET.exists():
        pytest.skip("dataset not built")
    raw = json.loads(DATASET.read_text(encoding="utf-8"))
    i = {c: n for n, c in enumerate(raw["columns"])}
    out: dict[str, list[float]] = defaultdict(list)
    for r in raw["rows"]:
        out[r[i["block"]]].append(r[i["minutes"]])
    return out


def test_the_model_loads(est):
    assert est is not None
    assert est.quantile == 0.2
    assert len(est.blocks) == 24


def test_a_missing_model_is_not_fatal(tmp_path):
    """A clone that never trained must still solve, on the constant model."""
    assert TraversalEstimator.load(tmp_path / "nope.json") is None


def test_the_family_function_matches_the_training_data(est):
    """The table is keyed on the family string, so the two must agree.

    traversal.py and build_traversal_dataset.py each carry one. If they drift
    apart every lookup misses, every rake silently falls back to the block
    figure, and nothing anywhere reports a problem.
    """
    from build_traversal_dataset import family_of as training_family

    for w in ("BOXN", "BOXNHL25T", "BCNAHSM1", "BRN22.9", "BFNSM1", "BTPN",
              "BLCA", "BOBYN", "NMGHS", "SHRN", "", "ZZZZ"):
        assert family_of(w) == training_family(w), w


def test_every_key_in_the_table_is_well_formed(est):
    for key in est.cells:
        block, direction, family, loaded = key.split("|")
        assert direction in ("DN", "UP")
        assert loaded in ("0", "1")
        assert block in est.blocks


def test_the_speed_restriction_is_not_counted_twice(g):
    """KUK-VZM carries the corridor's one big PSR, a 337 second penalty.

    The learned time is measured from trains that ran UNDER that restriction,
    so it already contains it. If the penalty were being added on top as well
    the learned figure would land above the constant model rather than below
    it - and it would do so on the block the plan most often turns on.
    """
    link = g.link("KUK", "VZM")
    line = link.lines[0].name
    assert g.psr_for(link.id, Direction.DOWN) is not None

    learned = g.running_time(link.id, line, "FREIGHT", Direction.DOWN,
                             wagon_type="BOXN", loaded=True)
    bare = SectionGraph(g.stations, g.links, g.psr, estimator=None)
    constant = bare.running_time(link.id, line, "FREIGHT", Direction.DOWN)
    assert learned < constant


def test_the_estimate_is_a_low_quantile_not_an_average(est, observed):
    """Every learned time must sit below that block's observed median.

    This is the assumption the optimiser is built on: it is handed a
    technical minimum and schedules the delay itself. An estimator that
    crept up towards the mean would hand it the queue as well, and the
    solver would add it a second time.
    """
    for block, minutes in est.blocks.items():
        if len(observed.get(block, [])) < 30:
            continue
        assert minutes < statistics.median(observed[block]), block


def test_a_loaded_rake_is_never_quicker_than_an_empty_one(est):
    """Physically it cannot be, so a table that says so is overfitted.

    Checked only where both cells carry real support - below that the
    quantile is a rumour and the loader ignores the cell anyway.
    """
    for key, loaded_min in est.cells.items():
        block, direction, family, loaded = key.split("|")
        if loaded != "1":
            continue
        empty_key = f"{block}|{direction}|{family}|0"
        if empty_key not in est.cells:
            continue
        if min(est.support.get(key, 0),
               est.support.get(empty_key, 0)) < MIN_SUPPORT:
            continue
        assert loaded_min >= est.cells[empty_key] - 1e-9, key


def test_the_graded_block_still_shows_the_load_effect(g):
    """KUK-VZM is the one block where load state measurably matters.

    It was +4.0 minutes at p20 in the training days and +4.0 again in the
    held-out week, which is why conditioning on the rake is in the model at
    all. If this collapses, the cell table has stopped earning its place and
    the plain per-block lookup is the better thing to ship.
    """
    link = g.link("KUK", "VZM")
    line = link.lines[0].name
    full = g.running_time(link.id, line, "FREIGHT", Direction.DOWN,
                          wagon_type="BOXN", loaded=True)
    empty = g.running_time(link.id, line, "FREIGHT", Direction.DOWN,
                           wagon_type="BOXN", loaded=False)
    assert full - empty > 1.5


def test_an_unknown_wagon_falls_back_to_the_block(est, g):
    link = g.link("KUK", "VZM")
    line = link.lines[0].name
    unknown = g.running_time(link.id, line, "FREIGHT", Direction.DOWN,
                             wagon_type="QQQQ", loaded=True)
    assert unknown == pytest.approx(est.blocks["KUK-VZM"])


def test_an_unknown_block_is_reported_as_a_miss(est):
    before = sum(est.misses.values())
    assert est.minutes("XXX", "YYY", "DN", "BOXN", True) is None
    assert sum(est.misses.values()) == before + 1


def test_thin_cells_are_not_trusted(est):
    """A cell the corridor ran twice is the model's prior, not evidence."""
    thin = [k for k, n in est.support.items() if n < MIN_SUPPORT]
    for key in thin[:20]:
        block, direction, family, loaded = key.split("|")
        got = est.minutes(*block.split("-"), direction,
                          family, loaded == "1")
        assert got == pytest.approx(est.blocks[block])


def test_passenger_times_are_untouched(g):
    """The estimator was trained on goods movements and must not touch
    passenger services, whose published times drive the fixed obstacles."""
    bare = SectionGraph(g.stations, g.links, g.psr, estimator=None)
    for link in list(g.links.values())[:6]:
        for cls in ("SUF", "MEX", "PAS"):
            a = g.running_time(link.id, link.lines[0].name, cls)
            b = bare.running_time(link.id, link.lines[0].name, cls)
            assert a == b


def test_the_planner_and_the_verifier_agree(g):
    """Both must ask the estimator the same question about the same rake.

    When they do not, the verifier rejects movements the planner built on a
    different figure and every freight path fails its own check. That is
    exactly what happened when the schedulers were first wired up without
    recording what the rake was.
    """
    from pravah.demand import DemandBook

    book = DemandBook.load()
    rake = book.busiest().rakes[0]
    plan = rake.to_train_plan()
    assert plan.wagon_type == rake.wagon_type
    assert plan.loaded == rake.loaded

    link = g.link("KUK", "VZM")
    line = link.lines[0].name
    planner = g.running_time(link.id, line, "FREIGHT", Direction.DOWN,
                             wagon_type=rake.wagon_type, loaded=rake.loaded)
    verifier = g.running_time(link.id, line, "FREIGHT", Direction.DOWN,
                              wagon_type=plan.wagon_type, loaded=plan.loaded)
    assert planner == verifier


def test_the_rake_survives_a_round_trip_through_json(g):
    """The verifier runs on plans loaded from disk as well as in memory."""
    from pravah.plan import TrainPlan

    t = TrainPlan(id="x", train_class="FREIGHT", direction=Direction.DOWN,
                  wagon_type="BOXNHL", loaded=False)
    back = TrainPlan.from_dict(json.loads(json.dumps(t.to_dict())))
    assert back.wagon_type == "BOXNHL"
    assert back.loaded is False
