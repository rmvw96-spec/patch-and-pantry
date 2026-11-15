"""The nightly baseline (spec P1-9, D14 and D15): tiers, the regression check, and the summary."""

import json
from pathlib import Path
from typing import Any

import pytest

from evals.baseline import Baseline, check, make_baseline, markdown
from evals.cases import Case, load_cases
from evals.check_baseline import main as check_main
from evals.make_baseline import BASELINE
from evals.make_baseline import main as make_main
from evals.results import CaseResult, Miss, Results

LINES = ["Onions $1/lb", "Garlic 3 ct $1.49", "Mangoes 2 @ $1.25", "Green onions $1"]


def case(name: str = "c", group: str = "extraction") -> Case:
    lines = [{"text": t, "expect": {"note": True}} for t in LINES]
    return Case.model_validate(
        {"name": name, "group": group, "why": "w", "source": "s", "lines": lines}
    )


def miss(text: str, wrong: int) -> Miss:
    return Miss(text=text, wrong=wrong, expected="note", got=("asks no_match",))


def result(
    reader: str = "model", passed: int = 3, misses: tuple[Miss, ...] = (), **more: Any
) -> CaseResult:
    fields: dict[str, Any] = {
        "case": "c",
        "reader": reader,
        "group": "extraction",
        "runs": 3,
        "passed": passed,
        "pass_rate": passed / 3,
        "fields": 1.0,
        "seconds_per_run": 1.0,
        "output_tokens_per_run": 100,
        "misses": misses,
    }
    return CaseResult.model_validate(fields | more)


def results(*rs: CaseResult, commit: str = "b" * 40) -> Results:
    return Results(
        model="m",
        repeat=3,
        commit=commit,
        cases={f"{r.case} [{r.reader}]": r for r in rs},
    )


def baseline(*rs: CaseResult) -> Baseline:
    return make_baseline(results(*rs, commit="a" * 40), [case()])


def kinds(found: Any) -> list[tuple[str, str]]:
    return [(f.kind, f.text or f.what) for f in found.findings]


def test_cases_and_lines_are_sorted_into_three_tiers() -> None:
    b = baseline(
        result(
            "model", 1, (miss("Garlic 3 ct $1.49", 3), miss("Mangoes 2 @ $1.25", 2))
        ),
        result("rules_first", 3),
    )
    model = b.cases["c [model]"]
    assert model.tier == "flaky"
    assert model.lines.stable == ("Onions $1/lb", "Green onions $1")
    assert model.lines.flaky == {"Mangoes 2 @ $1.25": 2}
    assert model.lines.broken == ("Garlic 3 ct $1.49",)
    assert b.cases["c [rules_first]"].tier == "stable"
    assert (
        make_baseline(results(result(passed=0)), [case()]).cases["c [model]"].tier
        == "broken"
    )
    with pytest.raises(ValueError, match="not in the case files"):
        make_baseline(results(result()), [case("other")])


def test_a_stable_case_regresses_on_two_runs_or_two_nights() -> None:
    b = baseline(result("model"), result("rules_first"))
    one_dip = results(result("model", 2))
    assert kinds(check(b, one_dip)) == [
        ("watch", "stable, passed 2 of 3 runs: one run dipped")
    ]
    assert check(b, results(result("model", 1))).regressed
    # One run fewer, two nights in a row.
    assert check(b, one_dip, previous=one_dip).regressed
    assert not check(b, one_dip, previous=results(result("model"))).regressed


def test_rules_first_is_held_to_the_stricter_bar() -> None:
    b = baseline(result("model"), result("rules_first"))
    found = check(b, results(result("rules_first", 2, (miss("Onions $1/lb", 1),))))
    assert kinds(found) == [
        ("regression", "stable, passed 2 of 3 runs (rules first)"),
        ("regression", "Onions $1/lb"),
    ]


def test_stable_lines_regress_and_flaky_and_broken_lines_are_tracked() -> None:
    b = baseline(
        result("model", 3, (miss("Garlic 3 ct $1.49", 3), miss("Mangoes 2 @ $1.25", 1)))
    )
    once = check(
        b,
        results(
            result("model", 3, (miss("Onions $1/lb", 1), miss("Garlic 3 ct $1.49", 3)))
        ),
    )
    assert ("watch", "Onions $1/lb") in kinds(once)
    assert ("better", "flaky line right every run: Mangoes 2 @ $1.25") in kinds(once)
    assert not once.regressed

    twice = check(b, results(result("model", 3, (miss("Onions $1/lb", 2),))))
    assert ("regression", "Onions $1/lb") in kinds(twice)
    assert ("better", "broken line now right: Garlic 3 ct $1.49") in kinds(twice)

    worse = check(
        b,
        results(
            result(
                "model", 3, (miss("Mangoes 2 @ $1.25", 3), miss("Garlic 3 ct $1.49", 3))
            )
        ),
    )
    assert kinds(worse) == [("watch", "Mangoes 2 @ $1.25")]


def test_extraction_fields_may_drop_three_points() -> None:
    b = baseline(result("model", fields=0.95))
    assert not check(b, results(result("model", fields=0.925))).findings
    assert kinds(check(b, results(result("model", fields=0.91)))) == [
        ("regression", "fields 91%, down from 95% in the baseline")
    ]
    behavior = make_baseline(
        results(result(group="behavior", fields=1.0)), [case(group="behavior")]
    )
    assert not check(behavior, results(result(group="behavior", fields=0.5))).findings


def test_failed_runs_new_cases_and_better_cases() -> None:
    b = baseline(result("model", 0), result("rules_first", 1))
    found = check(
        b,
        results(
            result("model", 1, errors=("rate limited",)),
            result("rules_first", 3),
            result("model", case="added"),
            result("rules_first", 3, (miss("Brand new line", 1),)),
        ),
    )
    assert ("regression", "a run failed: rate limited") in kinds(found)
    assert ("better", "broken, passed 1 of 3 runs") in kinds(found)
    assert ("better", "flaky, passed every run (3 of 3 runs)") in kinds(found)
    assert ("new", "not in the baseline") in kinds(found)
    assert ("new", "Brand new line") in kinds(found)


def test_the_summary_says_whether_it_fails_and_links_the_commits() -> None:
    b = baseline(result("model"))
    bad = results(result("model", 0, (miss("Onions $1/lb", 3),)))
    report_only = markdown(
        check(b, bad, previous=results(result(), commit="c" * 40)), False, "o/r"
    )
    assert "report only for now" in report_only
    assert f"https://github.com/o/r/compare/{'c' * 40}...{'b' * 40}" in report_only
    same = markdown(check(b, bad, previous=bad), False, "o/r")
    assert "no commits since the last nightly run" in same
    assert "| stable | 1 | 0 |" in report_only
    assert (
        "`Onions $1/lb`: expected note, got asks no\\_match (3 of 3 runs)"
        in report_only
    )
    assert "the workflow fails" in markdown(check(b, bad), True)
    assert "No regressions" in markdown(check(b, results(result())), True)


def test_the_check_fails_only_when_enforced(tmp_path: Path) -> None:
    (tmp_path / "base.json").write_text(baseline(result("model")).model_dump_json())
    (tmp_path / "night.json").write_text(results(result("model", 0)).model_dump_json())
    args = [str(tmp_path / "night.json"), "--baseline", str(tmp_path / "base.json")]
    args += ["--markdown", str(tmp_path / "s.md"), "--repo", "o/r"]
    assert check_main(args) == 0
    assert "Regressions (1)" in (tmp_path / "s.md").read_text()
    assert check_main([*args, "--enforce"]) == 1
    assert (
        check_main([*args, "--previous", str(tmp_path / "night.json"), "--enforce"])
        == 1
    )


def test_make_baseline_writes_the_file(tmp_path: Path) -> None:
    (tmp_path / "cases.yaml").write_text(
        "cases:\n- {name: c, group: behavior, why: w, source: s,"
        " lines: [{text: Publix, expect: {note: true}}]}\n"
    )
    (tmp_path / "r.json").write_text(
        results(result(group="behavior")).model_dump_json()
    )
    out = tmp_path / "b.json"
    assert (
        make_main([str(tmp_path / "r.json"), "-o", str(out), "--dir", str(tmp_path)])
        == 0
    )
    assert json.loads(out.read_text())["cases"]["c [model]"]["tier"] == "stable"


def test_the_committed_baseline_covers_every_case_line_once() -> None:
    committed = Baseline.model_validate_json(BASELINE.read_text())
    cases = {c.name: c for c in load_cases()}
    assert {k.rsplit(" [", 1)[0] for k in committed.cases} == set(cases)
    for key, entry in committed.cases.items():
        lines = entry.lines
        tiered = [*lines.stable, *lines.flaky, *lines.broken]
        assert sorted(tiered) == sorted(
            line.text for line in cases[key.rsplit(" [", 1)[0]].lines
        ), key


def test_a_stable_line_wrong_once_two_nights_running_regresses() -> None:
    b = baseline(result("model"))
    night = results(result("model", 3, (miss("Onions $1/lb", 1),)))
    assert not check(b, night).regressed
    assert ("regression", "Onions $1/lb") in kinds(check(b, night, previous=night))
