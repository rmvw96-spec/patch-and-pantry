"""The golden-eval runner (spec P1-9), with no live model: case files, outcomes, and scoring."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session

from evals.__main__ import main
from evals.capture import (
    Outcome,
    RetryOnRateLimit,
    capture_case,
    score_case,
    score_line,
)
from evals.cases import CASES_DIR, Case, Expect, load_cases

pytestmark = pytest.mark.usefixtures("catalog")


def case(*lines: dict[str, Any], group: str = "extraction") -> Case:
    return Case.model_validate(
        {"name": "t", "group": group, "why": "t", "source": "t", "lines": list(lines)}
    )


def never(_m: list[ModelMessage], _i: AgentInfo) -> ModelResponse:
    raise AssertionError("the model was called")


def test_the_committed_cases_load() -> None:
    cases = load_cases()
    assert {c.group for c in cases} == {"extraction", "clarification", "behavior"}
    assert sum(len(c.lines) for c in cases) > 150


@pytest.mark.parametrize(
    "expect",
    [
        {},  # nothing expected
        {"note": True, "item": "Onion"},
        {
            "ask": "missing_unit",
            "item": "Onion",
        },  # unit questions do not carry the item
        {"ask": "which_item", "price": 100},
        {"item": "Onion"},  # a price needs its price
        {"item": "Onion", "price": 100, "colour": "red"},
    ],
)
def test_an_expectation_must_be_one_outcome(expect: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Expect.model_validate(expect)


def test_a_case_rejects_repeated_lines_and_spaces_at_the_ends(tmp_path: Path) -> None:
    line = {"text": "Publix", "expect": {"note": True}}
    with pytest.raises(ValidationError, match="appears twice"):
        case(line, line)
    with pytest.raises(ValidationError, match="spaces at the ends"):
        case({"text": "Publix ", "expect": {"note": True}})
    (tmp_path / "a.yaml").write_text(
        "cases:\n- {name: x, group: behavior, why: w, source: s, lines: [{text: A, expect: {note: true}}]}\n"
        "- {name: x, group: behavior, why: w, source: s, lines: [{text: B, expect: {note: true}}]}\n"
    )
    with pytest.raises(ValueError, match="used twice: x"):
        load_cases(tmp_path)


def test_the_closest_acceptable_outcome_is_scored() -> None:
    wanted = (
        Expect(item="Lime", quantity=1, unit="each", price=100),
        Expect(ask="which_item", options=("Lime", "Lemon")),
    )
    asked = Outcome("which_item", options=("Lemon", "Lime", "Orange"))
    assert score_line("Limes, lemons $1 each", wanted, asked).wrong == ()
    too_few = Outcome("which_item", options=("Lemon", "Orange"))
    assert score_line("Limes, lemons $1 each", wanted, too_few).wrong == ("options",)

    priced = Outcome("price", item="Lime", quantity=1, unit="lb", price=100)
    score = score_line("Limes, lemons $1 each", wanted, priced)
    assert (score.right, score.scored, score.wrong) == (4, 5, ("unit",))


def test_a_wrong_outcome_gets_every_field_wrong() -> None:
    wanted = (Expect(item="Onion", quantity=3, unit="lb", price=299),)
    score = score_line("Onions 3 lb $2.99", wanted, Outcome("note"))
    assert score.right == 0
    assert score_line("Onions 3 lb $2.99", wanted, None).right == 0
    # The right items offered in the wrong kind of question are not partly right.
    which = (Expect(ask="which_item", options=("Kale",)),)
    no_match = Outcome("no_match", options=("Kale",))
    assert score_line("greens $2", which, no_match).wrong == ("outcome", "options")


def test_capture_outcomes_for_prices_questions_and_notes(db_session: Session) -> None:
    c = case(
        {"text": "Publix", "expect": {"note": True}},
        {
            "text": "onions 3 lb bag 2.99",
            "expect": {"item": "Onion", "quantity": 3, "unit": "lb", "price": 299},
        },
        {"text": "greens $2/lb", "expect": {"ask": "which_item", "options": ["Kale"]}},
        {"text": "Dragon fruit $4 each", "expect": {"ask": "no_match"}},
        # A count price whose piece weight is asked about (no eggplant weight in this
        # catalog) still scores as a price.
        {
            "text": "Eggplant $1 each",
            "expect": {"item": "Eggplant", "quantity": 1, "unit": "each", "price": 100},
        },
    )
    run = asyncio.run(capture_case(db_session, c, FunctionModel(never), "rules_first"))

    assert run.outcomes["Publix"] == Outcome("note")
    assert run.outcomes["onions 3 lb bag 2.99"] == Outcome(
        "price", item="Onion", quantity=3, unit="lb", price=299
    )
    assert run.outcomes["greens $2/lb"].kind == "which_item"
    assert run.outcomes["Eggplant $1 each"].kind == "price"
    assert all(not s.wrong for s in score_case(c, run)), score_case(c, run)
    assert run.model_lines == 0


def test_the_model_reads_every_line_in_model_mode(db_session: Session) -> None:
    def reads(_m: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        line = {
            "line": 1,
            "is_price": True,
            "item_text": "onions",
            "quantity": 3,
            "unit": "lb",
            "price_text": "2.99",
        }
        args = {"lines": [line]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    c = case({"text": "onions 3 lb bag 2.99", "expect": {"note": True}})
    run = asyncio.run(capture_case(db_session, c, FunctionModel(reads), "model"))
    assert run.model_lines == 1
    assert run.usage.requests == 1
    assert score_case(c, run)[0].wrong == ("outcome",)


def test_a_rate_limit_is_waited_out() -> None:
    calls = 0

    def busy_once(_m: list[ModelMessage], _i: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelRateLimitError("test", "slow down")
        return ModelResponse(parts=[TextPart("ok")])

    from pydantic_ai import Agent

    agent = Agent(RetryOnRateLimit(FunctionModel(busy_once), waits=(0,)))
    assert asyncio.run(agent.run("hi")).output == "ok"
    assert calls == 2

    def always_busy(_m: list[ModelMessage], _i: AgentInfo) -> ModelResponse:
        raise ModelRateLimitError("test", "slow down")

    agent = Agent(RetryOnRateLimit(FunctionModel(always_busy), waits=(0, 0)))
    with pytest.raises(ModelRateLimitError):
        asyncio.run(agent.run("hi"))


def test_the_command_line_scores_and_writes_results(tmp_path: Path) -> None:
    # Notes need no catalog, so this runs on CI's empty one. The demo reader is free.
    (tmp_path / "cases.yaml").write_text(
        """
cases:
  - name: notes
    group: behavior
    why: store names are notes
    source: test
    lines:
      - {text: "Publix", expect: {note: true}}
      - {text: "Kroger", expect: {note: true}}
  - name: wrong-on-purpose
    group: behavior
    why: a store name expected as a question, to fail
    source: test
    lines:
      - {text: "Aldi", expect: {ask: no_match}}
"""
    )
    out = tmp_path / "results.json"
    argv = [
        "--dir",
        str(tmp_path),
        "--model",
        "demo",
        "--repeat",
        "2",
        "--json",
        str(out),
    ]

    assert asyncio.run(main(argv)) == 1  # one case fails
    results = json.loads(out.read_text())
    assert results["repeat"] == 2
    assert results["cases"]["notes [rules_first]"]["pass_rate"] == 1.0
    assert results["cases"]["notes [model]"]["pass_rate"] == 1.0
    assert results["cases"]["wrong-on-purpose [model]"]["pass_rate"] == 0.0

    assert asyncio.run(main([*argv, "--case", "notes", "--reader", "model"])) == 0
    assert asyncio.run(main([*argv, "--case", "nothing"])) == 2


def test_cases_live_next_to_the_runner() -> None:
    assert CASES_DIR.name == "cases"
    assert CASES_DIR.parent.name == "evals"


def test_extraction_passes_at_95_percent_and_the_others_need_every_field() -> None:
    from evals.__main__ import passes
    from evals.capture import LineScore

    def scores(right: int, scored: int) -> list[LineScore]:
        return [LineScore("x", right, scored, None, (), Expect(note=True))]

    line = {"text": "Publix", "expect": {"note": True}}
    assert passes(case(line), scores(19, 20))
    assert not passes(case(line), scores(18, 20))
    assert not passes(case(line, group="behavior"), scores(19, 20))
    assert passes(case(line, group="clarification"), scores(20, 20))
