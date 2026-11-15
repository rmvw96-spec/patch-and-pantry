"""The golden-eval report tools (spec P1-9): one run's page, and the page of what changed."""

from pathlib import Path

from evals.generate_evals_report import main as report_main
from evals.generate_evals_report_diff import main as diff_main
from evals.report import render_diff, render_report
from evals.results import CaseResult, Miss, Results, load_results

FIRST_RUN = Path(__file__).parents[2] / "evals" / "reports" / "first_golden_run.json"


def result(
    case: str = "aldi-list", reader: str = "model", **change: object
) -> CaseResult:
    base: dict[str, object] = {
        "case": case,
        "reader": reader,
        "group": "extraction",
        "why": "a store list",
        "lines": 3,
        "runs": 3,
        "passed": 3,
        "pass_rate": 1.0,
        "fields": 1.0,
        "seconds_per_run": 2.0,
        "output_tokens_per_run": 300,
    }
    return CaseResult.model_validate(base | change)


def results(*cases: CaseResult, model: str = "foundry:deepseek-v3.2") -> Results:
    return Results(
        model=model, repeat=3, cases={f"{c.case} [{c.reader}]": c for c in cases}
    )


GARLIC = Miss(
    text="Garlic 3 ct $1.49",
    wrong=3,
    expected="Garlic: 3 each = $1.49",
    got=("asks unknown_unit",),
)


def test_the_report_shows_scores_and_every_miss_escaped() -> None:
    evil = Miss(
        text="<script>x</script> $1", wrong=1, expected="note", got=("asks no_match",)
    )
    run = results(
        result(passed=0, pass_rate=0.0, fields=0.8, misses=(GARLIC, evil)),
        result(reader="rules_first"),
    )
    html = render_report(run, notes=["checked by hand"])
    assert html.startswith("<!doctype html>")
    assert "Garlic 3 ct $1.49" in html and "3 of 3 runs wrong" in html
    assert "<script>x" not in html and "&lt;script&gt;x" in html
    assert "1 of 2" in html  # pairs passing every run
    assert "checked by hand" in html


def test_the_first_run_is_committed_and_renders() -> None:
    first = load_results(FIRST_RUN)
    assert first.model == "foundry:deepseek-v3.2"
    assert sum(r.passed == r.runs for r in first.cases.values()) == 28
    assert "28 of 40" in render_report(first)


def test_the_diff_shows_only_what_changed() -> None:
    peppers = Miss(
        text="Green peppers 3 ct $2.69",
        wrong=3,
        expected="Sweet pepper: 3 each = $2.69",
        got=("asks unknown_unit",),
    )
    before = results(
        result(passed=0, pass_rate=0.0, fields=0.8, misses=(GARLIC, peppers)),
        result(case="publix-list"),
        result(case="steady", misses=(GARLIC,), fields=0.97),
    )
    differently = peppers.model_copy(
        update={"wrong": 1, "got": ("Sweet pepper: 1 each = $2.69",)}
    )
    newly = Miss(
        text="Bananas 0.25/lb",
        wrong=2,
        expected="Banana: 1 lb = $0.25",
        got=("asks no_match",),
    )
    after = results(
        result(passed=2, pass_rate=2 / 3, fields=0.95, misses=(differently,)),
        result(
            case="publix-list", passed=1, pass_rate=1 / 3, fields=0.9, misses=(newly,)
        ),
        result(case="steady", misses=(GARLIC,), fields=0.97, output_tokens_per_run=310),
    )
    html = render_diff(before, after)

    assert "now right (was 3 of 3 runs wrong)" in html  # garlic
    assert "was 3 of 3 runs wrong" in html and "Sweet pepper: 1 each" in html
    assert "newly wrong" in html and "Bananas 0.25/lb" in html
    assert "0/3</span> → <span" in html
    assert "steady" not in html  # same misses, tokens within 20%


def test_a_diff_of_the_same_results_says_nothing_changed() -> None:
    first = load_results(FIRST_RUN)
    html = render_diff(first, first)
    assert "Nothing changed" in html
    assert "Cases whose result changed" not in html


def test_the_diff_notes_a_new_model_new_cases_and_token_jumps() -> None:
    before = results(result(), result(case="gone"))
    after = results(
        result(output_tokens_per_run=900), result(case="new"), model="other"
    )
    html = render_diff(before, after)
    assert "foundry:deepseek-v3.2 → other" in html
    assert "new [model]" in html and "gone [model]" in html
    assert "300 → 900" in html


def test_both_tools_write_their_pages(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    assert report_main([str(FIRST_RUN), "-o", str(out), "--title", "First run"]) == 0
    assert "<title>First run</title>" in out.read_text()
    diff = tmp_path / "diff.html"
    assert diff_main([str(FIRST_RUN), str(FIRST_RUN), "-o", str(diff)]) == 0
    assert "Nothing changed" in diff.read_text()
