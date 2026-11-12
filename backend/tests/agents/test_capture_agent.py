"""Typed-list capture (Phase 1, P1-5) with scripted models: never a live model call.

A scripted model returns a fixed reading of the list, standing in for DeepSeek; the tests check
what the deterministic code does with it. The behavioral cases come from
docs/specs/known-answers.md.
"""

import asyncio
import uuid
from datetime import date
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session, func, select

import app.agents.capture_agent as capture
from app.agents.capture_agent import (
    CaptureFailed,
    CaptureResult,
    capture_typed_list,
    price_cents,
    total_cents,
)
from app.agents.demo_model import demo_capture_model, read_list_line
from app.agents.models import build_model
from app.core.config import settings
from app.models import (
    CatalogItem,
    Household,
    HouseholdAlias,
    PriceObservation,
    Vendor,
)
from tests.catalog_fixtures import make_household

Catalog = dict[str, CatalogItem]
pytestmark = pytest.mark.usefixtures("catalog")
DAY = date(2026, 11, 11)


def scripted(*entries: dict[str, Any]) -> FunctionModel:
    """A model that reads the list as given: one entry per line, numbered from 1 unless set."""
    lines = [{"line": n, "is_price": True} | e for n, e in enumerate(entries, start=1)]

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"lines": lines})]
        )

    return FunctionModel(respond)


def run(
    session: Session,
    household: Household,
    vendor: Vendor,
    text: str,
    model: FunctionModel | None,
) -> CaptureResult:
    return asyncio.run(
        capture_typed_list(session, household, text, vendor.id, DAY, model)
    )


@pytest.fixture
def capture_run(db_session: Session, household: Household, vendor: Vendor) -> Any:
    def go(text: str, *entries: dict[str, Any]) -> CaptureResult:
        return run(db_session, household, vendor, text, scripted(*entries))

    return go


# ---------- behavioral cases (known-answers.md) ----------


def test_onions_3_lb_bag(capture_run: Any, catalog: Catalog, vendor: Vendor) -> None:
    result = capture_run(
        "onions 3 lb bag 2.99",
        {"item_text": "onions", "quantity": 3, "unit": "lb", "price_text": "2.99"},
    )
    assert result.questions == []
    (draft,) = result.drafts
    assert draft.raw_text == "onions 3 lb bag 2.99"
    assert draft.catalog_item_id == catalog["onion"].id
    assert (draft.quantity, draft.unit, draft.price_cents) == (3, "lb", 299)
    assert (draft.vendor_id, draft.observed_on) == (vendor.id, DAY)
    assert draft.cost_per_cup_cents is not None
    assert draft.cost_per_cup_cents / 100 == pytest.approx(0.3906, abs=1e-4)
    assert draft.match_note is None


def test_kale_bunch_asks_for_the_weight(capture_run: Any, catalog: Catalog) -> None:
    result = capture_run(
        "kale bunch 2.50", {"item_text": "kale", "unit": "bunch", "price_text": "2.50"}
    )
    assert result.drafts == []
    (question,) = result.questions
    assert question.kind == "needs_weight"
    assert "Weigh it if you can" in question.message
    assert "estimate is fine" in question.message
    # What was read is kept, so the answer completes it instead of starting over.
    assert question.draft is not None
    assert question.draft.catalog_item_id == catalog["kale"].id
    assert (question.draft.unit, question.draft.price_cents) == ("bunch", 250)


def test_tomatoes_3_for_5_is_one_price_for_the_lot_and_asks_the_size(
    capture_run: Any,
) -> None:
    result = capture_run(
        "tomatoes 3 for $5",
        {"item_text": "tomatoes", "quantity": 3, "unit": "each", "price_text": "$5"},
    )
    (question,) = result.questions
    assert question.kind == "needs_size"
    assert question.message == "Which size tomato: small, medium, or large?"
    assert question.options == ["small", "medium", "large"]
    assert question.draft is not None
    assert (question.draft.quantity, question.draft.price_cents) == (3, 500)


def test_a_size_on_the_line_needs_no_question(capture_run: Any) -> None:
    result = capture_run(
        "large tomatoes 3 for $5",
        {"item_text": "tomatoes", "quantity": 3, "unit": "each", "size": "large", "price_text": "$5"},
    )  # fmt: skip
    (draft,) = result.drafts
    assert draft.piece_grams == 200.0
    assert draft.grams_purchased == pytest.approx(600.0)
    assert draft.piece_source is not None and "tomato large each" in draft.piece_source


def test_misspelled_onions_match_and_say_so(capture_run: Any, catalog: Catalog) -> None:
    result = capture_run(
        "yellow onoins 3 lb 2.99",
        {
            "item_text": "yellow onoins",
            "quantity": 3,
            "unit": "lb",
            "price_text": "2.99",
        },
    )
    (draft,) = result.drafts
    assert draft.catalog_item_id == catalog["onion"].id
    assert draft.match_note == "matched 'yellow onoins' to Onion"


def test_greens_asks_which(capture_run: Any) -> None:
    result = capture_run("greens 2.48", {"item_text": "greens", "price_text": "2.48"})
    (question,) = result.questions
    assert question.kind == "which_item"
    assert question.options == [
        "Collards", "Kale", "Mustard greens", "Swiss chard", "Turnip greens"
    ]  # fmt: skip


# ---------- raw text and nothing saved ----------


def test_raw_text_is_kept_exactly_and_nothing_is_saved(
    db_session: Session, household: Household, vendor: Vendor
) -> None:
    text = "  Onions 3 LB bag   2.99 \nkale bunch 2.50\nyellow onoins 3 lb 2.99"
    before = {
        model: db_session.exec(select(func.count()).select_from(model)).one()
        for model in (PriceObservation, HouseholdAlias)
    }
    model = scripted(
        # The model's item words differ from the line; the draft keeps the line itself.
        {"item_text": "ONIONS", "quantity": 3, "unit": "LB", "price_text": "2.99"},
        {"item_text": "kale", "unit": "bunch", "price_text": "2.50"},
        {
            "item_text": "yellow onoins",
            "quantity": 3,
            "unit": "lb",
            "price_text": "2.99",
        },
    )

    result = run(db_session, household, vendor, text, model)

    assert [d.raw_text for d in result.drafts] == [
        "  Onions 3 LB bag   2.99 ",
        "yellow onoins 3 lb 2.99",
    ]
    assert result.questions[0].raw_text == "kale bunch 2.50"
    assert not db_session.new and not db_session.dirty
    for model_type, count in before.items():
        assert (
            db_session.exec(select(func.count()).select_from(model_type)).one() == count
        )


# ---------- the model copies; the code converts ----------


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("2.99", 299), ("$2.99", 299), ("$5", 500), ("5", 500), (".99", 99), ("0.5", 50),
        ("99c", 99), ("99¢", 99), ("99 cents", 99), ("1,299.00", 129900), (" $ 3.49 ", 349),
        (None, None), ("", None), ("two", None), ("2.999", None), ("-1", None), ("$", None),
    ],
)  # fmt: skip
def test_price_text_to_cents(text: str | None, cents: int | None) -> None:
    assert price_cents(text) == cents


def test_a_per_unit_price_is_multiplied_out_to_the_total(capture_run: Any) -> None:
    """Prices are stored as the total paid for the amount bought (dogfooding: "$0.29 each (3)")."""
    result = capture_run(
        "tomatoes 3 lb at 2.49/lb",
        {"item_text": "tomatoes", "quantity": 3, "unit": "lb", "price_text": "2.49", "price_is_per_unit": True},
    )  # fmt: skip
    (draft,) = result.drafts
    assert (draft.quantity, draft.unit, draft.price_cents) == (3, "lb", 747)
    assert draft.price_note == "$2.49 a lb x 3"


def test_a_count_after_an_each_price_is_the_amount_bought(capture_run: Any) -> None:
    result = capture_run(
        "large tomatoes $0.29 each (3)",
        {"item_text": "tomatoes", "quantity": 3, "unit": "each", "size": "large", "price_text": "$0.29", "price_is_per_unit": True},
    )  # fmt: skip
    (draft,) = result.drafts
    assert (draft.quantity, draft.unit, draft.price_cents) == (3, "each", 87)
    assert draft.price_note == "$0.29 each x 3"


def test_a_per_unit_price_with_no_amount_is_one_unit(capture_run: Any) -> None:
    result = capture_run(
        "tomatoes 2.49/lb",
        {"item_text": "tomatoes", "unit": "lb", "price_text": "2.49", "price_is_per_unit": True},
    )  # fmt: skip
    (draft,) = result.drafts
    assert (draft.quantity, draft.price_cents, draft.price_note) == (1, 249, None)


@pytest.mark.parametrize(
    ("per_unit", "quantity", "total"),
    [(29, 3, 87), (249, 1.5, 374), (249, 1.37, 341), (199, 0.25, 50), (1, 0.5, 1)],
)  # fmt: skip
def test_total_cents_rounds_half_up(per_unit: int, quantity: float, total: int) -> None:
    assert total_cents(per_unit, quantity) == total


def test_a_unit_without_a_number_is_one_unit(capture_run: Any) -> None:
    result = capture_run(
        "romaine head 2.49",
        {"item_text": "romaine", "unit": "head", "price_text": "2.49"},
    )
    (draft,) = result.drafts
    assert (draft.quantity, draft.unit) == (1, "head")
    assert draft.piece_grams == 666.0
    assert draft.cups is not None


# ---------- every unclear line is a question ----------


def test_a_line_the_model_skipped_is_unreadable(
    db_session: Session, household: Household, vendor: Vendor
) -> None:
    def respond(_m: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        lines = [
            {"line": 1, "is_price": True, "item_text": "onions", "unit": "lb", "price_text": "1"},
            {"line": 1, "is_price": True, "item_text": "kale", "unit": "lb", "price_text": "9"},
            {"line": 7, "is_price": True, "item_text": "kale", "unit": "lb", "price_text": "9"},
        ]  # fmt: skip
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"lines": lines})]
        )

    result = run(
        db_session, household, vendor, "onions lb 1\n???", FunctionModel(respond)
    )

    # The duplicate for line 1 and the out-of-range line 7 are ignored, not trusted.
    (draft,) = result.drafts
    assert draft.price_cents == 100
    (question,) = result.questions
    assert (question.raw_text, question.kind) == ("???", "unreadable")


def test_lines_that_are_not_prices_are_set_aside(capture_run: Any) -> None:
    result = capture_run(
        "Kroger 11/10\nonions 3 lb 2.99",
        {"is_price": False},
        {"item_text": "onions", "quantity": 3, "unit": "lb", "price_text": "2.99"},
    )
    assert result.ignored == ["Kroger 11/10"]
    assert len(result.drafts) == 1


@pytest.mark.parametrize(
    ("entry", "kind", "words"),
    [
        ({"item_text": "kumquats", "unit": "lb", "price_text": "4.99"}, "no_match", "not in the catalog"),
        ({"item_text": None, "unit": "lb", "price_text": "4.99"}, "no_match", "Which item"),
        ({"item_text": "onions", "unit": "lb", "price_text": None}, "missing_price", "price of onion"),
        ({"item_text": "onions", "unit": "lb", "price_text": "two"}, "missing_price", "('two')"),
        ({"item_text": "okra", "price_text": "99c"}, "missing_unit", "What did $0.99 buy"),
        ({"item_text": "onions", "unit": "bag", "price_text": "2.99"}, "unknown_unit", "Unknown unit 'bag'"),
        ({"item_text": "onions", "quantity": 0, "unit": "lb", "price_text": "2.99"}, "unreadable", "more than zero"),
    ],
)  # fmt: skip
def test_unclear_lines_become_questions(
    db_session: Session,
    household: Household,
    vendor: Vendor,
    entry: dict[str, Any],
    kind: str,
    words: str,
) -> None:
    db_session.add(
        CatalogItem(
            slug="okra",
            name="Okra",
            kind="produce",
            category="vegetable",
            preparation_yield_factor=0.7695,
            cup_equivalent_lb=0.3527,
            conversion_source="test",
        )
    )
    db_session.flush()
    result = run(db_session, household, vendor, "the line", scripted(entry))
    assert result.drafts == []
    (question,) = result.questions
    assert question.kind == kind
    assert words in question.message


def test_no_cup_conversion_is_a_draft_without_a_per_cup_figure(
    capture_run: Any,
) -> None:
    result = capture_run(
        "eggplant 1.99/lb",
        {"item_text": "eggplant", "unit": "lb", "price_text": "1.99", "price_is_per_unit": True},
    )  # fmt: skip
    (draft,) = result.drafts
    assert draft.cost_per_cup_cents is None and draft.cups is None
    assert draft.note is not None and "no cup conversion" in draft.note


def test_stand_ins_are_named_on_the_draft(capture_run: Any) -> None:
    result = capture_run(
        "yellow squash 1.99/lb",
        {"item_text": "yellow squash", "unit": "lb", "price_text": "1.99", "price_is_per_unit": True},
    )  # fmt: skip
    (draft,) = result.drafts
    assert draft.stand_in_name == "Zucchini"
    assert draft.cost_per_cup_cents is not None


def test_household_aliases_are_used(
    db_session: Session, household: Household, vendor: Vendor, catalog: Catalog
) -> None:
    from app.tools.catalog import remember_alias

    remember_alias(db_session, household, "greens", catalog["collards"])
    result = run(
        db_session,
        household,
        vendor,
        "greens 1 lb 2.48",
        scripted(
            {"item_text": "greens", "quantity": 1, "unit": "lb", "price_text": "2.48"}
        ),
    )
    (draft,) = result.drafts
    assert draft.catalog_item_id == catalog["collards"].id
    assert draft.match_note == "'greens' is Collards, as you confirmed before"


def test_the_snapshots_read_are_recorded(capture_run: Any) -> None:
    result = capture_run(
        "romaine head 2.49",
        {"item_text": "romaine", "unit": "head", "price_text": "2.49"},
    )
    assert len(result.data_snapshot_ids) == 1  # the piece weight's snapshot


# ---------- limits and failures ----------


def test_only_the_households_own_vendors(
    db_session: Session, household: Household
) -> None:
    other = make_household(db_session)
    theirs = Vendor(household_id=other.id, name="Theirs", vendor_type="store")
    db_session.add(theirs)
    db_session.flush()
    with pytest.raises(CaptureFailed) as raised:
        run(db_session, household, theirs, "onions 1 lb 1.00", scripted())
    assert raised.value.reason == "unknown_vendor"
    with pytest.raises(CaptureFailed):
        asyncio.run(
            capture_typed_list(
                db_session, household, "x", uuid.uuid4(), DAY, scripted()
            )
        )


@pytest.mark.parametrize(
    ("text", "reason"),
    [("x 1\n" * 101, "too_many_lines"), ("x" * 201, "line_too_long")],
)
def test_list_limits(
    db_session: Session, household: Household, vendor: Vendor, text: str, reason: str
) -> None:
    with pytest.raises(CaptureFailed) as raised:
        run(db_session, household, vendor, text, scripted())
    assert raised.value.reason == reason


def test_an_empty_list_needs_no_model(
    db_session: Session, household: Household, vendor: Vendor
) -> None:
    def never(_m: list[ModelMessage], _i: AgentInfo) -> ModelResponse:
        raise AssertionError("the model was called")

    result = run(db_session, household, vendor, "\n  \n", FunctionModel(never))
    assert result == CaptureResult()


def test_unusable_model_output_is_a_clear_failure(
    db_session: Session, household: Household, vendor: Vendor
) -> None:
    def prose(_m: list[ModelMessage], _i: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Here are your prices!")])

    with pytest.raises(CaptureFailed) as raised:
        run(db_session, household, vendor, "onions 1 lb 1.00", FunctionModel(prose))
    assert raised.value.reason == "invalid_output"


def test_a_runaway_model_is_stopped(
    db_session: Session,
    household: Household,
    vendor: Vendor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture, "MAX_MODEL_REQUESTS", 1)

    def prose(_m: list[ModelMessage], _i: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Let me think about that.")])

    with pytest.raises(CaptureFailed) as raised:
        run(db_session, household, vendor, "onions 1 lb 1.00", FunctionModel(prose))
    assert raised.value.reason == "too_many_steps"


# ---------- demo mode ----------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("onions 3 lb bag 2.99", {"item_text": "onions", "quantity": 3.0, "unit": "lb", "price_text": "2.99"}),
        ("kale bunch 2.50", {"item_text": "kale", "unit": "bunch", "price_text": "2.50"}),
        ("tomatoes 3 for $5", {"item_text": "tomatoes", "quantity": 3.0, "unit": "each", "price_text": "$5"}),
        ("tomatoes 2.49/lb", {"item_text": "tomatoes", "unit": "lb", "price_text": "2.49", "price_is_per_unit": True}),
        ("large green peppers 2 for 2.00", {"item_text": "green peppers", "size": "large", "quantity": 2.0}),
        ("fresh banana $0.13 each (5)", {"item_text": "fresh banana", "quantity": 5.0, "unit": "each", "price_text": "$0.13", "price_is_per_unit": True}),
        ("hass avocado $.73 ea - 2", {"item_text": "hass avocado", "quantity": 2.0, "price_is_per_unit": True}),
        ("limes 1.29 ea x3", {"item_text": "limes", "quantity": 3.0, "price_is_per_unit": True}),
        ("okra 99c", {"item_text": "okra", "price_text": "99c"}),
        ("Kroger", {"is_price": False}),
    ],
)  # fmt: skip
def test_demo_reader(line: str, expected: dict[str, Any]) -> None:
    assert read_list_line(line).items() >= expected.items()


def test_demo_mode_runs_the_whole_capture(
    db_session: Session,
    household: Household,
    vendor: Vendor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIM_MODEL=demo needs no model key: the demo reader stands in for the model."""
    monkeypatch.setattr(settings, "SIM_MODEL", "demo")
    text = (
        "Kroger\nonions 3 lb bag 2.99\nkale bunch 2.50\ngreens 2.48\nromaine head 2.49"
    )

    result = run(db_session, household, vendor, text, None)

    assert [d.item_name for d in result.drafts] == ["Onion", "Lettuce"]
    assert [q.kind for q in result.questions] == ["needs_weight", "which_item"]
    assert result.ignored == ["Kroger"]


def test_demo_capture_model_is_built_for_the_capture_role() -> None:
    assert isinstance(build_model("demo", role="capture"), FunctionModel)
    assert isinstance(demo_capture_model(), FunctionModel)


def test_demo_answers_can_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEMO_DELAY_SECONDS makes E2E runs take long enough that the page must poll."""
    import app.agents.demo_model as demo

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(demo.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(settings, "DEMO_DELAY_SECONDS", 1.5)
    lines, _usage = asyncio.run(
        capture.extract_lines(["onions 1 lb 1.00"], demo_capture_model())
    )
    assert waits == [1.5]
    assert lines[1].item_text == "onions"
    monkeypatch.setattr(settings, "DEMO_DELAY_SECONDS", 0.0)
    asyncio.run(capture.extract_lines(["onions 1 lb 1.00"], demo_capture_model()))
    assert waits == [1.5]
