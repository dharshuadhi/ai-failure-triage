"""Offline rule-based triage: classify, detect flakes, suggest owners, assign priority."""

import json
import re

from .models import CATEGORIES, TriageResult

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
    ("build",
     [r"error TS\d+", r"\.java:[^\n]*error:", r"\.go:\d+:\d+:", r"\bjavac\b",
      r"\bmypy\b", r"\beslint\b", r"error:\s*cannot find symbol",
      r"Failed to execute goal.*compil", r"compilation failed"],
     0.85,
     "The code does not compile — a syntax or type error broke the build.",
     "Fix the reported file/line error and re-run the compiler locally before pushing."),
    ("dependency",
     [r"npm ERR!|yarn.*error|pnpm.*ERR", r"ERESOLVE|E404|ENOTFOUND.*registry",
      r"No matching distribution found", r"pip.*Could not find a version",
      r"Could not resolve dependencies", r"Failed to (?:resolve|download) dependencies",
      r"gradle.*Could not resolve", r"UnsatisfiableError", r"poetry.*(?:install|lock).*(?:failed|error)",
      r"bundle install.*(?:failed|error)", r"go: .*downloading.*(?:failed|error)"],
     0.8,
     "Dependency installation failed — a package is missing, pinned wrong, or the registry is unreachable.",
     "Check the lockfile and version pins, verify registry access, and clear the package cache if stale."),
    ("infrastructure",
     [r"OOMKilled|out of memory|heap out of memory|MemoryError",
      r"No space left on device|ENOSPC|disk.*full|no space left",
      r"Cannot connect to the Docker daemon|docker.*daemon.*(?:error|not running)",
      r"runner.*(?:lost|went offline|unreachable)|The runner has received a shutdown",
      r"cancelled.*(?:workflow|job|run)|Lost communication with the server"],
     0.85,
     "The machine or CI runner itself failed — out of memory, out of disk, or the runner went away.",
     "Give the job more memory/disk, clean up artifacts, and re-run; alert infra if runners keep dying."),
    ("http",
     [r"\b4\d\d\b", r"\b5\d\d\b", r"\b429\b", r"Too Many Requests",
      r"rate.?limit(?:ed|ing|s)?", r"Unauthorized|Forbidden|401|403",
      r"Invalid (?:API key|token|credentials)",
      r"SSLError|CERTIFICATE_VERIFY_FAILED|certificate.*(?:expired|invalid|verify failed)|self.signed certificate",
      r"TLS.*(?:error|fail|handshake)", r"\bENOTFOUND\b|getaddrinfo|Name or service not known",
      r"Temporary failure in name resolution"],
     0.8,
     "An HTTP/API call failed — bad request, auth problem, rate limit, or TLS/DNS trouble.",
     "Check the status code: fix the request or credentials for 4xx, retry with backoff for 429/5xx, "
     "and verify certs and DNS for TLS errors."),
    ("data",
     [r"OperationalError|ProgrammingError|IntegrityError|DataError",
      r"psycopg2|pymysql|sqlite3\.", r"could not connect to (?:server|database)",
      r"database.*(?:unavailable|does not exist)", r"FATAL:.*database",
      r"relation .* does not exist|no such table|Unknown column|column .* does not exist",
      r"FileNotFoundError|No such file or directory|ENOENT",
      r"missing (?:fixture|file|data|table)", r"schema.*mismatch|mismatched schema",
      r"syntax error.*SQL|SQL.*syntax error"],
     0.8,
     "A data problem — database unreachable, query invalid, or a file/fixture the test needs is missing.",
     "Verify the database is up and migrated, check the query, and make sure fixtures and data files exist."),
    ("concurrency",
     [r"deadlock|DeadlockDetected|Lock wait timeout|lock.*timeout",
      r"race condition|data race|WARNING: DATA RACE|ConcurrentModification",
      r"thread.*starv|livelock"],
     0.85,
     "Threads or transactions collided — deadlock, race condition, or lock starvation.",
     "Serialize the critical section, add lock timeouts, and reproduce under load before fixing."),
    ("framework",
     [r"AssumptionViolated|TestAborted|assumption.*(?:violated|failed)",
      r"no tests (?:ran|were collected|found)|collected 0 items",
      r"\bSKIPPED\b|was skipped", r"Errors:\s*[1-9]",
      r"invalid test|duplicate test|discovery.*fail"],
     0.65,
     "The test framework itself reported a problem — skipped assumption, collection error, or runner error.",
     "Check why the framework bailed out: bad test IDs, collection imports, or violated assumptions."),
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


def build_fallback_prompt(failure) -> str:
    """Build the prompt used when the rule engine returns 'unknown'.

    Pure function (no network) so tests can assert on the prompt directly.
    """
    categories = ", ".join(CATEGORIES)
    excerpt = (failure.traceback or "")[:2000]
    return (
        f"Classify this failure into exactly one of these categories: {categories}.\n"
        f"test_id: {failure.test_id}\n"
        f"message: {failure.message}\n"
        f"log excerpt:\n{excerpt}\n"
        'Reply with JSON only: {"category": "<one of the categories>", '
        '"cause": "<one-sentence root cause>"}.'
    )


def classify_with_llm_fallback(failure, client=None, deployment="gpt-4o"):
    """Rule-based classify(); if it returns 'unknown', ask the LLM.

    Returns (category, confidence, evidence), or None when the LLM is
    unavailable (no credentials, no openai package, or a bad reply).
    Offline-safe: never raises for missing configuration. Pass a stub
    `client` in tests to avoid network calls.
    """
    category, confidence, evidence = classify(failure)
    if category != "unknown":
        return category, confidence, evidence
    if client is None:
        try:
            from .agents import _client
            client, deployment = _client()
        except (RuntimeError, ImportError):
            return None
    try:
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system",
                 "content": "You classify software failure logs. Reply with JSON only."},
                {"role": "user", "content": build_fallback_prompt(failure)},
            ],
            temperature=0.0,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None
    valid = [cat for cat, _rx, _c, _cause, _fix in PATTERNS] + ["unknown"]
    chosen = str(data.get("category", "unknown")).strip().lower()
    if chosen not in valid:
        return None
    cause = str(data.get("cause", "")).strip()
    return chosen, 0.6, (["llm-classified"] + [cause] if cause else ["llm-classified"])


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
