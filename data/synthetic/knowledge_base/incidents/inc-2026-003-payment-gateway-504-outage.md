---
doc_id: inc-2026-003
doc_type: incident
title: Incident Postmortem — Primary Card Processor 504 Outage
service: payment-service
tags: [incident, payment_service_failure, payment-service, failover, timeout]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-003: Primary Card Processor 504 Outage

## Summary and Impact

On 2026-10-02 (18:05–18:34 UTC), primary processor `stripe-eu` suffered regional gateway timeouts. `payment-service` `error_rate` jumped to `0.41`, `request_latency_ms` spiked above `680 ms`, and `checkout-service` tripped its circuit breaker (`rb-010`).

## Resolution

Operators executed secondary failover to `adyen-eu` (`rb-003`) at 18:19 UTC. Payment authorization latency normalized to `290 ms` and `error_rate` fell below `0.01` by 18:24 UTC.

## Lessons Learned
- Manual failover took 14 minutes; added automated health-probe scoring to recommend failover after 3 minutes of > 25% upstream 504 errors.
- Confirmed zero duplicate charges thanks to strict `payment-service` idempotency key enforcement (`rb-011`).
