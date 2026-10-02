"""AI triage agent: tool-calling loop over Azure OpenAI.

The agent investigates each failure with local tools (stack traces, history,
blast-radius search) and writes a root-cause verdict. Requires Azure OpenAI
credentials; without them the caller should fall back to the rule engine.
"""

import json
import os

from .models import CATEGORIES

SYSTEM_PROMPT = """You are a senior SDET triaging automated test failures.
For each failure, use your tools to investigate: read the stack trace,
check run history for flakiness, and search for systemic patterns across failures.
Then give a verdict as JSON with keys:
  category (assertion|timeout|locator|connection|setup|environment|build|dependency|infrastructure|http|data|concurrency|framework|unknown),
  root_cause (one sentence), suggested_fix (one sentence),
  flaky (true/false), priority (P1-P4).
Be decisive. Prefer evidence from tool output over guessing."""


def _client():
    try:
        from openai import AzureOpenAI
    except ImportError as exc:
        raise RuntimeError("openai package not installed") from exc
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    if not endpoint or not key:
        raise RuntimeError("Azure OpenAI not configured (AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)")
    return AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version="2024-06-01"), deployment


def investigate(failure, schemas, executors, max_steps=8) -> dict:
    """Run one agentic investigation loop for a single failure."""
    client, deployment = _client()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Investigate this failure and return your verdict as JSON.\n"
            f"test_id: {failure.test_id}\nmessage: {failure.message}")},
    ]
    verdict = {}
    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=deployment, messages=messages, tools=schemas, tool_choice="auto",
            temperature=0.2,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            try:
                verdict = json.loads(msg.content or "{}")
            except json.JSONDecodeError:
                verdict = {"agent_notes": msg.content or ""}
            break
        messages.append(msg)
        for call in msg.tool_calls:
            fn = executors.get(call.function.name)
            try:
                args = json.loads(call.function.arguments or "{}")
                result = fn(**args) if fn else f"Unknown tool {call.function.name}"
            except Exception as exc:  # noqa: BLE001 - surface tool errors to the agent
                result = f"Tool error: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": str(result)[:4000]})
    else:
        verdict = {"agent_notes": "Investigation hit the step limit."}
    return verdict if isinstance(verdict, dict) else {"agent_notes": str(verdict)}


def agent_triage(failures, schemas, executors, base_results):
    """Enrich rule-based results with one agent investigation per failure."""
    for failure, result in zip(failures, base_results):
        try:
            verdict = investigate(failure, schemas, executors)
        except RuntimeError as exc:
            result.agent_notes = f"AI agent unavailable: {exc}"
            continue
        if verdict.get("category") in CATEGORIES:
            result.category = verdict["category"]
        result.root_cause = verdict.get("root_cause", result.root_cause)
        result.suggested_fix = verdict.get("suggested_fix", result.suggested_fix)
        if isinstance(verdict.get("flaky"), bool):
            result.flaky = verdict["flaky"]
        if verdict.get("priority") in ("P1", "P2", "P3", "P4"):
            result.priority = verdict["priority"]
        result.agent_notes = verdict.get("agent_notes", "Verdict from AI investigation.")
    return base_results
