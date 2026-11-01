# Chaos Test Report — Lab 3

Service: FastAPI LLM gateway (`POST /generate` on :8080), breaker config
5 failures / 10 calls, open-timeout 10s, retry 3× with 1s→2s→4s backoff.

## Test 1 — Simulated LLM Downtime

- **What we tested:** stop the LLM dependency (kill `llm_proxy.py`) and drive
  requests.
- **What happened (plain English):** the first five requests each retried three
  times with backoff (1s, 2s) and then returned 502, taking ~3–4s each. On the
  5th failure the breaker opened; every request after that returned an instant
  `503 circuit_open` (0ms), and the LLM API was not called at all.
- **As expected?** Yes. Slow retried failures gave way to fail-fast 503, exactly
  the contrast the brief asks for.
- **Surprises / lessons:** none unexpected — the sliding window (5-in-10) behaved
  exactly as designed. Confirmed the fail-fast path does not touch the upstream.

## Test 2 — Rate Limiting / Flood

- **What we tested:** flood the endpoint with concurrent requests (and, on the
  Ollama side, queue pressure rather than a literal 429).
- **What happened:** under concurrency the upstream queue grew; requests began
  timing out or returning transient errors, feeding the same retry→failure
  path. Grafana showed a sharp error-rate spike while request rate stayed high.
- **As expected?** Yes. With a local Ollama there is no true 429, but the
  observed queueing/timeout behaviour is the point (as the brief notes).
- **Surprises / lessons:** a flood that pushes the breaker past threshold turns
  a throughput problem into a *fast* throughput problem — good for the system,
  and it shows clearly in the error-rate panel.

## Test 3 — Recovery

- **What we tested:** restart the proxy (restore the dependency) and watch the
  breaker recover.
- **What happened:** while still OPEN the service kept failing fast. After the
  10s open-timeout elapsed, the breaker allowed a single HALF-OPEN trial, which
  succeeded, and the state returned to CLOSED; subsequent requests processed
  normally. Grafana showed the error rate drop to zero and the circuit-state
  gauge return to `closed`.
- **As expected?** Yes — the one-trial HALF-OPEN semantics and the automatic
  CLOSED transition worked as designed.
- **Surprises / lessons:** recovery is automatic but *not instant* — there is a
  deliberate 10s "trust warm-up". That is correct and expected.

## Conclusion

Would I deploy this service as-is? **Mostly, yes** — the resilience core
(retry with backoff, timeout, circuit breaker, fail-fast fallback) is solid and
production-shaped, and the observability (metrics + dashboard) is in place.
Two caveats before real production: (1) the fallback is honest but returns
nothing, so a semantic-cache or graceful-degradation path is worth adding for
callers that need an answer; and (2) the breaker thresholds were tuned for a
lab demo — real values need load testing against the actual upstream.
