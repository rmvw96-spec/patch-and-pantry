# ruff: noqa: T201  -- a command-line tool: printing is its output
"""Build an HTML page of only what changed between two golden-eval runs.

    uv run python -m evals.generate_evals_report_diff before.json after.json -o diff.html

Shows cases whose pass count, share of fields, missed lines, or output tokens (by 20% or more)
changed, and each line that is newly wrong, now right, or wrong in a different way. Exit status
is 0 either way; it reports, it does not judge (the baseline check does that).
"""

import argparse
import sys
from pathlib import Path

from evals.report import render_diff
from evals.results import load_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before", type=Path, help="the earlier results file")
    parser.add_argument("after", type=Path, help="the later results file")
    parser.add_argument(
        "-o", "--output", type=Path, required=True, help="the HTML file to write"
    )
    parser.add_argument("--title", default="Typed list evals: what changed")
    args = parser.parse_args(argv)

    html = render_diff(
        load_results(args.before), load_results(args.after), title=args.title
    )
    args.output.write_text(html)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
