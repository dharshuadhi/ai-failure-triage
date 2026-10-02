"""Tests for stats, issue filing and the CI provider (mocked HTTP)."""

import io
import os
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ci_providers import GitHubActionsProvider, _first_error_line
from src.issues import file_cluster_issues, file_issue
from src.models import Failure
from src.stats import flake_verdict, wilson_interval


class TestStats(unittest.TestCase):
    def test_wilson_bounds(self):
        lo, hi = wilson_interval(0, 100)
        self.assertEqual(lo, 0.0)
        self.assertLess(hi, 0.05)
        lo, hi = wilson_interval(100, 100)
        self.assertGreater(lo, 0.9)

    def test_verdicts(self):
        self.assertEqual(flake_verdict(100, 0)["verdict"], "stable")
        self.assertEqual(flake_verdict(0, 100)["verdict"], "broken")
        self.assertEqual(flake_verdict(50, 50)["verdict"], "flaky")
        self.assertEqual(flake_verdict(2, 0)["verdict"], "insufficient-data")
        v = flake_verdict(95, 5)
        self.assertIn(v["verdict"], ("flaky", "suspect", "stable"))
        lo, hi = v["ci95"]
        self.assertLessEqual(lo, v["flake_rate"])
        self.assertGreaterEqual(hi, v["flake_rate"])


class TestIssues(unittest.TestCase):
    def test_dry_run_does_not_touch_network(self):
        r = file_issue("o/r", "title", "body", token="x", dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["title"], "title")

    def test_cluster_issues_dry_run(self):
        from src.fingerprint import cluster
        failures = [Failure(test_id="a::t1", message="boom", traceback="Traceback:\nAssertionError: boom")]
        groups = cluster(failures)
        out = file_cluster_issues("o/r", groups, {}, token="x", dry_run=True)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["dry_run"])
        self.assertIn("Triage", out[0]["title"])


class TestCIProvider(unittest.TestCase):
    def test_first_error_line_skips_generic_annotations(self):
        text = ("##[error]Process completed with exit code 1.\n"
                "ImportError: cannot import name 'x'")
        self.assertIn("ImportError", _first_error_line(text))

    def test_collect_parses_mock_log(self):
        log_text = ("2026-01-01T00:00:00Z FAILED tests/test_a.py::test_x - AssertionError: boom\n"
                    "2026-01-01T00:00:01Z ===== output =====\n")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("0_test.txt", log_text)
        zip_bytes = buf.getvalue()

        provider = GitHubActionsProvider("o/r")
        with patch("src.ci_providers._get") as mock_get, \
             patch("src.ci_providers._download", return_value=zip_bytes):
            mock_get.side_effect = [
                {"workflow_runs": [{"id": 1}]},
                {"jobs": [{"id": 10, "name": "tests", "conclusion": "failure"}]},
            ]
            failures = provider.collect_failures(limit=1)
        self.assertEqual(len(failures), 1)
        self.assertIn("test_x", failures[0].test_id)
        self.assertIn("o/r run 1", failures[0].source)


if __name__ == "__main__":
    unittest.main()
