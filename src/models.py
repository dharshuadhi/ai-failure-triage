"""Shared data models for the failure triage pipeline."""

from dataclasses import dataclass, field


@dataclass
class Failure:
    """A single failed test case extracted from a report or log."""
    test_id: str                      # e.g. tests/test_login.py::TestLogin::test_valid
    suite: str = ""                   # test class / module
    message: str = ""                 # one-line failure message
    traceback: str = ""               # full stack trace text
    duration: float = 0.0             # seconds
    source: str = ""                  # which file/report it came from


@dataclass
class TriageResult:
    """Triage verdict for one failure."""
    test_id: str
    category: str                     # see CATEGORIES below
    confidence: float                 # 0.0 - 1.0
    flaky: bool = False               # intermittent in recent history
    owner: str = ""                   # suggested owner / team
    priority: str = "P3"              # P1..P4
    root_cause: str = ""              # short explanation
    suggested_fix: str = ""           # recommended next step
    agent_notes: str = ""             # filled by the AI agent when used
    evidence: list = field(default_factory=list)  # matched signals


CATEGORIES = ("assertion", "timeout", "locator", "connection", "setup",
              "environment", "build", "dependency", "infrastructure", "http",
              "data", "concurrency", "framework", "unknown")
