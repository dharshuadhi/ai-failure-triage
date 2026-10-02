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


def collect_inputs(pattern: str) -> list:
    if os.path.isdir(pattern):
        files = sorted(glob.glob(os.path.join(pattern, "*")))
    else:
        files = sorted(glob.glob(pattern)) or [pattern]
    return [f for f in files if os.path.isfile(f)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agentic failure analysis & triage")
    ap.add_argument("-i", "--input", required=True,
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
    args = ap.parse_args(argv)

    paths = collect_inputs(args.input)
    if not paths:
        print(f"No input files matched: {args.input}", file=sys.stderr)
        return 2

    history = load_json(args.history) if args.history else {}
    owner_map = load_json(args.owners) if args.owners else {}

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
