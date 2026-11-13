"""Answering a question on a read price list (dogfooding): the line is completed in code.

An answer (an item, a unit, a size, or one piece's weight) turns the question into a draft, or
the next question, without calling the model again, and is kept on the run. The items here are
owned by the test user's household, so the tests do not depend on the shared catalog.
"""

import random
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session, select

from app.api.deps import get_run_model
from app.core.config import settings
from app.main import app
from app.models import CatalogItem, GroceryAnalysisRun, Household, HouseholdAlias

RUNS_URL = f"{settings.API_V1_STR}/grocery-analysis-runs/"
VENDORS_URL = f"{settings.API_V1_STR}/vendors/"
HOUSEHOLD_URL = f"{settings.API_V1_STR}/household/"


def made_up_word() -> str:
    """A name no catalog has. New for each test: answers save aliases, and API tests commit."""
    return "qq" + "".join(random.choice("bcdfghjklmnpvwxz") for _ in range(6))


def reads(*entries: dict[str, Any], calls: list[int] | None = None) -> FunctionModel:
    """A model that reads the list as given: one entry per line, numbered from 1."""
    lines = [{"line": n, "is_price": True} | e for n, e in enumerate(entries, start=1)]

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if calls is not None:
            calls.append(1)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"lines": lines})]
        )

    return FunctionModel(respond)


@pytest.fixture
def use_model() -> Iterator[Callable[[Any], None]]:
    def _use(model: Any) -> None:
        app.dependency_overrides[get_run_model] = lambda: model

    yield _use
    app.dependency_overrides.pop(get_run_model, None)


@pytest.fixture
def headers(normal_user_token_headers: dict[str, str]) -> dict[str, str]:
    return normal_user_token_headers


@pytest.fixture
def household(db: Session, client: TestClient, headers: dict[str, str]) -> Household:
    household_id = client.get(HOUSEHOLD_URL, headers=headers).json()["id"]
    found = db.get(Household, uuid.UUID(household_id))
    assert found is not None
    return found


def _own_item(db: Session, household: Household, name: str) -> CatalogItem:
    item = CatalogItem(
        slug=f"test-{name.lower()}-{uuid.uuid4().hex[:8]}",
        name=name,
        kind="produce",
        category="vegetable",
        scope="household",
        household_id=household.id,
        preparation_yield_factor=0.9,
        cup_equivalent_lb=0.3527,
        conversion_source="test",
    )
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def onion(db: Session, household: Household) -> CatalogItem:
    return _own_item(db, household, "Testonion")


def read_list(
    client: TestClient,
    headers: dict[str, str],
    use_model: Callable[[Any], None],
    text: str,
    *entries: dict[str, Any],
) -> dict[str, Any]:
    vendor = client.post(VENDORS_URL, headers=headers, json={"name": "Market"})
    assert vendor.status_code == 201, vendor.text
    use_model(reads(*entries))
    body = {
        "kind": "price_capture",
        "text": text,
        "vendor_id": vendor.json()["id"],
        "observed_on": "2026-11-12",
    }
    r = client.post(RUNS_URL, headers=headers, json=body)
    assert r.status_code == 202, r.text
    run = client.get(f"{RUNS_URL}{r.json()['id']}", headers=headers).json()
    assert run["status"] == "succeeded", run
    return dict(run)


def answer(
    client: TestClient, headers: dict[str, str], run: dict[str, Any], **fields: Any
) -> Any:
    return client.post(f"{RUNS_URL}{run['id']}/answers", headers=headers, json=fields)


def test_choosing_an_item_turns_the_question_into_a_draft(
    client: TestClient,
    headers: dict[str, str],
    use_model: Callable[[Any], None],
    db: Session,
    household: Household,
    onion: CatalogItem,
) -> None:
    unknown = made_up_word()
    line = f"{unknown} 3 lb 2.99"
    run = read_list(
        client, headers, use_model, line,
        {"item_text": unknown, "quantity": 3, "unit": "lb", "price_text": "2.99"},
    )  # fmt: skip
    (question,) = run["result"]["questions"]
    assert question["kind"] == "no_match"
    assert question["reading"]["item_text"] == unknown  # kept for the answer

    calls: list[int] = []
    use_model(reads(calls=calls))
    r = answer(client, headers, run, raw_text=line, catalog_item_id=str(onion.id))

    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["questions"] == []
    (draft,) = result["drafts"]
    assert (draft["item_name"], draft["match_note"]) == (
        "Testonion",
        "you chose Testonion",
    )
    assert draft["cost_per_cup_cents"] is not None  # computed in code, like any draft
    assert calls == []  # no model call
    # Kept on the run, so a refresh shows the draft.
    again = client.get(f"{RUNS_URL}{run['id']}", headers=headers).json()
    assert again["result"] == result
    # Remembered: the same words match this item next time.
    alias = db.exec(
        select(HouseholdAlias).where(HouseholdAlias.household_id == household.id)
    ).one()
    assert (alias.text, alias.catalog_item_id) == (unknown, onion.id)


def test_answers_chain_item_then_unit_then_weight(
    client: TestClient,
    headers: dict[str, str],
    use_model: Callable[[Any], None],
    onion: CatalogItem,
) -> None:
    """Each answer keeps the earlier ones; the next question comes from the same code."""
    unknown = made_up_word()
    line = f"{unknown} 2.50"
    run = read_list(
        client, headers, use_model, line,
        {"item_text": unknown, "price_text": "2.50"},
    )  # fmt: skip
    r = answer(client, headers, run, raw_text=line, catalog_item_id=str(onion.id))
    (question,) = r.json()["result"]["questions"]
    assert question["kind"] == "missing_unit"
    assert question["options"] == ["lb", "each", "bunch", "head"]

    r = answer(client, headers, r.json(), raw_text=line, unit="bunch")
    (question,) = r.json()["result"]["questions"]
    assert question["kind"] == "needs_weight"  # no bunch weight for this item
    assert question["draft"]["catalog_item_id"] == str(onion.id)

    weight = {"amount": 0.5, "unit": "lb", "source": "estimated"}
    r = answer(client, headers, r.json(), raw_text=line, piece_weight=weight)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["questions"] == []
    (draft,) = result["drafts"]
    assert (draft["unit"], draft["piece_weight"]) == ("bunch", weight)
    assert draft["piece_grams"] == pytest.approx(226.796, abs=1e-3)
    assert draft["piece_source"] == "estimated at capture: 226.796 g"
    assert (
        draft["match_note"] == "you chose Testonion"
    )  # the first answer's note is kept


def test_answers_are_refused_when_they_cannot_apply(
    client: TestClient,
    headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    db: Session,
    onion: CatalogItem,
) -> None:
    unknown = made_up_word()
    line = f"{unknown} 3 lb 2.99"
    run = read_list(
        client, headers, use_model, line,
        {"item_text": unknown, "quantity": 3, "unit": "lb", "price_text": "2.99"},
    )  # fmt: skip
    item = str(onion.id)

    two = answer(client, headers, run, raw_text=line, catalog_item_id=item, unit="lb")
    assert two.status_code == 422
    assert answer(client, headers, run, raw_text=line).status_code == 422  # none
    missing = answer(client, headers, run, raw_text="no such line", unit="lb")
    assert missing.status_code == 404
    no_item = answer(
        client, headers, run, raw_text=line, catalog_item_id=str(uuid.uuid4())
    )
    assert no_item.status_code == 422, no_item.text
    # Someone else's run, even for a superuser: answers change the owner's household.
    other = answer(client, superuser_token_headers, run, raw_text=line, unit="lb")
    assert other.status_code == 403

    # A question with nothing read (an older run, or an unreadable line) cannot be completed.
    stored = db.get(GroceryAnalysisRun, uuid.UUID(run["id"]))
    assert stored is not None and stored.result is not None
    stored.result = {
        **stored.result,
        "questions": [{**q, "reading": None} for q in stored.result["questions"]],
    }
    db.add(stored)
    db.commit()
    unread = answer(client, headers, run, raw_text=line, catalog_item_id=item)
    assert unread.status_code == 409
    assert "Drop it and type it again" in unread.json()["detail"]
