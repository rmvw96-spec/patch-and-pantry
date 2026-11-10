"""OpenTelemetry tracing for the study agent, exported to Phoenix.

Pydantic AI emits spans for agent runs, model requests, and tool calls once instrumentation is
turned on. The OpenInference processor adds the attributes Phoenix uses to show those spans as
agent, LLM, and tool spans. Tracing is off unless PHOENIX_COLLECTOR_ENDPOINT is set, so tests and
CI run without it.

The tracer provider is kept here rather than installed as the global OpenTelemetry provider: the
global can be set only once per process, which would stop tests from installing and removing
their own exporter.
"""

from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from pydantic_ai import Agent, InstrumentationSettings

SERVICE_NAME = "sim-agent"

_provider: TracerProvider | None = None


def setup_tracing(
    *,
    endpoint: str | None,
    include_content: bool,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Turn on tracing for every agent. Returns the provider, or None when tracing stays off.

    Pass `exporter` to send spans somewhere other than the OTLP endpoint; tests pass an in-memory
    exporter, which is exported synchronously so spans are visible as soon as they end.
    """
    global _provider
    processor: SpanProcessor
    if exporter is not None:
        processor = SimpleSpanProcessor(exporter)
    elif endpoint:
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    else:
        return None

    shutdown_tracing()
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    # Order matters: the OpenInference processor adds attributes when a span ends, so it must
    # run before the processor that exports the span.
    provider.add_span_processor(OpenInferenceSpanProcessor())
    provider.add_span_processor(processor)
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=provider, include_content=include_content
        )
    )
    _provider = provider
    return provider


def get_tracer() -> trace.Tracer:
    """Tracer for app spans. A no-op tracer when tracing is off."""
    if _provider is None:
        return trace.NoOpTracer()
    return _provider.get_tracer("app")


def shutdown_tracing() -> None:
    """Flush pending spans, stop exporting, and turn agent instrumentation off."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
    Agent.instrument_all(False)
