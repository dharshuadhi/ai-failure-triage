"""Tests for parsers, rule triage and reporters."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import Failure
from src.parsers import parse_junit_xml, parse_pytest_text
from src.pipeline import run, summarize
from src import reporters
from src.rule_triage import classify, is_flaky, triage

HERE = os.path.dirname(os.path.abspath(__file__))
XML = os.path.join(HERE, "..", "examples", "sample_results.xml")
HISTORY = os.path.join(HERE, "..", "examples", "sample_history.json")
OWNERS = os.path.join(HERE, "..", "examples", "sample_owners.json")


class TestParsers(unittest.TestCase):
    def test_junit_xml(self):
        failures = parse_junit_xml(XML)
        self.assertEqual(len(failures), 4)
        ids = [f.test_id for f in failures]
        self.assertIn("tests.test_login::test_invalid_password", ids)

    def test_pytest_text(self):
        text = ("FAILED tests/test_x.py::test_a - AssertionError: boom\n"
                "ERROR tests/test_x.py::test_b - setup failed\n")
        failures = parse_pytest_text(text)
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0].message, "AssertionError: boom")


class TestRuleTriage(unittest.TestCase):
    def test_classify_timeout(self):
        f = Failure(test_id="t", message="TimeoutException: timed out after 30s")
        cat, conf, _ev = classify(f)
        self.assertEqual(cat, "timeout")
        self.assertGreaterEqual(conf, 0.8)

    def test_classify_locator(self):
        f = Failure(test_id="t", message="NoSuchElementException: Unable to locate element")
        self.assertEqual(classify(f)[0], "locator")

    def test_flaky(self):
        self.assertTrue(is_flaky("a", {"a": ["pass", "fail", "pass"]}))
        self.assertFalse(is_flaky("b", {"b": ["fail", "fail", "fail"]}))
        self.assertFalse(is_flaky("c", {"c": ["fail"]}))

    def test_pipeline_end_to_end(self):
        from src.rule_triage import load_json
        failures, results = run([XML], load_json(HISTORY), load_json(OWNERS), engine="rule")
        self.assertEqual(len(results), 4)
        by_id = {r.test_id: r for r in results}
        self.assertEqual(by_id["tests.test_login::test_invalid_password"].category, "assertion")
        self.assertTrue(by_id["tests.test_login::test_invalid_password"].flaky)
        self.assertEqual(by_id["tests.test_api::test_create_order"].priority, "P1")
        self.assertEqual(by_id["tests.test_checkout::test_apply_coupon"].owner, "payments-team")
        summary = summarize(results)
        self.assertEqual(summary["total"], 4)
        md = reporters.render_markdown(results, summary)
        self.assertIn("# Failure Triage Report", md)
        js = reporters.render_json(results, summary)
        self.assertIn("test_create_order", js)


if __name__ == "__main__":
    unittest.main()
