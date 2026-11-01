#!/usr/bin/env python3
"""Prometheus metrics for the Lab 3 LLM gateway."""

import time
from functools import wraps

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response

# circuit breaker state gauge: one labelled gauge, active state = 1, others 0.
CIRCUIT_STATE = Gauge(
    "llm_circuit_breaker_state",
    "Current circuit breaker state (1 = active).",
    labelnames=["state"],
)

# initialise all three labels so the gauge always appears in /metrics
for _s in ("closed", "open", "half-open"):
    CIRCUIT_STATE.labels(state=_s).set(1 if _s == "closed" else 0)

REQUEST_COUNT = Counter(
    "llm_generate_requests_total",
    "Total POST /generate requests received.",
)

LLM_REQUESTS_TOTAL = Counter(
    "llm_upstream_requests_total",
    "Total LLM API calls attempted (before retry).",
)

LLM_ERRORS_TOTAL = Counter(
    "llm_errors_total",
    "LLM call errors by reason.",
    labelnames=["reason"],
)

GENERATE_LATENCY = Histogram(
    "llm_generate_duration_seconds",
    "End-to-end /generate latency.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)


def log_latency(histogram):
    """Decorator that records handler duration into a histogram."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                histogram.observe(time.monotonic() - start)

        return wrapper

    return decorator


def metrics_response() -> Response:
    """Return a FastAPI Response containing Prometheus text metrics."""
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
