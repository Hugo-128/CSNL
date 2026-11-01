# ARCHITECTURAL DECISION RECORD

- **Title:** Fallback strategy for LLM unavailability (circuit-open response)
- **Date:** 2026-11-01
- **Status:** Accepted

## Context

Our service exposes `POST /generate` and depends on an external LLM API (local
Ollama). That dependency is inherently fragile: non-deterministic latency
(0.5s–5s+), aggressive rate limits, and possible outright outage. When the LLM
is slow or down, a naive service would hang threads, exhaust retries, and let
the failure cascade upstream. The circuit breaker (CLOSED→OPEN→HALF-OPEN→
CLOSED) protects us from repeatedly calling a failing dependency, but we still
must decide *what the client receives while the circuit is OPEN*.

## Decision

When the circuit breaker is OPEN, return immediately — without calling the LLM —
an HTTP **503** with a structured body:

```json
{"status": "circuit_open", "retry_after": 10,
 "message": "LLM service temporarily unavailable"}
```

`retry_after` mirrors the breaker's open-timeout so well-behaved clients can
back off coherently. We chose **fail-fast with an explicit error** over the
alternatives below.

## Consequences

- **Positive:** no thread/connection is held during an outage; latency to the
  caller is ~0ms instead of a 30s timeout; the outage is signalled clearly and
  machine-readable; costs are bounded (we never bill retries against a dead
  endpoint).
- **Negative:** the caller gets *no* answer at all during an outage. If a
  caller cannot tolerate an empty result, they must implement their own
  degraded path. This is a deliberate trade-off — honesty over silent
  wrongness.

## Alternatives

1. **Cached response (semantic cache).** Serve the last good answer for a
   similar prompt. Pro: always returns something. Con: risks returning stale or
   wrong content; needs an embedding store and a hit-rate/wrong-answer
   measurement loop. Deferred — out of scope for this lab, noted as a
   follow-up.
2. **Degrade gracefully (e.g. a canned fallback text).** Pro: simple. Con:
   misleading the user with an answer that pretends to be generated is worse
   than an explicit 503; we rejected it on honesty grounds.
3. **Block and wait.** Pro: nothing. Con: reintroduces exactly the cascading
   failure the breaker exists to prevent. Rejected.
