"""Tests for fingerprinting, analytics and bug-report drafting."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analytics import FailureAnalytics
from src.bugreports import baseline_diff, draft_bug_report
from src.fingerprint import cluster, fingerprint
from src.models import Failure
from src.parsers import iter_junit_results
from src.pipeline import run
from src.rule_triage import load_json

HERE = os.path.dirname(os.path.abspath(__file__))
XML = os.path.join(HERE, "..", "examples", "sample_results.xml")


def _failure(msg="AssertionError: boom", tb=""):
    return Failure(test_id="t::x", message=msg, traceback=tb or
                   'Traceback (most recent call last):\n  File "a.py", line 42, in x\nAssertionError: boom')


class TestFingerprint(unittest.TestCase):
    def test_same_bug_different_lines_same_group(self):
        a = _failure(tb='Traceback:\n  File "a.py", line 42, in x\nAssertionError: boom')
        b = _failure(tb='Traceback:\n  File "a.py", line 97, in x\nAssertionError: boom')
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_different_bugs_different_groups(self):
        a = _failure("AssertionError: boom")
        b = _failure("TimeoutException: timed out")
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_cluster_groups(self):
        failures = [_failure("AssertionError: boom"), _failure("AssertionError: boom"),
                    _failure("TimeoutException: slow")]
        groups = cluster(failures)
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(len(v) for v in groups.values()), [1, 2])


class TestAnalytics(unittest.TestCase):
    def test_bitmap_flake_rate(self):
        a = FailureAnalytics()  # in-memory
        for passed in [True, False, True, False, False]:
            a.record("t::flaky", passed, day="2026-10-01")
        row = a.flake_rate("t::flaky", day="2026-10-01")
        self.assertEqual(row["runs"], 5)
        self.assertEqual(row["fails"], 3)
        self.assertAlmostEqual(row["flake_rate"], 0.6)

    def test_hll_counts_distinct_failures(self):
        a = FailureAnalytics()
        a.record("t::a", False, day="2026-10-01")
        a.record("t::b", False, day="2026-10-01")
        a.record("t::a", False, day="2026-10-01")
        self.assertEqual(a.failing_tests_today(day="2026-10-01"), 2)

    def test_leaderboard_ranks_flakiest_first(self):
        a = FailureAnalytics()
        for p in [True, False]:
            a.record("t::sometimes", p, day="2026-10-01")
        for p in [False, False]:
            a.record("t::always", p, day="2026-10-01")
        board = a.leaderboard(["t::sometimes", "t::always"], day="2026-10-01")
        self.assertEqual(board[0]["test_id"], "t::always")

    def test_junit_results_include_passes(self):
        results = list(iter_junit_results(XML))
        self.assertEqual(len(results), 6)  # 4 fail + 2 pass
        self.assertEqual(sum(1 for _, p in results if p), 2)


class TestBugReports(unittest.TestCase):
    def test_draft_contains_key_sections(self):
        failures, results = run([XML], load_json(os.path.join(HERE, "..", "examples", "sample_history.json")),
                                load_json(os.path.join(HERE, "..", "examples", "sample_owners.json")),
                                engine="rule")
        groups = cluster(failures)
        fp, members = next(iter(groups.items()))
        doc = draft_bug_report(fp, members, {r.test_id: r for r in results})
        self.assertIn("# [Bug]", doc)
        self.assertIn("## Root cause", doc)
        self.assertIn("## Evidence", doc)
        self.assertIn(fp, doc)

    def test_baseline_diff_flags_regressions(self):
        failures, results = run([XML], engine="rule")
        out = baseline_diff(results, {"by_category": {"assertion": 5}})
        self.assertIn("🔺", out)  # timeout/locator/connection are new vs baseline


if __name__ == "__main__":
    unittest.main()
