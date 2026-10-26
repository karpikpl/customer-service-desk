"""Metadata-only spans using the host's existing OpenTelemetry provider."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
from time import perf_counter
from typing import Any, ParamSpec, TypeVar

from opentelemetry import context, trace
from opentelemetry.trace import Span, StatusCode
from opentelemetry.util.types import AttributeValue

P = ParamSpec("P")
R = TypeVar("R")
_turn: ContextVar[Turn | None] = ContextVar("service_desk_turn", default=None)


def failed(span: Span, error: BaseException) -> None:
    span.set_status(StatusCode.ERROR)
    span.set_attribute("error.type", type(error).__name__)


@contextmanager
def operation(name: str, **attributes: AttributeValue) -> Iterator[Span]:
    with trace.get_tracer("service_desk").start_as_current_span(
        "service_desk." + name,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            failed(span, error)
            raise


def traced(
    name: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Coroutine[Any, Any, R]]]:
    def decorate(function: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with operation(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


@dataclass
class Turn:
    span: Span
    started: float = field(default_factory=perf_counter)
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "agent_runs": 0,
            "tool_calls": 0,
            "tool_failures": 0,
            "tool_validation_failures": 0,
            "model_retries": 0,
            "question_repairs": 0,
        }
    )
    first_delivery: bool = False


@contextmanager
def turn() -> Iterator[None]:
    with operation("turn") as span:
        state = Turn(span)
        token = _turn.set(state)
        try:
            yield
        finally:
            span.set_attribute("desk.duration_ms", (perf_counter() - state.started) * 1000)
            for name, value in state.counts.items():
                span.set_attribute("desk." + name, value)
            _turn.reset(token)


def count(name: str, value: int = 1) -> None:
    state = _turn.get()
    if state is not None:
        state.counts[name] = state.counts.get(name, 0) + value


def route(kind: str, name: str = "") -> None:
    state = _turn.get()
    if state is not None:
        state.span.set_attribute("desk.route", kind)
        if name:
            state.span.set_attribute("desk.operation", name)


def handled_error(error: BaseException) -> None:
    state = _turn.get()
    if state is not None:
        failed(state.span, error)


def delivered() -> None:
    state = _turn.get()
    if state is not None:
        count("deliveries")
        if not state.first_delivery:
            state.first_delivery = True
            state.span.set_attribute(
                "desk.first_content_sent_ms", (perf_counter() - state.started) * 1000
            )


def callback_scope() -> Callable[[], AbstractContextManager[None]]:
    """SDK tool callbacks run outside the host's Python/OTEL context."""
    parent, state = context.get_current(), _turn.get()

    @contextmanager
    def bound() -> Iterator[None]:
        attached = context.attach(parent)
        token = _turn.set(state)
        try:
            yield
        finally:
            _turn.reset(token)
            context.detach(attached)

    return bound


class ModelEvents:
    def __init__(self, parent: Span) -> None:
        self.parent = parent
        self.active: dict[str, Span] = {}
        self.calls = 0
        self.usage_events = 0

    def observe(self, kind: str, data: Any) -> None:
        if kind == "model.call_start":
            self.calls += 1
            key = data.turn_id
            if key in self.active:
                self.active.pop(key).end()
            self.active[key] = trace.get_tracer("service_desk").start_span(
                "service_desk.model_call"
            )
        elif kind == "model.call_finished":
            span = self.active.pop(data.turn_id, None)
            if span is not None:
                if getattr(data.outcome, "value", data.outcome) != "success":
                    span.set_status(StatusCode.ERROR)
                span.set_attribute("desk.completion_observed", True)
                span.end()
        elif kind == "model.call_failure":
            count("model_failures")
        elif kind == "assistant.turn_retry":
            count("model_retries")
        elif kind == "assistant.usage":
            self.usage_events += 1
            for source, target in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cache_read_tokens"),
            ):
                value = getattr(data, source, None)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    count(target, value)
            duration = getattr(data, "duration", None)
            if isinstance(duration, timedelta):
                count("model_reported_ms", round(duration.total_seconds() * 1000))

    def close(self) -> None:
        for span in self.active.values():
            span.set_attribute("desk.completion_observed", False)
            span.end()
        self.active.clear()
        calls = self.calls or self.usage_events
        self.parent.set_attribute(
            "desk.model_count_source",
            "call_start" if self.calls else ("usage" if self.usage_events else "unavailable"),
        )
        if calls:
            self.parent.set_attribute("desk.model_calls", calls)
            count("model_calls", calls)
