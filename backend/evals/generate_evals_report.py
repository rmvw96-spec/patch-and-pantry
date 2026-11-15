# ruff: noqa: T201  -- a command-line tool: printing is its output
"""Build the HTML report for one golden-eval run from its results file.

    uv run python -m evals --json results.json
    uv run python -m evals.generate_evals_report results.json -o report.html

The page is self-contained: open it in a browser, upload it as a workflow artifact, or publish it.
"""

import argparse
import sys
from pathlib import Path

from evals.report import render_report
from evals.results import load_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "results", type=Path, help="a results file from python -m evals --json"
    )
    parser.add_argument(
        "-o", "--output", type=Path, required=True, help="the HTML file to write"
    )
    parser.add_argument("--title", default="Typed list evals")
    parser.add_argument(
        "--note", action="append", default=[], help="a note for the page (repeatable)"
    )
    args = parser.parse_args(argv)

    html = render_report(load_results(args.results), title=args.title, notes=args.note)
    args.output.write_text(html)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
