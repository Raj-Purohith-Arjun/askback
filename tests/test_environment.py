"""Does this run actually test what it claims to?

Every other test file asserts something about the framework. This one asserts
something about the run itself: that no module quietly fell out of collection.
The invariant is the same one the framework applies to the agents it grades -
unmeasurable never passes - turned on its author.

The collection assertions only hold with every optional extra installed, so they
are marked `strict_env` and run under `--strict-env` (CI) rather than locally.
"""

from __future__ import annotations

import pytest

from optional_deps import COLLECTED, INSTALL_HINT, MISSING, load_baseline, subset_selected


def _needs_the_whole_suite() -> None:
    """A run narrowed by path, -k or -m collects less by design, which says
    nothing about whether a module has gone missing."""
    if subset_selected():
        pytest.skip("the collection baseline applies to a full run only")


def test_the_baseline_file_is_self_consistent() -> None:
    """Guards the gate itself: a baseline whose total undercuts its own parts
    would pass while a whole module was missing."""
    baseline = load_baseline()
    per_module = baseline["minimum_per_module"]

    assert per_module, "the baseline records no modules, so it asserts nothing"
    assert baseline["minimum_total"] >= sum(per_module.values())


@pytest.mark.strict_env
def test_no_module_was_skipped_for_a_missing_dependency() -> None:
    assert not MISSING, "\n".join(
        [f"{len(MISSING)} test module(s) did not run. Install with: {INSTALL_HINT}"]
        + [f"  {m.test_module}: needs '{m.dependency}' from [{m.extra}]" for m in MISSING]
    )


@pytest.mark.strict_env
def test_every_recorded_module_was_collected() -> None:
    _needs_the_whole_suite()
    expected = set(load_baseline()["minimum_per_module"])
    missing = sorted(expected - set(COLLECTED))
    assert not missing, f"recorded test module(s) collected nothing: {missing}"


@pytest.mark.strict_env
def test_each_module_meets_its_recorded_minimum() -> None:
    _needs_the_whole_suite()
    short = {
        module: (COLLECTED.get(module, 0), minimum)
        for module, minimum in load_baseline()["minimum_per_module"].items()
        if COLLECTED.get(module, 0) < minimum
    }
    assert not short, f"module(s) collected fewer tests than recorded (got, expected): {short}"


@pytest.mark.strict_env
def test_the_total_meets_the_recorded_minimum() -> None:
    _needs_the_whole_suite()
    minimum = int(load_baseline()["minimum_total"])
    total = sum(COLLECTED.values())
    assert total >= minimum, f"collected {total} tests, baseline records at least {minimum}"
