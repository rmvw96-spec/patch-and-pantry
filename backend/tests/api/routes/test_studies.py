"""Layer 3 of the test pyramid: the async study API against real Postgres, Redis, and Prefect.

The model is injected through FastAPI dependency overrides (get_study_model), not Agent.override,
so the injected model reaches the background job no matter which thread or task runs it.

Note: FastAPI's TestClient finishes background tasks before returning, so these tests are
deterministic, but they do not prove the job runs asynchronously. A run against a real server
(the nightly job) proves that; poll_until_finished is written to work there too.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prefect.testing.utilities import prefect_test_harness
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session, select

from app.agents import simulator
from app.api.deps import get_study_model
from app.api.routes.studies import _idempotency_redis_key
from app.core.config import settings
from app.core.redis import get_redis_client
from app.main import app
from app.models import User

STUDIES_URL = f"{settings.API_V1_STR}/studies/"


@pytest.fixture(scope="module", autouse=True)
def prefect_harness() -> Iterator[None]:
    """A throwaway Prefect API for this module, so simulation flows run without a Prefect server."""
    with prefect_test_harness():
        yield


@pytest.fixture
def use_model() -> Iterator[Callable[[Any], None]]:
    """Inject a model into the study agents for the duration of one test."""

    def _use(model: Any) -> None:
        app.dependency_overrides[get_study_model] = lambda: model

    yield _use
    app.dependency_overrides.pop(get_study_model, None)


# ---------- scripted models ----------


def _call(name: str, **args: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(name, args)])


def _final(info: AgentInfo, **args: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


def _was_rejected(part: Any) -> bool:
    return isinstance(part, ToolReturnPart) and part.outcome == "retried"


def simulate_case_a_then_report(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    last = messages[-1].parts[-1]
    if isinstance(last, ToolReturnPart) and not _was_rejected(last):
        outlet = last.content["outlet_bar"]
        return _final(info, objective=outlet, feasible=outlet > 0)
    return _call("run_simulation", case_id="A", inlet_bar=60.0)


def unknown_case_then_case_a(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    last = messages[-1].parts[-1]
    if _was_rejected(last):  # the simulator rejected case "Z": switch to a known case
        return _call("run_simulation", case_id="A", inlet_bar=60.0)
    if isinstance(last, ToolReturnPart):
        outlet = last.content["outlet_bar"]
        return _final(info, objective=outlet, feasible=outlet > 0)
    return _call("run_simulation", case_id="Z", inlet_bar=60.0)


def malformed_output(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _final(info, objective="not a number")


def crashing_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    raise RuntimeError("unexpected bug inside the model call")


# ---------- helpers ----------


def submit(
    client: TestClient, headers: dict[str, str], prompt: str = "Optimize case A"
) -> Any:
    return client.post(STUDIES_URL, headers=headers, json={"prompt": prompt})


def poll_until_finished(
    client: TestClient,
    headers: dict[str, str],
    study_id: str,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Poll with a deadline instead of sleeping a fixed time; works against a real server too."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = client.get(f"{STUDIES_URL}{study_id}", headers=headers)
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(
                f"study {study_id} still {body['status']} after {timeout_s}s"
            )
        time.sleep(0.05)


@pytest.fixture
def count_simulations(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every simulator calculation. Prefect looks up the function at call time."""
    calls: list[float] = []
    real_solve = simulator.solve_outlet_pressure

    def counting_solve(case: simulator.PipeCase, inlet_bar: float) -> float:
        calls.append(inlet_bar)
        return real_solve(case, inlet_bar)

    monkeypatch.setattr(simulator, "solve_outlet_pressure", counting_solve)
    return calls


# ---------- happy path ----------


def test_submit_returns_202_then_succeeds(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(FunctionModel(simulate_case_a_then_report))

    r = submit(client, normal_user_token_headers)

    assert r.status_code == 202
    assert r.json()["status"] == "queued"  # the response is sent before the job runs
    study = poll_until_finished(client, normal_user_token_headers, r.json()["id"])
    assert study["status"] == "succeeded"
    # Case A at 60 bar: sqrt(60^2 - 1100) = 50.0, computed by the simulator inside Prefect
    assert study["result"] == {"objective": 50.0, "feasible": True, "review": None}
    assert study["started_at"] is not None
    assert study["finished_at"] is not None


# ---------- idempotency ----------


def test_same_idempotency_key_returns_same_study(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    count_simulations: list[float],
) -> None:
    use_model(FunctionModel(simulate_case_a_then_report))
    headers = {**normal_user_token_headers, "Idempotency-Key": str(uuid.uuid4())}

    first = submit(client, headers)
    second = submit(client, headers)

    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert count_simulations == [60.0]  # the job ran once, not twice


def test_idempotency_keys_are_scoped_per_user(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(FunctionModel(simulate_case_a_then_report))
    key = {"Idempotency-Key": str(uuid.uuid4())}

    mine = submit(client, {**normal_user_token_headers, **key})
    theirs = submit(client, {**superuser_token_headers, **key})

    assert mine.json()["id"] != theirs.json()["id"]
    assert "Idempotent-Replayed" not in theirs.headers


def test_key_reserved_by_an_in_flight_request_returns_409(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    db: Session,
) -> None:
    """Simulates a concurrent duplicate: the key is reserved, but its study row does not exist yet."""
    key = str(uuid.uuid4())
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    get_redis_client().set(_idempotency_redis_key(user.id, key), str(uuid.uuid4()))

    r = submit(client, {**normal_user_token_headers, "Idempotency-Key": key})

    assert r.status_code == 409


def test_no_idempotency_key_creates_separate_studies(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(FunctionModel(simulate_case_a_then_report))

    first = submit(client, normal_user_token_headers)
    second = submit(client, normal_user_token_headers)

    assert first.json()["id"] != second.json()["id"]


# ---------- failure handling ----------


def test_invalid_model_output_marks_study_failed(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(FunctionModel(malformed_output))

    r = submit(client, normal_user_token_headers)
    study = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert study["status"] == "failed"
    assert study["error_reason"] == "invalid_output"
    assert study["result"] is None
    assert study["finished_at"] is not None


def test_unexpected_crash_still_ends_in_a_failed_state(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    """A bug must never leave a study stuck in 'running', or clients would poll forever."""
    use_model(FunctionModel(crashing_model))

    r = submit(client, normal_user_token_headers)
    study = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert study["status"] == "failed"
    assert study["error_reason"] == "internal_error"
    assert (
        "unexpected bug" not in study["error_message"]
    )  # no internals leaked to users


def test_transient_simulator_error_is_retried(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_model(FunctionModel(simulate_case_a_then_report))
    attempts: list[float] = []
    real_solve = simulator.solve_outlet_pressure

    def flaky_solve(case: simulator.PipeCase, inlet_bar: float) -> float:
        attempts.append(inlet_bar)
        if len(attempts) == 1:
            raise ConnectionError("simulator briefly unavailable")
        return real_solve(case, inlet_bar)

    monkeypatch.setattr(simulator, "solve_outlet_pressure", flaky_solve)

    r = submit(client, normal_user_token_headers)
    study = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert len(attempts) == 2  # Prefect retried the failed simulation once
    assert study["status"] == "succeeded"
    assert study["result"]["objective"] == 50.0


def test_unknown_case_is_reported_back_to_the_model(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(FunctionModel(unknown_case_then_case_a))

    r = submit(client, normal_user_token_headers)
    study = poll_until_finished(client, normal_user_token_headers, r.json()["id"])

    assert (
        study["status"] == "succeeded"
    )  # the agent corrected itself after the rejection
    assert study["result"]["objective"] == 50.0


# ---------- access control ----------


def test_other_users_cannot_read_a_study(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    use_model: Callable[[Any], None],
) -> None:
    use_model(FunctionModel(simulate_case_a_then_report))
    r = submit(client, superuser_token_headers)

    other = client.get(
        f"{STUDIES_URL}{r.json()['id']}", headers=normal_user_token_headers
    )

    assert other.status_code == 403


def test_missing_study_returns_404(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    r = client.get(f"{STUDIES_URL}{uuid.uuid4()}", headers=normal_user_token_headers)
    assert r.status_code == 404


def test_submit_requires_authentication(client: TestClient) -> None:
    r = client.post(STUDIES_URL, json={"prompt": "Optimize case A"})
    assert r.status_code == 401
