"""Triage pipeline: parse -> triage (rule engine, optionally AI-enriched) -> results."""

import os

from . import rule_triage
from .parsers import parse_any


def _ai_configured() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_ENDPOINT")
                and os.environ.get("AZURE_OPENAI_API_KEY"))


def run(paths, history=None, owner_map=None, engine="auto"):
    """Triage every failure found in `paths`. Returns (failures, results)."""
    failures = []
    for path in paths:
        failures.extend(parse_any(path))
    if not failures:
        return [], []

    results = rule_triage.triage(failures, history or {}, owner_map or {})

    if engine == "auto":
        engine = "ai" if _ai_configured() else "rule"
    if engine == "ai" and failures:
        from . import agents
        from .tools import build_tools
        schemas, executors = build_tools(failures, history or {})
        results = agents.agent_triage(failures, schemas, executors, results)

    return failures, results


def summarize(results) -> dict:
    by_cat, by_prio = {}, {}
    for r in results:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1
        by_prio[r.priority] = by_prio.get(r.priority, 0) + 1
    return {
        "total": len(results),
        "flaky": sum(1 for r in results if r.flaky),
        "by_category": by_cat,
        "by_priority": by_prio,
    }
