"""Parse test reports and logs into Failure objects."""

import re
import xml.etree.ElementTree as ET

from .models import Failure

FAILED_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+?)\s*-\s*(.*)$")
TRACE_HEADER_RE = re.compile(r"^_{2,}\s*(.+?)\s*_{2,}$")
# GitHub Actions / CI log lines are prefixed with an RFC3339 timestamp.
TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+")


def _strip_ts(line: str) -> str:
    return TS_PREFIX_RE.sub("", line)


def parse_junit_xml(path: str) -> list:
    """Parse a JUnit XML report (pytest --junitxml, surefire, etc.)."""
    failures = []
    tree = ET.parse(path)
    for tc in tree.iter("testcase"):
        failed = tc.find("failure")
        errored = tc.find("error")
        node = failed if failed is not None else errored
        if node is None:
            continue
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        failures.append(Failure(
            test_id=test_id,
            suite=classname,
            message=(node.get("message") or "").strip().splitlines()[0] if node.get("message") else "",
            traceback=(node.text or "").strip(),
            duration=float(tc.get("time", 0) or 0),
            source=path,
        ))
    return failures


def parse_pytest_text(text: str) -> list:
    """Parse `pytest -q` style output: FAILED/ERROR summary lines + tracebacks."""
    failures = []
    tracebacks = {}
    current = None
    buf = []
    for raw in text.splitlines():
        line = _strip_ts(raw)
        m = TRACE_HEADER_RE.match(line.strip())
        if m:
            if current:
                tracebacks[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
            continue
        if current is not None:
            if line.startswith("=") and line.strip("= "):
                tracebacks[current] = "\n".join(buf).strip()
                current = None
                buf = []
            else:
                buf.append(line)
    if current:
        tracebacks[current] = "\n".join(buf).strip()

    for raw in text.splitlines():
        line = _strip_ts(raw)
        m = FAILED_LINE_RE.match(line.strip())
        if not m:
            continue
        test_id, message = m.group(2), m.group(3)
        tb = ""
        short = test_id.split("::")[-1]
        for key, val in tracebacks.items():
            if short in key or key in test_id:
                tb = val
                break
        failures.append(Failure(
            test_id=test_id,
            suite="::".join(test_id.split("::")[:-1]),
            message=message.strip(),
            traceback=tb,
            source="pytest output",
        ))
    return failures


def parse_generic_log(text: str) -> list:
    """Best-effort extraction of failure blocks from a raw log.

    First tries Python traceback blocks; if the log has none (e.g. an infra
    or build log), falls back to the lines that look like errors so there is
    always *something* for the triage engine to classify.
    """
    failures = []
    blocks = re.split(r"(?=Traceback \(most recent call last\))", text)
    for i, block in enumerate(blocks[1:], 1):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue
        last = lines[-1]
        err = re.match(r"([\w.]+(?:Error|Exception|Failure|Timeout))(?::\s*(.*))?", last)
        message = last.strip() if not err else (err.group(2) or err.group(1)).strip()
        test_id = f"log-trace-{i}"
        m = re.search(r'File "([^"]+)", line \d+, in (\w+)', block)
        if m:
            test_id = f"{m.group(1)}::{m.group(2)}"
        failures.append(Failure(
            test_id=test_id,
            message=message,
            traceback=block.strip(),
            source="log",
        ))
    if failures:
        return failures

    # No traceback blocks: harvest error-looking lines instead.
    lines = text.splitlines()
    err_idx = [i for i, l in enumerate(lines)
               if re.search(r"(?i)\w*(error|exception|fail(?:ed|ure)?|fatal|panic|oomkilled|timeout|denied|refused|"
                            r"not found|missing|deadlock|daemon|killed|abort|cancelled|unreachable)\b", l)]
    if not err_idx:
        return []
    first = err_idx[0]
    context = lines[max(0, first - 3):first + 4]
    message = lines[first].strip()[:300]
    failures.append(Failure(
        test_id="log-error-1",
        message=message,
        traceback="\n".join(context).strip(),
        source="log",
    ))
    return failures


def parse_any(path: str) -> list:
    """Dispatch on file type."""
    if path.endswith(".xml"):
        return parse_junit_xml(path)
    text = open(path, encoding="utf-8", errors="replace").read()
    failures = parse_pytest_text(text)
    if failures:
        return failures
    # Build logs can mix tools (e.g. a monorepo CI log), so aggregate
    # across all build parsers instead of stopping at the first hit.
    for build_parser in (parse_maven_log, parse_gradle_log, parse_npm_log, parse_typescript_log):
        failures.extend(build_parser(text))
    return failures or parse_generic_log(text)


# --- Build-log parsers -----------------------------------------------------

MAVEN_ERROR_RE = re.compile(r"^\[ERROR\]\s+(\S+?):\[(\d+)(?:,\d+)?\]\s*(.*)$")
GRADLE_ERROR_RE = re.compile(r"^(\S+\.java):(\d+):\s*error:\s*(.*)$")
GRADLE_TASK_RE = re.compile(r"^>\s*Task\s+(\S+)\s+FAILED$")
NPM_ERR_RE = re.compile(r"^npm ERR!\s*(.*)$")
TS_ERROR_RE = re.compile(r"^([^(]+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.*)$")


def parse_maven_log(text: str) -> list:
    """Parse Maven output: `[ERROR] path/File.java:[line,col] message` lines.

    Falls back to a single build-level failure when no per-file errors exist.
    """
    failures = []
    for raw in text.splitlines():
        line = _strip_ts(raw).strip()
        m = MAVEN_ERROR_RE.match(line)
        if m:
            failures.append(Failure(
                test_id=f"{m.group(1)}:{m.group(2)}",
                suite="maven",
                message=m.group(3).strip(),
                traceback=line,
                source="maven log",
            ))
    if not failures:
        for raw in text.splitlines():
            line = _strip_ts(raw).strip()
            if "BUILD FAILURE" in line or "Failed to execute goal" in line:
                failures.append(Failure(
                    test_id="maven:build",
                    suite="maven",
                    message=line.lstrip("[ERROR] ").strip(),
                    traceback=line,
                    source="maven log",
                ))
                break
    return failures


def parse_gradle_log(text: str) -> list:
    """Parse Gradle output: `Foo.java:10: error: ...` lines and `> Task :x FAILED`."""
    failures = []
    for raw in text.splitlines():
        line = _strip_ts(raw).strip()
        m = GRADLE_ERROR_RE.match(line)
        if m:
            failures.append(Failure(
                test_id=f"{m.group(1)}:{m.group(2)}",
                suite="gradle",
                message=m.group(3).strip(),
                traceback=line,
                source="gradle log",
            ))
            continue
        m = GRADLE_TASK_RE.match(line)
        if m:
            failures.append(Failure(
                test_id=f"gradle:{m.group(1)}",
                suite="gradle",
                message=f"Task {m.group(1)} failed",
                traceback=line,
                source="gradle log",
            ))
    return failures


def parse_npm_log(text: str) -> list:
    """Parse npm output: consecutive `npm ERR!` lines form one failure."""
    failures = []
    buf, code = [], None

    def flush():
        nonlocal buf, code
        if buf:
            failures.append(Failure(
                test_id=f"npm:{code}" if code else "npm:install",
                suite="npm",
                message=buf[0],
                traceback="\n".join(buf),
                source="npm log",
            ))
            buf, code = [], None

    for raw in text.splitlines():
        line = _strip_ts(raw).strip()
        m = NPM_ERR_RE.match(line)
        if m:
            detail = m.group(1).strip()
            cm = re.match(r"code\s+(\S+)", detail, re.IGNORECASE)
            if cm:
                code = cm.group(1)
            buf.append(detail if detail else line)
        elif buf and line:
            flush()
    flush()
    return failures


def parse_typescript_log(text: str) -> list:
    """Parse tsc output: `src/file.ts(10,5): error TS2322: message`."""
    failures = []
    for raw in text.splitlines():
        line = _strip_ts(raw).strip()
        m = TS_ERROR_RE.match(line)
        if m:
            failures.append(Failure(
                test_id=f"{m.group(1)}:{m.group(2)}",
                suite="tsc",
                message=f"{m.group(4)}: {m.group(5).strip()}",
                traceback=line,
                source="tsc log",
            ))
    return failures


def iter_junit_results(path: str):
    """Yield (test_id, passed) for every testcase in a JUnit XML report."""
    tree = ET.parse(path)
    for tc in tree.iter("testcase"):
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        failed = tc.find("failure") is not None or tc.find("error") is not None
        skipped = tc.find("skipped") is not None
        if not skipped:
            yield test_id, not failed
