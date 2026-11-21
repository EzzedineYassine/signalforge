---
doc_id: rb-010
doc_type: runbook
title: Checkout Service Circuit Breaker Open Response
service: checkout-service
tags: [checkout-service, circuit_breaker, payment-service, resilience, fallback]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Checkout Service Circuit Breaker Open Response

## Symptoms

`checkout-service` logs `circuit_breaker_state=OPEN` for downstream `payment-service` calls. Fast-fail HTTP 503 responses keep `checkout-service` `request_latency_ms` low (< 25 ms) while `error_rate` stays elevated until the downstream dependency recovers.

## Diagnostic Checks

1. Verify the breaker opened for legitimate downstream failure (`payment-service` 5xx rate > 50% over a 20-request rolling window) rather than a misconfigured timeout threshold.
2. Inspect half-open probe outcomes every 30 seconds in `GET /api/v1/events?service=checkout-service&trace_id=...`.

## Recovery Actions

1. Remediate the root cause in `payment-service` (`rb-003`) before touching breaker settings; never disable the circuit breaker during an active downstream outage.
2. Once `payment-service` passes 5 consecutive half-open probes, the breaker automatically closes.
3. Confirm queued retry workers drain cleanly without creating duplicate charges (`rb-011`).
