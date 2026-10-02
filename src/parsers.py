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
    """Best-effort extraction of stack-trace blocks from a raw log."""
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
    return failures


def parse_any(path: str) -> list:
    """Dispatch on file type."""
    if path.endswith(".xml"):
        return parse_junit_xml(path)
    text = open(path, encoding="utf-8", errors="replace").read()
    failures = parse_pytest_text(text)
    return failures or parse_generic_log(text)


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
