#!/usr/bin/env python3
"""
COMP41720 Lab 3 - Resilience Patterns with LLM API Circuit Breaker
FastAPI + Tenacity implementation.

Service contract (fixed for the harness):
  - runs on port 8080
  - exposes exactly POST /generate  {"prompt": "..."}
  - healthy LLM  -> 2xx with generated content
  - circuit OPEN -> 503 {"status": "circuit_open", "retry_after": 10,
                          "message": "LLM service temporarily unavailable"}
  - LLM timeout -> 504 Gateway Timeout
  - auth failure -> 401/403 (non-retryable)
  - 429           -> retried

All resilience knobs are configurable via environment variables (defaults below
match the brief):
  RETRY_MAX_ATTEMPTS=3
  RETRY_INITIAL_DELAY=1.0    (seconds; multiplier 2 -> 1s, 2s, 4s)
  RETRY_MULTIPLIER=2.0
  CB_FAILURE_THRESHOLD=5     (open after 5 failures)
  CB_SLIDING_WINDOW=10       (counted over 10 calls)
  CB_OPEN_TIMEOUT=10.0       (seconds in OPEN before HALF-OPEN)
  LLM_TIMEOUT=30.0           (max duration for a single LLM call)
  LLM_API_URL=http://localhost:11434
  LLM_MODEL=llama3.2:1b
"""

import logging
import os
import threading
import time
from enum import Enum

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from metrics import (
    CIRCUIT_STATE,
    GENERATE_LATENCY,
    LLM_ERRORS_TOTAL,
    LLM_REQUESTS_TOTAL,
    REQUEST_COUNT,
    log_latency,
)

# ---------------------------------------------------------------------------
# configuration (env overridable)
# ---------------------------------------------------------------------------

RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", "3"))
RETRY_INITIAL_DELAY = float(os.environ.get("RETRY_INITIAL_DELAY", "1.0"))
RETRY_MULTIPLIER = float(os.environ.get("RETRY_MULTIPLIER", "2.0"))

CB_FAILURE_THRESHOLD = int(os.environ.get("CB_FAILURE_THRESHOLD", "5"))
CB_SLIDING_WINDOW = int(os.environ.get("CB_SLIDING_WINDOW", "10"))
CB_OPEN_TIMEOUT = float(os.environ.get("CB_OPEN_TIMEOUT", "10.0"))

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "30.0"))
LLM_API_URL = os.environ.get("LLM_API_URL", "http://localhost:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.2:1b")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("llm-gateway")


# ---------------------------------------------------------------------------
# circuit breaker (a small, explicit state machine)
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class CircuitBreaker:
    """Sliding-window, count-based breaker mirroring the brief's defaults.

    CLOSED -> OPEN when `failure_threshold` failures occur in the last
    `window` calls. OPEN fails fast for `open_timeout` seconds, then lets one
    trial request through (HALF-OPEN): success closes, failure re-opens.
    """

    def __init__(self, failure_threshold, window, open_timeout):
        self.failure_threshold = failure_threshold
        self.window = window
        self.open_timeout = open_timeout
        self._state = CircuitState.CLOSED
        self._window = [False] * window  # True = failure, False = success
        self._pos = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    # -- state --------------------------------------------------------------
    @property
    def state(self):
        with self._lock:
            return self._state

    @state.setter
    def state(self, value):
        with self._lock:
            old = self._state
            self._state = value
        if old != value:
            log.info("circuit state change: %s -> %s", old, value)
            CIRCUIT_STATE.labels(state=value.value).set(1)
            for s in (CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN):
                if s != value:
                    CIRCUIT_STATE.labels(state=s.value).set(0)

    # -- call gating --------------------------------------------------------
    def allow_request(self):
        """Return True if a request may proceed, else fail fast (OPEN)."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._opened_at >= self.open_timeout:
                    self._state = CircuitState.HALF_OPEN
                    log.info("circuit state change: open -> half-open (trial allowed)")
                    CIRCUIT_STATE.labels(state="half-open").set(1)
                    CIRCUIT_STATE.labels(state="open").set(0)
                    return True
                return False
            # HALF-OPEN: allow exactly the trial request
            return True

    def _record(self, failed):
        with self._lock:
            self._window[self._pos] = failed
            self._pos = (self._pos + 1) % self.window

            if self._state == CircuitState.HALF_OPEN:
                if failed:
                    self._opened_at = time.monotonic()
                    self._state = CircuitState.OPEN
                    log.info("circuit state change: half-open -> open (trial failed)")
                    CIRCUIT_STATE.labels(state="open").set(1)
                    CIRCUIT_STATE.labels(state="half-open").set(0)
                else:
                    self._state = CircuitState.CLOSED
                    self._window = [False] * self.window
                    log.info("circuit state change: half-open -> closed (trial succeeded)")
                    CIRCUIT_STATE.labels(state="closed").set(1)
                    CIRCUIT_STATE.labels(state="half-open").set(0)
                return

            if self._state == CircuitState.CLOSED:
                failures = sum(self._window)
                if failures >= self.failure_threshold:
                    self._opened_at = time.monotonic()
                    self._state = CircuitState.OPEN
                    log.info(
                        "circuit state change: closed -> open (%d failures in %d calls)",
                        failures, self.window,
                    )
                    CIRCUIT_STATE.labels(state="open").set(1)
                    CIRCUIT_STATE.labels(state="closed").set(0)

    def record_success(self):
        self._record(False)

    def record_failure(self):
        self._record(True)


breaker = CircuitBreaker(CB_FAILURE_THRESHOLD, CB_SLIDING_WINDOW, CB_OPEN_TIMEOUT)

# ---------------------------------------------------------------------------
# custom exceptions (carry a status code, avoid httpx object serialization)
# ---------------------------------------------------------------------------

class UpstreamError(Exception):
    """The LLM API answered with a non-2xx status (or was unreachable)."""

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        self.message = message
        super().__init__(f"upstream status {status_code}: {message}")


# ---------------------------------------------------------------------------
# retry policy (Tenacity)
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """Retry only on transient upstream conditions (429 / 503 / 502)."""
    if isinstance(exc, UpstreamError):
        return exc.status_code in (429, 502, 503)
    if isinstance(exc, httpx.TimeoutException):
        # a timed-out upstream call is transient -> worth retrying
        return True
    return False


retryer = retry(
    reraise=True,
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(
        multiplier=RETRY_INITIAL_DELAY,
        exp_base=RETRY_MULTIPLIER,
        min=RETRY_INITIAL_DELAY,
        max=RETRY_INITIAL_DELAY * (RETRY_MULTIPLIER ** (RETRY_MAX_ATTEMPTS - 1)),
    ),
    retry=retry_if_exception(_is_retryable),
    before_sleep=lambda retry_state: log.info(
        "retry attempt %d/%d after %.1fs (%s)",
        retry_state.attempt_number,
        RETRY_MAX_ATTEMPTS,
        retry_state.next_action.sleep if retry_state.next_action else 0,
        getattr(retry_state.outcome.exception(), "status_code", "n/a")
        if retry_state.outcome
        else "n/a",
    ),
)


# ---------------------------------------------------------------------------
# the LLM call (single attempt), wrapped by retry
# ---------------------------------------------------------------------------

def _call_llm_once(prompt: str) -> str:
    """One call to the Ollama API. Raises UpstreamError on any non-2xx."""
    # trust_env=False: bypass the system HTTP proxy so localhost calls go direct.
    with httpx.Client(timeout=LLM_TIMEOUT, trust_env=False) as client:
        try:
            resp = client.post(
                f"{LLM_API_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": prompt, "stream": False},
            )
        except httpx.TimeoutException as e:
            raise UpstreamError(504, f"timeout: {e}") from e
        except httpx.RequestError as e:
            # connection refused / DNS / etc. -> treat as unreachable (503)
            raise UpstreamError(503, f"unreachable: {e}") from e

        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, "non-200 from LLM")
        data = resp.json()
        return data.get("response", "").strip()


@retryer
def call_llm(prompt: str) -> str:
    """LLM call with retry. Raises the final UpstreamError on exhaustion."""
    return _call_llm_once(prompt)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="LLM Gateway (Lab 3)")


@app.get("/health")
def health():
    return {"status": "ok", "circuit": breaker.state.value}


@app.post("/generate")
@log_latency(GENERATE_LATENCY)
async def generate(request: Request):
    REQUEST_COUNT.inc()
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt:
        return JSONResponse(status_code=422, content={"error": "prompt required"})

    # 1) circuit gate
    if not breaker.allow_request():
        log.info("circuit OPEN: failing fast with 503 circuit_open (no API call)")
        LLM_ERRORS_TOTAL.labels(reason="circuit_open").inc()
        return JSONResponse(
            status_code=503,
            content={
                "status": "circuit_open",
                "retry_after": int(CB_OPEN_TIMEOUT),
                "message": "LLM service temporarily unavailable",
            },
        )

    # 2) call with retry
    LLM_REQUESTS_TOTAL.inc()
    try:
        text = call_llm(prompt)
    except UpstreamError as e:
        code = e.status_code
        # non-retryable auth errors surface directly (retryer skips them)
        if code in (401, 403):
            LLM_ERRORS_TOTAL.labels(reason=f"auth_{code}").inc()
            breaker.record_failure()
            return JSONResponse(
                status_code=code,
                content={"error": "authentication failed with the LLM provider"},
            )
        if code == 504:
            # timeout after retries
            LLM_ERRORS_TOTAL.labels(reason="timeout").inc()
            breaker.record_failure()
            log.warning("LLM call timed out after retries")
            return JSONResponse(status_code=504, content={"error": "gateway timeout"})
        # transient (429/503/502) or other status after retries exhausted
        LLM_ERRORS_TOTAL.labels(reason=f"http_{code}").inc()
        breaker.record_failure()
        log.warning("LLM call failed after retries with HTTP %s", code)
        return JSONResponse(
            status_code=502,
            content={"error": f"upstream LLM returned HTTP {code}"},
        )
    except Exception as e:  # noqa: BLE001 - fail-safe for the boundary
        LLM_ERRORS_TOTAL.labels(reason="unknown").inc()
        breaker.record_failure()
        log.error("unexpected LLM call failure: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    # 3) success
    breaker.record_success()
    return {"response": text, "model": LLM_MODEL}


# ---------------------------------------------------------------------------
# metrics endpoint (Prometheus)
# ---------------------------------------------------------------------------

from metrics import metrics_response  # noqa: E402


@app.get("/metrics")
def metrics():
    return metrics_response()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
