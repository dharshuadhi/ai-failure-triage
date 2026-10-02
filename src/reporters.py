"""Render triage results as Markdown or JSON."""

import json
from dataclasses import asdict


def render_markdown(results, summary) -> str:
    lines = ["# Failure Triage Report", ""]
    lines.append(f"**{summary['total']}** failures triaged "
                 f"({summary['flaky']} flaky).")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|---|---|")
    for cat, n in sorted(summary["by_category"].items()):
        lines.append(f"| {cat} | {n} |")
    lines.append("")
    lines.append("| Priority | Count |")
    lines.append("|---|---|")
    for prio in ("P1", "P2", "P3", "P4"):
        if prio in summary["by_priority"]:
            lines.append(f"| {prio} | {summary['by_priority'][prio]} |")
    lines.append("")
    order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
    for r in sorted(results, key=lambda x: order.get(x.priority, 4)):
        lines.append(f"## [{r.priority}] {r.test_id}")
        lines.append("")
        flaky = "YES — quarantine" if r.flaky else "no"
        lines.append(f"- **Category:** {r.category} (confidence {r.confidence:.0%})")
        lines.append(f"- **Flaky:** {flaky}")
        lines.append(f"- **Owner:** {r.owner or 'unassigned'}")
        lines.append(f"- **Root cause:** {r.root_cause}")
        lines.append(f"- **Suggested fix:** {r.suggested_fix}")
        if r.agent_notes:
            lines.append(f"- **Agent notes:** {r.agent_notes}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(results, summary) -> str:
    return json.dumps({
        "summary": summary,
        "results": [asdict(r) for r in results],
    }, indent=2) + "\n"
