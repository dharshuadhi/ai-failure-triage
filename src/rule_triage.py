"""Offline rule-based triage: classify, detect flakes, suggest owners, assign priority."""

import json
import re

from .models import TriageResult

# (category, [regexes], confidence, root-cause template, fix template)
PATTERNS = [
    ("timeout",
     [r"\btimed?\s?out\b", r"TimeoutException", r"wait.*exceeded", r"deadline exceeded"],
     0.9,
     "The test waited for something that never completed — slow page, hung service, or too-short wait.",
     "Increase the explicit wait, check for backend slowness, and confirm the app reaches a ready state."),
    ("locator",
     [r"NoSuchElement", r"Unable to locate element", r"StaleElementReference", r"locator", r"selector.*not found"],
     0.9,
     "A UI locator no longer matches — the page changed or the element loads late.",
     "Update the selector to a stable attribute and add an explicit wait for visibility."),
    ("connection",
     [r"ConnectionError", r"connection refused", r"Max retries exceeded", r"ECONNREFUSED",
      r"NameResolution", r"Failed to establish", r"socket.*timeout", r"502|503|504"],
     0.85,
     "The test could not reach a service — network, DNS, or the service is down.",
     "Check service health and test-environment networking; add retries for transient blips."),
    ("assertion",
     [r"AssertionError", r"\bassert\b", r"expected.*but (?:was|got)", r"to equal", r"mismatch"],
     0.8,
     "An assertion failed — the app behaved differently than the test expects.",
     "Verify whether the app changed intentionally (update the test) or this is a real regression."),
    ("setup",
     [r"error in setup", r"fixture.*error", r"setUp.*(error|fail)", r"before.*hook.*fail"],
     0.75,
     "The test never really ran — setup or teardown failed.",
     "Fix the fixture/environment setup; check test data and service prerequisites."),
    ("environment",
     [r"ModuleNotFoundError", r"ImportError", r"pip.*(failed|error)", r"uv.*(failed|error)",
      r"pre-commit.*(failed|error)", r"tox.*(failed|error)", r"dependency.*(conflict|resolution)",
      r"externally-managed-environment", r"Could not (resolve|install|download)",
      r"ECONNRESET.*registry", r"npm ERR!", r"action.*failed"],
     0.7,
     "The CI job failed before tests ran — broken environment, dependencies, or tooling.",
     "Check the failed step's log: pin or update dependencies, fix the CI config."),
]

FIXTURE_HINTS = {
    "timeout": "Flaky-prone: correlate with infra metrics before calling it a product bug.",
    "locator": "Often a test-maintenance issue rather than a product bug.",
}


def classify(failure) -> tuple:
    """Returns (category, confidence, evidence list)."""
    blob = f"{failure.message}\n{failure.traceback}"
    best = ("unknown", 0.4, [])
    for category, regexes, confidence, _cause, _fix in PATTERNS:
        hits = [r for r in regexes if re.search(r, blob, re.IGNORECASE)]
        if hits and confidence > best[1]:
            best = (category, confidence, hits)
    return best


def root_cause_and_fix(category: str) -> tuple:
    for cat, _rx, _c, cause, fix in PATTERNS:
        if cat == category:
            hint = FIXTURE_HINTS.get(cat, "")
            return cause, (fix + (" " + hint if hint else ""))
    return ("Could not determine the failure type from the available output.",
            "Re-run with full logs and stack traces, then triage again.")


def is_flaky(test_id: str, history: dict) -> bool:
    """A test is flaky if recent runs mix pass and fail (needs >=3 runs, >=1 of each)."""
    runs = history.get(test_id, [])
    if len(runs) < 3:
        return False
    return any(r == "pass" for r in runs) and any(r == "fail" for r in runs)


def suggest_owner(test_id: str, owner_map: dict) -> str:
    for prefix, owner in owner_map.items():
        if test_id.startswith(prefix):
            return owner
    return owner_map.get("*", "unassigned")


def assign_priority(category: str, flaky: bool, history: dict, test_id: str) -> str:
    recent_fails = sum(1 for r in history.get(test_id, [])[:5] if r == "fail")
    if category in ("connection", "setup") and recent_fails >= 3:
        return "P1"  # systemic: everything is red
    if flaky:
        return "P3"  # quarantine, don't block the release
    if category == "assertion":
        return "P2"
    return "P3"


def triage(failures: list, history: dict = None, owner_map: dict = None) -> list:
    """Run the full offline triage pipeline over parsed failures."""
    history = history or {}
    owner_map = owner_map or {}
    results = []
    for f in failures:
        category, confidence, evidence = classify(f)
        flaky = is_flaky(f.test_id, history)
        cause, fix = root_cause_and_fix(category)
        if flaky:
            fix = "Marked flaky — quarantine the test and investigate separately. " + fix
        results.append(TriageResult(
            test_id=f.test_id,
            category=category,
            confidence=confidence,
            flaky=flaky,
            owner=suggest_owner(f.test_id, owner_map),
            priority=assign_priority(category, flaky, history, f.test_id),
            root_cause=cause,
            suggested_fix=fix,
            evidence=evidence,
        ))
    return results


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
