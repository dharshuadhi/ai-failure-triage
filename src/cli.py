"""CLI: triage test failures from the terminal.

Examples:
    python -m src.cli -i results.xml --history history.json -o triage.md
    python -m src.cli -i pytest.log --format json --engine rule
    python -m src.cli -i results/ --owners owners.json
"""

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from src import reporters
from src.pipeline import run, summarize
from src.rule_triage import load_json
from src import rule_triage


def collect_inputs(pattern: str) -> list:
    if os.path.isdir(pattern):
        files = sorted(glob.glob(os.path.join(pattern, "*")))
    else:
        files = sorted(glob.glob(pattern)) or [pattern]
    return [f for f in files if os.path.isfile(f)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agentic failure analysis & triage")
    ap.add_argument("-i", "--input",
                    help="Report file, glob, or directory (JUnit XML, pytest text, logs)")
    ap.add_argument("--history", help="JSON: {test_id: ['pass','fail',...]} for flaky detection")
    ap.add_argument("--owners", help="JSON: {test_id_prefix: owner} for owner suggestion")
    ap.add_argument("--engine", choices=["auto", "ai", "rule"], default="auto")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("-o", "--output", help="Write report to file (default: stdout)")
    ap.add_argument("--cluster", action="store_true",
                    help="Group failures by stack-trace fingerprint")
    ap.add_argument("--bug-reports", metavar="DIR",
                    help="Write one draft bug report per failure cluster into DIR")
    ap.add_argument("--baseline", metavar="FILE",
                    help="Compare against a previous JSON report (baseline diff)")
    ap.add_argument("--analytics", action="store_true",
                    help="Record this run (JUnit XML) and print the flaky leaderboard")
    ap.add_argument("--redis-url", help="Redis URL for analytics (default: in-memory)")
    ap.add_argument("--ci", metavar="OWNER/REPO",
                    help="Triage live failures from a repo's GitHub Actions runs")
    ap.add_argument("--ci-limit", type=int, default=3,
                    help="How many failed runs to pull with --ci (default: 3)")
    ap.add_argument("--flake-stats", action="store_true",
                    help="Wilson-score flake verdicts from --history")
    ap.add_argument("--file-issues", metavar="OWNER/REPO",
                    help="File one GitHub issue per failure cluster (needs GITHUB_TOKEN)")
    ap.add_argument("--no-dry-run", action="store_true",
                    help="Actually file issues (default is dry-run preview)")
    args = ap.parse_args(argv)
    if not args.input and not args.ci:
        ap.error("one of -i/--input or --ci is required")

    paths = collect_inputs(args.input) if args.input else []
    if args.input and not paths:
        print(f"No input files matched: {args.input}", file=sys.stderr)
        return 2

    history = load_json(args.history) if args.history else {}
    owner_map = load_json(args.owners) if args.owners else {}

    if args.ci:
        from src.ci_providers import GitHubActionsProvider
        provider = GitHubActionsProvider(args.ci, token=os.environ.get("GITHUB_TOKEN"))
        failures = provider.collect_failures(limit=args.ci_limit)
        if not failures:
            print(f"No parseable failures in recent runs of {args.ci}.", file=sys.stderr)
            return 1
        print(f"Pulled {len(failures)} failures from {args.ci} live runs.", file=sys.stderr)
        results = rule_triage.triage(failures, history, owner_map)
    else:
        failures, results = run(paths, history, owner_map, args.engine)
        if not failures:
            print("No failures found in input.", file=sys.stderr)
            return 1

    summary = summarize(results)
    text = (reporters.render_markdown(results, summary) if args.format == "markdown"
            else reporters.render_json(results, summary))

    extras = []
    if args.cluster:
        from src.fingerprint import cluster, render_clusters
        groups = cluster(failures)
        extras.append(render_clusters(groups))
    if args.baseline:
        from src.bugreports import baseline_diff
        extras.append(baseline_diff(results, load_json(args.baseline)))
    if args.bug_reports:
        from src.bugreports import draft_bug_report
        from src.fingerprint import cluster
        os.makedirs(args.bug_reports, exist_ok=True)
        groups = cluster(failures)
        by_id = {r.test_id: r for r in results}
        for fp, members in groups.items():
            path = os.path.join(args.bug_reports, f"bug-{fp}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(draft_bug_report(fp, members, by_id))
        print(f"Wrote {len(groups)} bug report draft(s) -> {args.bug_reports}/", file=sys.stderr)
    if args.analytics:
        from src.analytics import FailureAnalytics
        from src.parsers import iter_junit_results
        analytics = FailureAnalytics(args.redis_url)
        test_ids = set()
        for path in paths:
            if path.endswith(".xml"):
                for test_id, passed in iter_junit_results(path):
                    analytics.record(test_id, passed)
                    test_ids.add(test_id)
        if test_ids:
            board = analytics.leaderboard(sorted(test_ids))
            extras.append("## Flaky leaderboard (backend: %s)" % analytics.backend)
            extras.append("| Test | Runs | Fails | Flake rate |")
            extras.append("|---|---|---|---|")
            for row in board:
                extras.append(f"| {row['test_id']} | {row['runs']} | {row['fails']} | "
                              f"{row['flake_rate']:.0%} |")
            extras.append(f"\nDistinct failing tests today (HyperLogLog): "
                          f"{analytics.failing_tests_today()}")
    if args.flake_stats and history:
        from src.stats import verdicts_from_history
        extras.append("## Flake verdicts (Wilson 95% CI)")
        extras.append("| Test | Runs | Fail | Rate | 95% CI | Verdict |")
        extras.append("|---|---|---|---|---|---|")
        for tid, v in sorted(verdicts_from_history(history).items(),
                             key=lambda kv: -kv[1]["flake_rate"])[:15]:
            lo, hi = v["ci95"]
            extras.append(f"| {tid} | {v['runs']} | {v['fails']} | {v['flake_rate']:.0%} | "
                          f"{lo:.0%}–{hi:.0%} | **{v['verdict']}** |")
    if args.file_issues:
        from src.fingerprint import cluster as _cluster
        from src.issues import file_cluster_issues
        token = os.environ.get("GITHUB_TOKEN")
        dry = not args.no_dry_run
        if not dry and not token:
            print("GITHUB_TOKEN is required to file issues.", file=sys.stderr)
            return 2
        filed = file_cluster_issues(args.file_issues, _cluster(failures),
                                    {r.test_id: r for r in results},
                                    token or "dry-run", dry_run=dry)
        extras.append("## Filed issues" + (" (dry-run)" if dry else ""))
        for f in filed:
            dest = f.get("url") or f["title"]
            extras.append(f"- {dest} [{f['failures']} failures, `{f['fingerprint']}`]")
    if extras:
        text = text.rstrip() + "\n\n" + "\n\n".join(extras)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Triaged {len(results)} failures -> {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
