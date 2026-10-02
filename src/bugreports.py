"""Draft bug reports from triaged failure clusters.

Turns a fingerprint group into a paste-ready bug report: title, environment,
repro, stack-trace evidence, and the triage verdict — the document an SDET
would otherwise assemble by hand.
"""

import datetime


def draft_bug_report(fingerprint_id, members, triage_by_id) -> str:
    """Build a markdown bug report for one failure cluster."""
    rep = members[0]
    t = triage_by_id.get(rep.test_id)
    category = t.category if t else "unknown"
    priority = t.priority if t else "P3"
    owner = t.owner if t and t.owner else "unassigned"
    root_cause = t.root_cause if t else ""
    fix = t.suggested_fix if t else ""

    affected = sorted({m.test_id for m in members})
    lines = [
        f"# [Bug] {rep.message[:90]}",
        "",
        f"- **Priority:** {priority}",
        f"- **Category:** {category}",
        f"- **Suggested owner:** {owner}",
        f"- **Fingerprint:** `{fingerprint_id}`",
        f"- **Affected tests ({len(affected)}):**",
    ]
    lines += [f"  - `{a}`" for a in affected]
    lines += [
        "",
        "## Root cause",
        root_cause or "_Fill in after investigation._",
        "",
        "## Suggested fix",
        fix or "_Fill in after investigation._",
        "",
        "## Evidence",
        "```",
        (rep.traceback or rep.message)[:2000],
        "```",
        "",
        f"_Auto-drafted by ai-failure-triage on {datetime.date.today().isoformat()}._",
    ]
    return "\n".join(lines).rstrip() + "\n"


def baseline_diff(current_results, baseline_summary: dict) -> str:
    """Compare this run's category counts against a baseline summary JSON."""
    cur = {}
    for r in current_results:
        cur[r.category] = cur.get(r.category, 0) + 1
    base = baseline_summary.get("by_category", {})
    lines = ["## Baseline diff", "", "| Category | Baseline | Now | Δ |",
             "|---|---|---|---|"]
    for cat in sorted(set(base) | set(cur)):
        b, c = base.get(cat, 0), cur.get(cat, 0)
        delta = c - b
        arrow = "🔺" if delta > 0 else "🟢" if delta < 0 else "➖"
        lines.append(f"| {cat} | {b} | {c} | {arrow} {delta:+d} |")
    new_cats = [c for c in cur if c not in base and cur[c] > 0]
    if new_cats:
        lines.append("")
        lines.append(f"⚠️ New failure categories since baseline: {', '.join(new_cats)}")
    return "\n".join(lines) + "\n"
