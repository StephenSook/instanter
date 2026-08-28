"""Custom OpenTelemetry spans for the triage graph.

Strands already traces model loops. These spans are ours: they name the
deterministic work (deadline math, the escalation ladder, the attorney
interrupt) so AgentCore Observability shows what the run actually did to
each case rather than a flat sequence of generic LLM calls.

Fail-soft on the tracer: a missing provider is a no-op span, never a
crashed sweep. Never catch the wrapped body: the attorney interrupt is
raised as an exception, and swallowing it would skip the human.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from typing import Any

from agent.audit import AuditEvent
from agent.run_context import RunContext


@contextmanager
def instanter_span(
    ctx: RunContext | None,
    name: str,
    **attrs: Any,
) -> Iterator[None]:
    """Open a span named ``instanter.*`` and, when a context is present,
    record it on the audit trail so the run receipt can print it."""
    span_id = ""
    otel: AbstractContextManager[Any] = nullcontext()
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("instanter")
        otel = tracer.start_as_current_span(name)
    except Exception:
        otel = nullcontext()

    entered = False
    span: Any = None
    try:
        span = otel.__enter__()
        entered = True
    except Exception:
        span = None
    if span is not None:
        with suppress(Exception):
            for key, value in attrs.items():
                if value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    span.set_attribute(f"instanter.{key}", value)
                else:
                    span.set_attribute(f"instanter.{key}", str(value)[:200])
            ctx_span = span.get_span_context()
            span_id = format(ctx_span.span_id, "016x") if ctx_span else ""

    try:
        yield
    finally:
        if entered:
            with suppress(Exception):
                otel.__exit__(*sys.exc_info())
        if ctx is not None:
            payload: dict[str, Any] = {"name": name, "span_id": span_id}
            payload.update({k: v for k, v in attrs.items() if v is not None})
            ctx.span_log.append(payload)
            with suppress(Exception):
                ctx.audit.append(
                    AuditEvent(
                        kind="span",
                        case_id=str(attrs["case_id"]) if attrs.get("case_id") else None,
                        payload=payload,
                        run_id=ctx.run_id,
                    )
                )
