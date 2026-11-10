"""Tracing of study jobs: each study is one trace, rooted at a "study" span.

Calls execute_study directly with a scripted model, against the real simulator (run through a
throwaway Prefect API) and the test database, with spans sent to an in-memory exporter.
"""

import uuid
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from prefect.testing.utilities import prefect_test_harness
from pydantic_ai import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session

from app.core.tracing import setup_tracing, shutdown_tracing
from app.models import Study
from app.study_jobs import execute_study
from tests.utils.tracing import SnapshotExporter, by_name, parent_of
from tests.utils.user import create_random_user

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module", autouse=True)
def prefect_harness() -> Iterator[None]:
    """A throwaway Prefect API for this module, so simulation flows run without a Prefect server."""
    with prefect_test_harness():
        yield


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=True, exporter=exporter)
    yield exporter
    shutdown_tracing()


@pytest.fixture
def study(db: Session) -> Study:
    user = create_random_user(db)
    study = Study(owner_id=user.id, prompt="Optimize case A")
    db.add(study)
    db.commit()
    db.refresh(study)
    return study


# ---------- scripted models ----------


def simulate_case_a_then_report(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    last = messages[-1].parts[-1]
    if isinstance(last, ToolReturnPart):
        outlet = last.content["outlet_bar"]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"objective": outlet, "feasible": outlet > 0},
                )
            ]
        )
    return ModelResponse(
        parts=[ToolCallPart("run_simulation", {"case_id": "A", "inlet_bar": 60.0})]
    )


def malformed_output(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"objective": "not a number"})]
    )


def crashing_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    raise RuntimeError("unexpected bug inside the model call")


def status_in_db(db: Session, study: Study) -> str:
    db.refresh(study)
    return study.status


# ---------- tests ----------


async def test_study_span_is_the_root_of_the_agent_trace(
    exporter: InMemorySpanExporter, db: Session, study: Study
) -> None:
    await execute_study(study.id, model=FunctionModel(simulate_case_a_then_report))
    spans = exporter.get_finished_spans()

    root = by_name(spans, "study")
    assert root.parent is None
    assert root.attributes["openinference.span.kind"] == "CHAIN"
    assert root.attributes["study.id"] == str(study.id)
    assert root.attributes["study.status"] == "succeeded"
    assert root.status.status_code != StatusCode.ERROR

    agent = by_name(spans, "invoke_agent study_agent")
    assert parent_of(spans, agent) is root
    # One trace per study: every span shares the study span's trace id.
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert status_in_db(db, study) == "succeeded"


async def test_study_failure_is_recorded_on_the_span(
    exporter: InMemorySpanExporter, db: Session, study: Study
) -> None:
    await execute_study(study.id, model=FunctionModel(malformed_output))
    root = by_name(exporter.get_finished_spans(), "study")

    assert root.attributes["study.status"] == "failed"
    assert root.attributes["study.error_reason"] == "invalid_output"
    assert root.status.status_code == StatusCode.ERROR
    assert [e.name for e in root.events] == ["exception"]
    assert status_in_db(db, study) == "failed"


async def test_unexpected_crash_is_recorded_on_the_span(
    exporter: InMemorySpanExporter, db: Session, study: Study
) -> None:
    await execute_study(study.id, model=FunctionModel(crashing_model))
    root = by_name(exporter.get_finished_spans(), "study")

    assert root.attributes["study.status"] == "failed"
    assert root.attributes["study.error_reason"] == "internal_error"
    assert root.status.status_code == StatusCode.ERROR
    assert "unexpected bug" in root.events[0].attributes["exception.message"]
    # The terminal-state rule still holds with tracing on.
    assert status_in_db(db, study) == "failed"


async def test_deleted_study_still_ends_its_span(
    exporter: InMemorySpanExporter,
) -> None:
    missing_id = uuid.uuid4()
    await execute_study(missing_id, model=FunctionModel(simulate_case_a_then_report))
    spans = exporter.get_finished_spans()

    assert [s.name for s in spans] == ["study"]
    assert spans[0].attributes["study.status"] == "deleted"
