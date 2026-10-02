"""Tests for the broadened failure taxonomy, build-log parsers, and the
LLM fallback classifier (LLM is stubbed -- no network)."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import CATEGORIES, Failure
from src.parsers import (
    parse_any,
    parse_generic_log,
    parse_gradle_log,
    parse_maven_log,
    parse_npm_log,
    parse_typescript_log,
)
from src.rule_triage import (
    build_fallback_prompt,
    classify,
    classify_with_llm_fallback,
    root_cause_and_fix,
    triage,
)


def cat_of(message, traceback=""):
    return classify(Failure(test_id="t", message=message, traceback=traceback))[0]


class TestNewTaxonomy(unittest.TestCase):
    def test_build_typescript(self):
        self.assertEqual(
            cat_of("src/app.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'."),
            "build")

    def test_build_javac(self):
        self.assertEqual(
            cat_of("[ERROR] /app/src/main/java/com/example/App.java:[12,5] error: ';' expected"),
            "build")

    def test_build_mypy(self):
        self.assertEqual(
            cat_of("mypy: src/app.py:10: error: Incompatible return value type [return-value]"),
            "build")

    def test_build_go(self):
        self.assertEqual(cat_of("go build failed: main.go:10:5: undefined: foo"), "build")

    def test_dependency_npm(self):
        self.assertEqual(
            cat_of("npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"),
            "dependency")

    def test_dependency_maven(self):
        self.assertEqual(
            cat_of("ERROR: Could not resolve dependencies for project com.example:demo:jar:1.0"),
            "dependency")

    def test_dependency_conda(self):
        self.assertEqual(
            cat_of("UnsatisfiableError: The following specifications were found to be incompatible"),
            "dependency")

    def test_infrastructure_oom(self):
        self.assertEqual(
            cat_of("Container OOMKilled: exceeded memory limit of 512Mi"),
            "infrastructure")

    def test_infrastructure_disk(self):
        self.assertEqual(cat_of("No space left on device: '/tmp/junit12345'"), "infrastructure")

    def test_infrastructure_docker(self):
        self.assertEqual(
            cat_of("Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
            "infrastructure")

    def test_infrastructure_runner(self):
        self.assertEqual(
            cat_of("The runner has received a shutdown signal and will stop"),
            "infrastructure")

    def test_http_rate_limit(self):
        self.assertEqual(cat_of("HTTP 429 Too Many Requests: slow down"), "http")

    def test_http_tls(self):
        self.assertEqual(
            cat_of("requests.exceptions.SSLError: CERTIFICATE_VERIFY_FAILED: certificate has expired"),
            "http")

    def test_http_unauthorized(self):
        self.assertEqual(cat_of("401 Unauthorized: Invalid API key"), "http")

    def test_http_dns(self):
        self.assertEqual(
            cat_of("curl: (6) Could not resolve host: api.example.com; Temporary failure in name resolution"),
            "http")

    def test_data_db(self):
        self.assertEqual(
            cat_of('psycopg2.OperationalError: FATAL: database "mydb" does not exist'),
            "data")

    def test_data_missing_file(self):
        self.assertEqual(
            cat_of("FileNotFoundError: [Errno 2] No such file or directory: 'fixtures/users.json'"),
            "data")

    def test_data_schema(self):
        self.assertEqual(
            cat_of('relation "orders" does not exist'),
            "data")

    def test_concurrency_deadlock(self):
        self.assertEqual(
            cat_of("DeadlockDetected: deadlock detected while waiting for lock"),
            "concurrency")

    def test_concurrency_race(self):
        self.assertEqual(cat_of("WARNING: DATA RACE in cache.get"), "concurrency")

    def test_concurrency_lock_timeout(self):
        self.assertEqual(
            cat_of("ORA-00060: deadlock detected while waiting for resource"),
            "concurrency")

    def test_framework_skipped_assumption(self):
        self.assertEqual(
            cat_of("SKIPPED [1] tests/test_x.py: assumption violated: needs GPU"),
            "framework")

    def test_framework_no_tests(self):
        self.assertEqual(cat_of("collected 0 items / 1 error"), "framework")

    def test_root_cause_templates_cover_new_categories(self):
        for cat in ("build", "dependency", "infrastructure", "http", "data",
                    "concurrency", "framework"):
            cause, fix = root_cause_and_fix(cat)
            self.assertNotIn("Could not determine", cause)
            self.assertTrue(fix)

    def test_categories_tuple_updated(self):
        for cat in ("build", "dependency", "infrastructure", "http", "data",
                    "concurrency", "framework"):
            self.assertIn(cat, CATEGORIES)


class TestBuildLogParsers(unittest.TestCase):
    def test_maven_file_errors(self):
        text = ("[ERROR] /app/src/main/java/com/example/App.java:[12,5] error: ';' expected\n"
                "[ERROR] /app/src/main/java/com/example/App.java:[13,9] error: cannot find symbol\n")
        failures = parse_maven_log(text)
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0].test_id, "/app/src/main/java/com/example/App.java:12")
        self.assertIn("';' expected", failures[0].message)

    def test_maven_build_failure_fallback(self):
        text = ("[INFO] BUILD FAILURE\n"
                "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test "
                "(default-test) on project demo\n")
        failures = parse_maven_log(text)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].test_id, "maven:build")

    def test_gradle_errors_and_failed_task(self):
        text = ("> Task :app:compileJava FAILED\n"
                "/app/src/main/java/com/example/App.java:10: error: ';' expected\n")
        failures = parse_gradle_log(text)
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0].test_id, "gradle::app:compileJava")
        self.assertEqual(failures[1].test_id, "/app/src/main/java/com/example/App.java:10")

    def test_npm_err_block(self):
        text = ("npm ERR! code ERESOLVE\n"
                "npm ERR! ERESOLVE unable to resolve dependency tree\n"
                "npm ERR!\n"
                "npm ERR! Fix the upstream dependency conflict\n")
        failures = parse_npm_log(text)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].test_id, "npm:ERESOLVE")
        self.assertTrue(failures[0].message.startswith("code ERESOLVE"))

    def test_npm_no_code(self):
        failures = parse_npm_log("npm ERR! Something broke badly\n")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].test_id, "npm:install")

    def test_typescript_errors(self):
        text = ("src/app.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
                "src/util.ts(3,1): error TS2304: Cannot find name 'foo'.\n")
        failures = parse_typescript_log(text)
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0].test_id, "src/app.ts:10")
        self.assertTrue(failures[0].message.startswith("TS2322:"))

    def test_parse_any_dispatches_build_logs(self):
        text = "[ERROR] /app/Foo.java:[7,3] error: ';' expected\n"
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            failures = parse_any(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].source, "maven log")

    def test_build_parsers_empty_on_unrelated_text(self):
        text = "hello world\nnothing to see here\n"
        self.assertEqual(parse_maven_log(text), [])
        self.assertEqual(parse_gradle_log(text), [])
        self.assertEqual(parse_npm_log(text), [])
        self.assertEqual(parse_typescript_log(text), [])


class _FakeCompletions:
    def __init__(self, raw_content):
        self.raw_content = raw_content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = SimpleNamespace(content=self.raw_content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _FakeClient:
    def __init__(self, raw_content):
        self.chat = SimpleNamespace(completions=_FakeCompletions(raw_content))


class TestLlmFallback(unittest.TestCase):
    def test_prompt_lists_full_taxonomy(self):
        prompt = build_fallback_prompt(
            Failure(test_id="t", message="something completely bizarre", traceback="xyz"))
        for cat in CATEGORIES:
            self.assertIn(cat, prompt)
        self.assertIn("something completely bizarre", prompt)
        self.assertIn("JSON", prompt)

    def test_rule_hit_never_calls_llm(self):
        def boom_client():
            raise AssertionError("LLM should not be called")

        class BoomClient:
            @property
            def chat(self):
                boom_client()

        result = classify_with_llm_fallback(
            Failure(test_id="t", message="TimeoutException: timed out after 30s"),
            client=BoomClient())
        self.assertEqual(result[0], "timeout")

    def test_offline_safe_without_credentials(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            result = classify_with_llm_fallback(
                Failure(test_id="t", message="something completely bizarre"))
        self.assertIsNone(result)

    def test_stub_client_classifies_unknown(self):
        client = _FakeClient(json.dumps({
            "category": "infrastructure",
            "cause": "the CI runner ran out of memory",
        }))
        result = classify_with_llm_fallback(
            Failure(test_id="t", message="something completely bizarre",
                    traceback="weird log line"),
            client=client)
        self.assertEqual(result[0], "infrastructure")
        self.assertEqual(result[1], 0.6)
        # the stub saw the prompt with the full taxonomy
        sent = client.chat.completions.calls[0]
        user_text = sent["messages"][1]["content"]
        self.assertIn("infrastructure", user_text)

    def test_stub_client_rejects_invalid_category(self):
        client = _FakeClient(json.dumps({"category": "aliens", "cause": "ufo"}))
        result = classify_with_llm_fallback(
            Failure(test_id="t", message="something completely bizarre"),
            client=client)
        self.assertIsNone(result)

    def test_stub_client_bad_json_returns_none(self):
        client = _FakeClient("this is not json")
        result = classify_with_llm_fallback(
            Failure(test_id="t", message="something completely bizarre"),
            client=client)
        self.assertIsNone(result)


class GenericLogFallbackTest(unittest.TestCase):
    """parse_generic_log must surface *something* for non-traceback logs,
    otherwise the triage engine never sees infra/build one-liners."""

    def _triage_text(self, text):
        failures = parse_generic_log(text)
        self.assertTrue(failures, "expected at least one failure parsed")
        return triage(failures)[0].category

    def test_oom_one_liner(self):
        self.assertEqual(
            self._triage_text("Container OOMKilled exit code 137"),
            "infrastructure")

    def test_docker_daemon(self):
        self.assertEqual(
            self._triage_text("Cannot connect to the Docker daemon at "
                              "unix:///var/run/docker.sock"),
            "infrastructure")

    def test_deadlock_one_liner(self):
        self.assertEqual(
            self._triage_text("DeadlockDetected: deadlock detected"),
            "concurrency")

    def test_tls_error(self):
        self.assertEqual(
            self._triage_text("SSLError: CERTIFICATE_VERIFY_FAILED"),
            "http")

    def test_clean_log_yields_nothing(self):
        self.assertEqual(parse_generic_log("all tests passed\nBUILD SUCCESS\n"), [])

    def test_traceback_blocks_still_win(self):
        text = ("Traceback (most recent call last):\n"
                '  File "x.py", line 1, in main\n'
                "AssertionError: boom\n"
                "some OOMKilled noise after")
        failures = parse_generic_log(text)
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].test_id.startswith("x.py"))


if __name__ == "__main__":
    unittest.main()
