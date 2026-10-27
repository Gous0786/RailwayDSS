"""
Recorded freight path tests.

These paths are the only thing the optimiser's plan is compared against, so a
malformed rebuild would quietly corrupt the comparison rather than fail. The
checks here are the invariants that make a path drawable and a comparison
honest: time runs forward, chainage lands on real stations, and the standing
time is a part of the transit rather than something added to it.

    pytest tests/test_observed.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pravah.section import SectionGraph  # noqa: E402

OBSERVED = (Path(__file__).resolve().parent.parent
            / "data" / "derived" / "observed_freight.json")

pytestmark = pytest.mark.skipif(
    not OBSERVED.exists(),
    reason="run analysis/build_observed_freight.py first")


@pytest.fixture(scope="module")
def blob() -> dict:
    return json.loads(OBSERVED.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def chainage() -> set[float]:
    return {round(s.cum_km, 2) for s in SectionGraph.load().stations}


def every_train(blob: dict):
    for day, rec in blob["days"].items():
        for tr in rec["trains"]:
            yield day, tr


def test_days_are_present(blob):
    assert blob["days"], "no recorded days at all"
    assert all(len(d) == 10 and d[4] == "-" for d in blob["days"])


def test_time_never_runs_backwards(blob):
    for day, tr in every_train(blob):
        times = [p[0] for p in tr["pts"]]
        assert times == sorted(times), f"{day} {tr['id']} goes back in time"


def test_every_point_is_a_real_station(blob, chainage):
    for day, tr in every_train(blob):
        for _, km in tr["pts"]:
            assert round(km, 2) in chainage, \
                f"{day} {tr['id']} at {km} km, which is not a station"


def test_a_path_needs_two_points(blob):
    for day, tr in every_train(blob):
        assert len(tr["pts"]) >= 2, f"{day} {tr['id']} is a single point"


def test_standing_time_is_part_of_transit(blob):
    # Standing time is counted inside the journey, not added to it. If this
    # ever inverts, the comparison against the plan overstates the gain.
    for day, tr in every_train(blob):
        assert tr["standing_min"] <= tr["transit_min"] + 1e-6, \
            f"{day} {tr['id']} stood longer than its whole transit"


def test_direction_matches_the_path(blob):
    for day, tr in every_train(blob):
        first, last = tr["pts"][0][1], tr["pts"][-1][1]
        if first == last:
            continue
        expected = "DN" if last > first else "UP"
        assert tr["dir"] == expected, \
            f"{day} {tr['id']} is marked {tr['dir']} but runs {expected}"


def test_transit_matches_the_points(blob):
    for day, tr in every_train(blob):
        span = (tr["pts"][-1][0] - tr["pts"][0][0]) / 60.0
        assert abs(span - tr["transit_min"]) < 1.0, \
            f"{day} {tr['id']} reports {tr['transit_min']} min, path says {span:.1f}"


def test_ids_are_unique_within_a_day(blob):
    for day, rec in blob["days"].items():
        ids = [t["id"] for t in rec["trains"]]
        assert len(ids) == len(set(ids)), f"{day} repeats a consignment id"


def test_stats_agree_with_the_trains(blob):
    for day, rec in blob["days"].items():
        assert rec["stats"]["rakes"] == len(rec["trains"]), \
            f"{day} stats count disagrees with its trains"
