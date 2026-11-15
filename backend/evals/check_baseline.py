# ruff: noqa: T201  -- a command-line tool: printing is its output
"""Check a night's golden-eval results against the baseline, and write the Markdown summary.

    uv run python -m evals.check_baseline results.json --previous last-night.json \
        --markdown summary.md [--enforce] [--repo owner/name]

Exit status 1 only with ``--enforce`` and a regression; the nightly run is report only until
the owner turns enforcing on (spec P1-9, D14).
"""

import argparse
import os
import sys
from pathlib import Path

from evals.baseline import Baseline, check, markdown
from evals.make_baseline import BASELINE
from evals.results import load_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", type=Path, help="tonight's results file")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--previous", type=Path, help="the last nightly run's results")
    parser.add_argument("--markdown", type=Path, help="write the summary here")
    parser.add_argument("--enforce", action="store_true", help="exit 1 on a regression")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name, for commit links (default: GITHUB_REPOSITORY)",
    )
    args = parser.parse_args(argv)

    baseline = Baseline.model_validate_json(args.baseline.read_text())
    previous = load_results(args.previous) if args.previous else None
    result = check(baseline, load_results(args.results), previous)
    summary = markdown(result, args.enforce, args.repo)
    if args.markdown:
        args.markdown.write_text(summary)
    print(summary)
    return 1 if args.enforce and result.regressed else 0


if __name__ == "__main__":
    sys.exit(main())
