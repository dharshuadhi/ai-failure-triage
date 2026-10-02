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

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Triaged {len(results)} failures -> {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
