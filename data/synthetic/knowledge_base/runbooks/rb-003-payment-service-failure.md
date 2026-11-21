---
doc_id: rb-003
doc_type: runbook
title: Payment Service Outage and Provider Failover
service: payment-service
tags: [payment_service_failure, error_rate, payment-service, failover, timeout]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Payment Service Outage and Provider Failover

## Symptoms and Detection Signals

`payment-service` failure presents as a sharp rise in `error_rate` (> `0.20`), `request_latency_ms` exceeding `500 ms` (5-second upstream card processor timeouts), and a steep drop in completed `request_throughput` (> 35% decline). Distributed traces show `ChargeCard` client spans terminating with `status_code=ERROR` and `http.status_code=504` or `502`.

## Diagnostic Procedure

1. Query recent `payment-service` error events and traces to distinguish primary card processor (`stripe-eu`) outage from internal database or TLS errors:

```bash
# Query SignalForge traces for failing payment spans
curl -s "http://127.0.0.1:8000/api/v1/traces?service=payment-service&status_code=ERROR&limit=20"
```

2. Inspect `payment-service` circuit breaker state (`rb-010`) and verify whether `stripe-eu` health probes are failing while secondary provider `adyen-eu` remains healthy.
3. Check for elevated idempotency conflicts (`rb-011`) caused by aggressive client retries during timeouts.

## Mitigation and Remediation

### Activate Secondary Payment Processor Failover
1. Flip the runtime provider weight flag to route 100% of new `ChargeCard` traffic from `stripe-eu` to `adyen-eu` (`arch-003`).
2. Enforce strict exponential backoff with jitter on `checkout-service` payment retries (max 2 retries, 2000 ms timeout) so retry storms do not saturate worker pools.
3. Verify `payment-service` `error_rate` falls below `0.02`, `ChargeCard` spans return `status_code=OK`, and checkout conversion recovers.
4. Keep primary provider quarantined for 15 minutes after external status clears before gradual 10% canary re-enablement.
