"""Tools the AI agent can call while investigating a failure."""

import re


def build_tools(failures, history):
    """Return (tool_schemas, executors) bound to this triage run's data."""
    by_id = {f.test_id: f for f in failures}

    schemas = [
        {"type": "function", "function": {
            "name": "get_failure",
            "description": "Get the full message and stack trace of one failed test.",
            "parameters": {"type": "object", "properties": {
                "test_id": {"type": "string", "description": "The failing test's id"}},
                "required": ["test_id"]}}},
        {"type": "function", "function": {
            "name": "search_traces",
            "description": "Search all failure stack traces for a regex pattern.",
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "Regex to search for"}},
                "required": ["pattern"]}}},
        {"type": "function", "function": {
            "name": "lookup_history",
            "description": "Get recent pass/fail runs for a test to judge flakiness.",
            "parameters": {"type": "object", "properties": {
                "test_id": {"type": "string"}},
                "required": ["test_id"]}}},
        {"type": "function", "function": {
            "name": "blast_radius",
            "description": "List every failing test whose output matches a pattern — finds systemic issues.",
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string"}},
                "required": ["pattern"]}}},
    ]

    def get_failure(test_id):
        f = by_id.get(test_id)
        if not f:
            return f"Unknown test id. Known: {sorted(by_id)[:20]}"
        return f"MESSAGE: {f.message}\nTRACEBACK:\n{f.traceback[:3000]}"

    def search_traces(pattern):
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Bad regex: {e}"
        hits = [t for t, f in by_id.items()
                if rx.search(f.message) or rx.search(f.traceback)]
        return f"Matched {len(hits)} failures: {hits[:20]}" if hits else "No matches."

    def lookup_history(test_id):
        runs = history.get(test_id)
        if not runs:
            return "No history for this test."
        fails = sum(1 for r in runs if r == "fail")
        return f"Last {len(runs)} runs: {fails} failed, {len(runs)-fails} passed. Recent: {runs[:10]}"

    def blast_radius(pattern):
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Bad regex: {e}"
        hits = [t for t, f in by_id.items()
                if rx.search(f.message) or rx.search(f.traceback)]
        if len(hits) > len(by_id) / 2:
            return (f"{len(hits)}/{len(by_id)} failures match '{pattern}' — "
                    "this looks SYSTEMIC (infra/environment), not a product bug.")
        return f"{len(hits)}/{len(by_id)} failures match '{pattern}': {hits[:20]}"

    return schemas, {
        "get_failure": get_failure,
        "search_traces": search_traces,
        "lookup_history": lookup_history,
        "blast_radius": blast_radius,
    }
