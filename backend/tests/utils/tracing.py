"""Helpers for asserting on OpenTelemetry spans in tests."""

import copy
from collections.abc import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


class SnapshotExporter(InMemorySpanExporter):
    """Keeps each span as it was at export time, the way a real exporter serializes it.

    The plain in-memory exporter keeps references, so attributes another processor adds after
    export would still show up in the test even though a real collector would never get them.
    """

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return super().export([copy.copy(span) for span in spans])


def by_name(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    matches = [s for s in spans if s.name == name]
    assert len(matches) == 1, (
        f"expected one {name!r} span, got {[s.name for s in spans]}"
    )
    return matches[0]


def parent_of(spans: Sequence[ReadableSpan], span: ReadableSpan) -> ReadableSpan:
    assert span.parent is not None, f"{span.name!r} has no parent"
    return next(s for s in spans if s.context.span_id == span.parent.span_id)


def attribute_text(spans: Sequence[ReadableSpan]) -> str:
    return " ".join(str(v) for s in spans for v in (s.attributes or {}).values())
