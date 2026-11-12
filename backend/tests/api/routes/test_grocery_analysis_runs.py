"""Grocery analysis runs API (P1-1) against real Postgres and Redis, with scripted models.

These carry over what the retired Studies API tests covered: idempotency per user, a terminal state on
every exit path, users reading only their own runs. The model is injected through FastAPI
dependency overrides (get_run_model), so it reaches the background job whichever thread runs it.

FastAPI's TestClient finishes background tasks before returning, so these tests are deterministic
but do not prove the job runs asynchronously; poll_until_finished works against a real server too.

The national catalog differs between a seeded development database and CI's empty one, so these
tests use lines that match nothing, or catalog items the test user's own household owns.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session, select

from app.agents.models import build_model
from app.api.deps import get_run_model
from app.api.routes.grocery_analysis_runs import _idempotency_redis_key
from app.core.config import settings
from app.core.redis import get_redis_client
from app.main import app
from app.models import CatalogItem, Household, PriceObservation, User, Vendor

RUNS_URL = f"{settings.API_V1_STR}/grocery-analysis-runs/"
VENDORS_URL = f"{settings.API_V1_STR}/vendors/"
HOUSEHOLD_URL = f"{settings.API_V1_STR}/household/"


@pytest.fixture
def use_model() -> Iterator[Callable[[Any], None]]:
    """Inject a model into the run's agents for the duration of one test."""

    def _use(model: Any) -> None:
        app.dependency_overrides[get_run_model] = lambda: model

    yield _use
    app.dependency_overrides.pop(get_run_model, None)


# ---------- scripted models ----------


def reads_an_unknown_item(calls: list[int] | None = None) -> FunctionModel:
    """Reads every line as an item no catalog has, so the result does not depend on the catalog."""

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if calls is not None:
            calls.append(1)
        entry = {
            "line": 1,
            "is_price": True,
            "item_text": "zzqx",
            "unit": "lb",
            "price_text": "2.99",
        }  # spellchecker:disable-line
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"lines": [entry]})]
        )

    return FunctionModel(respond)


def malformed_output(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"lines": "nope"})]
    )


def crashing_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    raise RuntimeError("unexpected bug inside the model call")


def invents_numbers(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("Save 42% this week!")])


# ---------- helpers ----------


def vendor_id(
    client: TestClient, headers: dict[str, str], name: str = "Corner store"
) -> str:
    r = client.post(VENDORS_URL, headers=headers, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def capture(
    client: TestClient, headers: dict[str, str], vendor: str, text: str = "zzqx 2.99"
) -> Any:  # spellchecker:disable-line
    body = {
        "kind": "price_capture",
        "text": text,
        "vendor_id": vendor,
        "observed_on": "2026-11-11",
    }
    return client.post(RUNS_URL, headers=headers, json=body)


def poll_until_finished(
    client: TestClient, headers: dict[str, str], run_id: str, timeout_s: float = 10.0
) -> dict[str, Any]:
    """Poll with a deadline instead of sleeping a fixed time; works against a real server too."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = client.get(f"{RUNS_URL}{run_id}", headers=headers)
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(
                f"run {run_id} still {body['status']} after {timeout_s}s"
            )
        time.sleep(0.05)


@pytest.fixture
def vendor(client: TestClient, normal_user_token_headers: dict[str, str]) -> str:
    return vendor_id(client, normal_user_token_headers)


def _household(db: Session, client: TestClient, headers: dict[str, str]) -> Household:
    household_id = client.get(HOUSEHOLD_URL, headers=headers).json()["id"]
    household = db.get(Household, uuid.UUID(household_id))
    assert household is not None
    return household


@pytest.fixture
def own_onion(
    db: Session,
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    vendor: str,
) -> CatalogItem:
    """An item the test user's household owns, with one price: independent of the shared catalog."""
    household = _household(db, client, normal_user_token_headers)
    item = CatalogItem(
        slug=f"test-onion-{uuid.uuid4().hex[:8]}",
        name="Test onion",
        kind="produce",
        category="vegetable",
        scope="household",
        household_id=household.id,
        preparation_yield_factor=0.9,
        cup_equivalent_lb=0.3527,
        storage_days_min=30,
        storage_days_max=30,
        conversion_source="test",
    )
    db.add(item)
    db.flush()
    db.add(
        PriceObservation(
            household_id=household.id,
            catalog_item_id=item.id,
            vendor_id=uuid.UUID(vendor),
            price_cents=299,
            quantity=3,
            unit="lb",
            observed_on=date(2026, 11, 10),
            captured_via="typed",
            source_type="store",
            raw_text="test onions 3 lb 2.99",
        )
    )
    db.commit()
    return item


# ---------- price capture ----------


def test_capture_returns_202_then_succeeds(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    use_model(reads_an_unknown_item())

    r = capture(client, normal_user_token_headers, vendor)

    assert r.status_code == 202
    assert r.json()["status"] == "queued"  # the response is sent before the job runs
    assert r.json()["kind"] == "price_capture"
    run = poll_until_finished(client, normal_user_token_headers, r.json()["id"])
    assert run["status"] == "succeeded"
    result = run["result"]
    assert result["kind"] == "price_capture" and result["drafts"] == []
    (question,) = result["questions"]
    assert question["raw_text"] == "zzqx 2.99"  # spellchecker:disable-line
    assert question["kind"] == "no_match"
    assert run["tools_used"][0] == "capture_agent"
    assert run["model"].startswith("function:")
    assert run["requests"] == 1 and run["input_tokens"] > 0
    assert run["input"]["text"] == "zzqx 2.99"  # spellchecker:disable-line
    assert run["started_at"] is not None and run["finished_at"] is not None


def test_capture_with_the_demo_model_records_the_spec(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    use_model(None)  # the model comes from settings.SIM_MODEL, which is "demo" in tests
    r = capture(
        client, normal_user_token_headers, vendor, "Kroger\nzzqx 3 lb 2.99"
    )  # spellchecker:disable-line
    run = poll_until_finished(client, normal_user_token_headers, r.json()["id"])
    assert run["status"] == "succeeded"
    assert run["model"] == "demo"
    assert run["result"]["ignored"] == ["Kroger"]


# ---------- best buy ----------


def test_best_buy_returns_the_table_and_an_explanation(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    own_onion: CatalogItem,
) -> None:
    use_model(build_model("demo", role="best_buy"))
    body = {
        "kind": "best_buy",
        "needs": [{"catalog_item_id": str(own_onion.id), "cups_per_week": 2}],
        "on": "2026-11-11",
    }

    r = client.post(RUNS_URL, headers=normal_user_token_headers, json=body)
    run = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert run["status"] == "succeeded", run
    result = run["result"]
    (row,) = result["rows"]
    assert row["marked"] and row["label"] == "Corner store"
    assert row["cost_per_usable_cup_cents"] == pytest.approx(39.06, abs=0.01)
    assert "$0.39 per usable cup" in result["explanation"]
    assert run["tools_used"] == ["compare_best_buy", "best_buy_agent"]


def test_best_buy_stands_without_an_explanation(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    own_onion: CatalogItem,
) -> None:
    """A model that keeps inventing numbers loses its explanation, not the table."""
    use_model(FunctionModel(invents_numbers))
    body = {
        "kind": "best_buy",
        "needs": [{"catalog_item_id": str(own_onion.id), "cups_per_week": 2}],
    }

    r = client.post(RUNS_URL, headers=normal_user_token_headers, json=body)
    run = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert run["status"] == "succeeded"
    assert run["result"]["explanation"] is None
    assert "the table is still right" in run["result"]["explanation_error"]
    assert len(run["result"]["rows"]) == 1


# ---------- requests checked before they queue ----------


def test_another_households_vendor_is_rejected(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    theirs = vendor_id(client, superuser_token_headers, "Their store")
    r = capture(client, normal_user_token_headers, theirs)
    assert r.status_code == 422
    assert r.json()["detail"] == "Choose one of your own stores."


@pytest.mark.parametrize(
    ("needs", "detail"),
    [
        (
            [{"catalog_item_id": str(uuid.uuid4()), "cups_per_week": 2}],
            "Unknown produce item.",
        ),
    ],
)
def test_unknown_items_are_rejected(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    needs: list[dict[str, Any]],
    detail: str,
) -> None:
    r = client.post(
        RUNS_URL,
        headers=normal_user_token_headers,
        json={"kind": "best_buy", "needs": needs},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == detail


def test_items_listed_twice_are_rejected(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    own_onion: CatalogItem,
) -> None:
    need = {"catalog_item_id": str(own_onion.id), "cups_per_week": 2}
    r = client.post(
        RUNS_URL,
        headers=normal_user_token_headers,
        json={"kind": "best_buy", "needs": [need, need]},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "List each item once."


def test_another_households_private_item_is_rejected(
    client: TestClient, superuser_token_headers: dict[str, str], own_onion: CatalogItem
) -> None:
    need = {"catalog_item_id": str(own_onion.id), "cups_per_week": 2}
    r = client.post(
        RUNS_URL,
        headers=superuser_token_headers,
        json={"kind": "best_buy", "needs": [need]},
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "study", "prompt": "Optimize case A"},  # the retired kind
        {"kind": "price_capture", "text": "", "vendor_id": str(uuid.uuid4()), "observed_on": "2026-11-11"},
        {"kind": "price_capture", "text": "x", "vendor_id": str(uuid.uuid4()), "observed_on": "2026-11-11", "name": "Riley"},
        {"kind": "best_buy", "needs": []},
        {"kind": "best_buy", "needs": [{"catalog_item_id": str(uuid.uuid4()), "cups_per_week": 0}]},
    ],
)  # fmt: skip
def test_malformed_requests_are_rejected(
    client: TestClient, normal_user_token_headers: dict[str, str], body: dict[str, Any]
) -> None:
    assert (
        client.post(RUNS_URL, headers=normal_user_token_headers, json=body).status_code
        == 422
    )


# ---------- idempotency ----------


def test_same_idempotency_key_returns_same_run(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    calls: list[int] = []
    use_model(reads_an_unknown_item(calls))
    headers = {**normal_user_token_headers, "Idempotency-Key": str(uuid.uuid4())}

    first = capture(client, headers, vendor)
    second = capture(client, headers, vendor)

    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert len(calls) == 1  # the job ran once, not twice


def test_idempotency_keys_are_scoped_per_user(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    use_model(reads_an_unknown_item())
    key = {"Idempotency-Key": str(uuid.uuid4())}
    their_vendor = vendor_id(client, superuser_token_headers, "Their store")

    mine = capture(client, {**normal_user_token_headers, **key}, vendor)
    theirs = capture(client, {**superuser_token_headers, **key}, their_vendor)

    assert mine.json()["id"] != theirs.json()["id"]
    assert "Idempotent-Replayed" not in theirs.headers


def test_key_reserved_by_an_in_flight_request_returns_409(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    db: Session,
    vendor: str,
) -> None:
    """A concurrent duplicate: the key is reserved, but its run row does not exist yet."""
    key = str(uuid.uuid4())
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    get_redis_client().set(_idempotency_redis_key(user.id, key), str(uuid.uuid4()))

    r = capture(client, {**normal_user_token_headers, "Idempotency-Key": key}, vendor)

    assert r.status_code == 409


def test_no_idempotency_key_creates_separate_runs(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    use_model(reads_an_unknown_item())
    first = capture(client, normal_user_token_headers, vendor)
    second = capture(client, normal_user_token_headers, vendor)
    assert first.json()["id"] != second.json()["id"]


def test_a_failed_insert_releases_the_key(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    vendor: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlmodel import Session as SessionType

    key = str(uuid.uuid4())
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()

    def broken_commit(_self: SessionType) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(SessionType, "commit", broken_commit)
    with pytest.raises(RuntimeError):
        capture(client, {**normal_user_token_headers, "Idempotency-Key": key}, vendor)
    monkeypatch.undo()
    assert get_redis_client().get(_idempotency_redis_key(user.id, key)) is None


# ---------- failure handling ----------


def test_invalid_model_output_marks_the_run_failed(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    use_model(FunctionModel(malformed_output))

    r = capture(client, normal_user_token_headers, vendor)
    run = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert run["status"] == "failed"
    assert run["error_reason"] == "invalid_output"
    assert run["result"] is None
    assert run["finished_at"] is not None


def test_unexpected_crash_still_ends_in_a_failed_state(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    vendor: str,
) -> None:
    """A bug must never leave a run stuck in 'running', or clients would poll forever."""
    use_model(FunctionModel(crashing_model))

    r = capture(client, normal_user_token_headers, vendor)
    run = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert run["status"] == "failed"
    assert run["error_reason"] == "internal_error"
    assert "unexpected bug" not in run["error_message"]  # no internals leaked to users


# ---------- access control ----------


def test_other_users_cannot_read_a_run(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(reads_an_unknown_item())
    theirs = vendor_id(client, superuser_token_headers, "Their store")
    r = capture(client, superuser_token_headers, theirs)

    other = client.get(f"{RUNS_URL}{r.json()['id']}", headers=normal_user_token_headers)

    assert other.status_code == 403


def test_missing_run_returns_404(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    r = client.get(f"{RUNS_URL}{uuid.uuid4()}", headers=normal_user_token_headers)
    assert r.status_code == 404


def test_runs_require_authentication(client: TestClient) -> None:
    body = {
        "kind": "best_buy",
        "needs": [{"catalog_item_id": str(uuid.uuid4()), "cups_per_week": 2}],
    }
    assert client.post(RUNS_URL, json=body).status_code == 401
    assert client.get(f"{RUNS_URL}{uuid.uuid4()}").status_code == 401


# ---------- vendors ----------


def test_vendors_are_listed_by_name_and_private(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    suffix = uuid.uuid4().hex[:6]
    vendor_id(client, normal_user_token_headers, f"Zeta market {suffix}")
    created = client.post(
        VENDORS_URL,
        headers=normal_user_token_headers,
        json={
            "name": f"Alpha farm {suffix}",
            "vendor_type": "local_grower",
            "area": "Florence",
        },
    ).json()
    assert created["vendor_type"] == "local_grower" and created["area"] == "Florence"

    mine = [
        v["name"]
        for v in client.get(VENDORS_URL, headers=normal_user_token_headers).json()
    ]
    assert mine.index(f"Alpha farm {suffix}") < mine.index(f"Zeta market {suffix}")
    theirs = [
        v["name"]
        for v in client.get(VENDORS_URL, headers=superuser_token_headers).json()
    ]
    assert f"Alpha farm {suffix}" not in theirs
    stored = db.exec(select(Vendor).where(Vendor.name == f"Alpha farm {suffix}")).one()
    assert stored.area == "Florence"


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "Store", "vendor_type": "mall"},
        {"name": "Store", "address": "123 Main St"},  # never a street address
    ],
)
def test_vendor_input_is_checked(
    client: TestClient, normal_user_token_headers: dict[str, str], body: dict[str, Any]
) -> None:
    assert (
        client.post(
            VENDORS_URL, headers=normal_user_token_headers, json=body
        ).status_code
        == 422
    )


def test_vendors_require_authentication(client: TestClient) -> None:
    assert client.get(VENDORS_URL).status_code == 401
    assert client.post(VENDORS_URL, json={"name": "Store"}).status_code == 401
