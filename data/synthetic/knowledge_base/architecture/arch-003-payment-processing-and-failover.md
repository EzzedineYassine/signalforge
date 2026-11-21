---
doc_id: arch-003
doc_type: architecture
title: Payment Processing Architecture and Dual-Provider Failover
service: payment-service
tags: [architecture, payment-service, failover, idempotency, stripe-eu, adyen-eu]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Payment Processing Architecture and Dual-Provider Failover

## Dual-Provider Routing Design

`payment-service` abstracts external card acquisition behind a provider router supporting two processors:
- **Primary (`stripe-eu`):** Default 100% weight during healthy operation; typical latency 220–320 ms.
- **Secondary (`adyen-eu`):** Warm standby with active synthetic canary probes every 60 seconds; typical latency 260–340 ms.

## Safety Guarantees
- Every charge request persists a `payment_attempts` row keyed by unique `(order_id, idempotency_key)` in `postgres-primary` before contacting an external provider (`rb-011`).
- Hard outbound socket timeout is `5000 ms`; `checkout-service` applies circuit breaking (`rb-010`) after 50% failure rate across 20 calls.
- Webhook settlements arrive asynchronously on `POST /v1/payments/callback` and enqueue directly to `kafka-broker` topic `payment-events`.
