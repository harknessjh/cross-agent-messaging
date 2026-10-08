# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Run one CI shard of the offline unittest suite.

Usage: python .github/scripts/test_shard.py K/N

The suite is the one found by
``python -m unittest discover --start-directory tests``. Whole test modules
are assigned to N shards, slowest first, using the recorded durations below.
Before running anything, every shard checks that the N shards together hold
exactly the discovered tests, each once. A shard prints its modules first and
their measured durations last, which is what to copy into RECORDED_SECONDS
when the shards drift out of balance.
"""

from __future__ import annotations

import sys
import time
import unittest
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"

# Seconds per module on the Python 3.14 main run for 554191a. Modules not
# listed take about a second or are new; they count as DEFAULT_SECONDS.
RECORDED_SECONDS = {
    "test_cam1_transport_outcomes": 244,
    "test_cam1_reliability_reproductions": 204,
    "test_cam1_late_rejection_evidence": 193,
    "test_cam1_transport_project_discovery": 145,
    "test_cam1_transport_project_send_guards": 70,
    "test_cam1_conversation_transport": 70,
    "test_cam1_compatibility_cli": 67,
    "test_cam1_transport_project_lifecycle": 66,
    "test_cam1_capture": 55,
    "test_cam1_project_cli": 44,
    "test_cam1_transport_project_roster": 38,
    "test_cam1_project_ingest": 37,
    "test_cam1_transport_project_audit": 31,
    "test_cam1_cli": 29,
    "test_cam1_onboarding": 26,
    "test_cam1_cross_feature": 19,
    "test_cam1_causal_integration": 17,
    "test_cam1_profile": 17,
    "test_cam1_outstanding": 13,
    "test_cam1_product_approvals": 6,
    "test_cam1_project_journal": 6,
    "test_cam1_transport_cli": 4,
    "test_cam1_project": 4,
    "test_cam1_product_approval_recovery": 3,
    "test_cam1_state": 2,
}
DEFAULT_SECONDS = 1


class TimedResult(unittest.TextTestResult):
    """Text result that also adds up wall time per test module."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.module_seconds: dict[str, float] = {}
        self._started = 0.0

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        module = test.id().split(".", 1)[0]
        elapsed = time.perf_counter() - self._started
        self.module_seconds[module] = self.module_seconds.get(module, 0.0) + elapsed


def parse_shard(text: str) -> tuple[int, int]:
    index_text, separator, count_text = text.partition("/")
    if not separator or not index_text.isdigit() or not count_text.isdigit():
        raise SystemExit(f"shard must look like K/N, got {text!r}")
    index, count = int(index_text), int(count_text)
    if not 1 <= index <= count:
        raise SystemExit(f"shard {index} is outside 1..{count}")
    return index, count


def test_ids(suite: unittest.TestSuite) -> list[str]:
    ids = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            ids.extend(test_ids(item))
        else:
            ids.append(item.id())
    return ids


def assign(modules: list[str], count: int) -> list[list[str]]:
    """Longest-first assignment of whole modules to the least-loaded shard."""
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0] * count
    for module in sorted(
        modules, key=lambda name: (-RECORDED_SECONDS.get(name, DEFAULT_SECONDS), name)
    ):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        shards[target].append(module)
        loads[target] += RECORDED_SECONDS.get(module, DEFAULT_SECONDS)
    return shards


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: python .github/scripts/test_shard.py K/N")
    index, count = parse_shard(argv[0])

    # Import as `python -m unittest` does from the repository root, where
    # the tests find the `tools` package.
    sys.path[0] = str(REPO_ROOT)
    loader = unittest.TestLoader()
    # Discovery also puts tests/ on sys.path, as before sharding.
    discovered = test_ids(loader.discover(start_dir=str(TESTS_DIR)))
    if loader.errors:
        print("test discovery failed:")
        for error in loader.errors:
            print(error)
        return 1
    modules = sorted(path.stem for path in TESTS_DIR.glob("test*.py"))
    suites = {module: loader.loadTestsFromName(module) for module in modules}

    shards = assign(modules, count)
    assigned = [test_id for module in modules for test_id in test_ids(suites[module])]
    if Counter(assigned) != Counter(discovered):
        missing = sorted((Counter(discovered) - Counter(assigned)).elements())
        extra = sorted((Counter(assigned) - Counter(discovered)).elements())
        print(f"shards do not match discovery: missing={missing} extra={extra}")
        return 1

    mine = shards[index - 1]
    if not mine:
        print(f"shard {index}/{count} has no modules; use fewer shards")
        return 1
    suite = unittest.TestSuite(suites[module] for module in mine)
    recorded = sum(RECORDED_SECONDS.get(module, DEFAULT_SECONDS) for module in mine)
    print(
        f"Shard {index}/{count}: {len(mine)} of {len(modules)} modules, "
        f"{suite.countTestCases()} of {len(discovered)} tests, "
        f"about {recorded} s recorded",
        flush=True,
    )
    for module in mine:
        print(f"  {module}", flush=True)

    runner = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult)
    result = runner.run(suite)

    print("Module durations (seconds):")
    for module, seconds in sorted(
        result.module_seconds.items(), key=lambda item: -item[1]
    ):
        print(f"  {module}: {seconds:.0f}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
