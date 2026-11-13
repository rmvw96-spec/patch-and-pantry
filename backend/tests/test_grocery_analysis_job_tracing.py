"""The grocery analysis job (P1-1): each run is one trace, rooted at a "grocery_analysis_run" span.

Calls execute_run directly with scripted models against the test database, with spans sent to an
in-memory exporter. Also covers failures that the API's checks would normally stop first (a store
deleted after submitting, an unknown item), so the job's own guards stay tested.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session

from app.core.config import settings
from app.core.tracing import setup_tracing, shutdown_tracing
from app.grocery_analysis_jobs import execute_run, model_name
from app.households import get_or_create_household
from app.models import GroceryAnalysisRun, User, Vendor
from tests.utils.tracing import SnapshotExporter, by_name, parent_of
from tests.utils.user import create_random_user

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def model_reads_every_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests script the model's reading, so every line goes to it. Reading by the rules
    first has its own tests (``test_rules_first_*`` in tests/agents/test_capture_agent.py)."""
    monkeypatch.setattr(settings, "CAPTURE_READER", "model")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=True, exporter=exporter)
    yield exporter
    shutdown_tracing()


def _capture_run(db: Session, *, delete_vendor: bool = False) -> GroceryAnalysisRun:
    user = create_random_user(db)
    household = get_or_create_household(db, user.id)
    vendor = Vendor(household_id=household.id, name="Corner store", vendor_type="store")
    db.add(vendor)
    db.commit()
    run = GroceryAnalysisRun(
        owner_id=user.id,
        kind="price_capture",
        input={
            "kind": "price_capture",
            "text": "zzqx 2.99",  # spellchecker:disable-line
            "vendor_id": str(vendor.id),
            "observed_on": "2026-11-11",
        },
    )
    db.add(run)
    db.commit()
    if delete_vendor:
        db.delete(vendor)
        db.commit()
    db.refresh(run)
    return run


@pytest.fixture
def run(db: Session) -> GroceryAnalysisRun:
    return _capture_run(db)


# ---------- scripted models ----------


def reads_one_line(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
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


def malformed_output(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"lines": "nope"})]
    )


def crashing_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    raise RuntimeError("unexpected bug inside the model call")


def stored(db: Session, run: GroceryAnalysisRun) -> GroceryAnalysisRun:
    db.expire_all()
    found = db.get(GroceryAnalysisRun, run.id)
    assert found is not None
    return found


# ---------- tests ----------


async def test_the_run_span_is_the_root_of_the_agent_trace(
    exporter: InMemorySpanExporter, db: Session, run: GroceryAnalysisRun
) -> None:
    await execute_run(run.id, model=FunctionModel(reads_one_line))
    spans = exporter.get_finished_spans()

    root = by_name(spans, "grocery_analysis_run")
    assert root.parent is None
    assert root.attributes["openinference.span.kind"] == "CHAIN"
    assert root.attributes["grocery_analysis_run.id"] == str(run.id)
    assert root.attributes["grocery_analysis_run.kind"] == "price_capture"
    assert root.attributes["grocery_analysis_run.status"] == "succeeded"
    assert root.status.status_code != StatusCode.ERROR

    agent = by_name(spans, "invoke_agent capture_agent")
    assert parent_of(spans, agent) is root
    # One trace per run: every span shares the run span's trace id.
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert stored(db, run).status == "succeeded"


async def test_a_failure_is_recorded_on_the_span(
    exporter: InMemorySpanExporter, db: Session, run: GroceryAnalysisRun
) -> None:
    await execute_run(run.id, model=FunctionModel(malformed_output))
    root = by_name(exporter.get_finished_spans(), "grocery_analysis_run")

    assert root.attributes["grocery_analysis_run.status"] == "failed"
    assert root.attributes["grocery_analysis_run.error_reason"] == "invalid_output"
    assert root.status.status_code == StatusCode.ERROR
    assert [e.name for e in root.events] == ["exception"]
    assert stored(db, run).status == "failed"


async def test_an_unexpected_crash_is_recorded_on_the_span(
    exporter: InMemorySpanExporter, db: Session, run: GroceryAnalysisRun
) -> None:
    await execute_run(run.id, model=FunctionModel(crashing_model))
    root = by_name(exporter.get_finished_spans(), "grocery_analysis_run")

    assert root.attributes["grocery_analysis_run.status"] == "failed"
    assert root.attributes["grocery_analysis_run.error_reason"] == "internal_error"
    assert root.status.status_code == StatusCode.ERROR
    assert "unexpected bug" in root.events[0].attributes["exception.message"]
    # The terminal-state rule still holds with tracing on.
    assert stored(db, run).status == "failed"


async def test_a_deleted_run_still_ends_its_span(
    exporter: InMemorySpanExporter,
) -> None:
    await execute_run(uuid.uuid4(), model=FunctionModel(reads_one_line))
    spans = exporter.get_finished_spans()

    assert [s.name for s in spans] == ["grocery_analysis_run"]
    assert spans[0].attributes["grocery_analysis_run.status"] == "deleted"


async def test_a_store_deleted_after_submitting_fails_the_run(db: Session) -> None:
    run = _capture_run(db, delete_vendor=True)
    await execute_run(run.id, model=FunctionModel(reads_one_line))
    failed = stored(db, run)
    assert (failed.status, failed.error_reason) == ("failed", "unknown_vendor")


async def test_an_unknown_item_fails_a_best_buy_run(db: Session) -> None:
    user = create_random_user(db)
    run = GroceryAnalysisRun(
        owner_id=user.id,
        kind="best_buy",
        input={"kind": "best_buy", "needs": [{"catalog_item_id": str(uuid.uuid4()), "per_week": 2}], "on": None},
    )  # fmt: skip
    db.add(run)
    db.commit()

    await execute_run(run.id, model=FunctionModel(reads_one_line))

    failed = stored(db, run)
    assert (failed.status, failed.error_reason) == ("failed", "invalid_input")
    assert failed.model == "function:reads_one_line:"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (None, "demo"),
        ("test", "test"),
        (FunctionModel(reads_one_line), "function:reads_one_line:"),
    ],
)
def test_model_name(model: Any, expected: str) -> None:
    assert model_name(model) == expected


def test_runs_are_deleted_with_their_user(db: Session) -> None:
    run = _capture_run(db)
    user = db.get(User, run.owner_id)
    assert user is not None
    db.delete(user)
    db.commit()
    db.expire_all()
    assert db.get(GroceryAnalysisRun, run.id) is None
