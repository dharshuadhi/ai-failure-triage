"""File triaged bug reports as real GitHub issues.

Closes the loop: fingerprint cluster -> draft report -> GitHub issue,
so triage output becomes tracked work instead of a markdown file nobody reads.
"""

import json
import urllib.request

from .bugreports import draft_bug_report

API = "https://api.github.com"


def file_issue(repo: str, title: str, body: str, token: str,
               labels=None, dry_run=True) -> dict:
    """Create a GitHub issue. dry_run=True prints what would be filed."""
    if dry_run:
        return {"dry_run": True, "repo": repo, "title": title,
                "labels": labels or [], "body_preview": body[:200]}
    data = json.dumps({"title": title, "body": body,
                       "labels": labels or []}).encode()
    req = urllib.request.Request(
        f"{API}/repos/{repo}/issues", data=data,
        headers={"User-Agent": "ai-failure-triage",
                 "Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    return {"dry_run": False, "url": result.get("html_url"),
            "number": result.get("number")}


def file_cluster_issues(repo: str, groups: dict, triage_by_id: dict,
                        token: str, labels=None, dry_run=True,
                        fingerprint_fn=None) -> list:
    """File one issue per failure cluster. Returns per-cluster results."""
    labels = labels or ["bug", "auto-triaged"]
    results = []
    for fp, members in groups.items():
        body = draft_bug_report(fp, members, triage_by_id)
        title = f"[Triage] {members[0].message[:80]} ({len(members)} failures)"
        results.append({**file_issue(repo, title, body, token, labels, dry_run),
                        "fingerprint": fp, "failures": len(members)})
    return results
