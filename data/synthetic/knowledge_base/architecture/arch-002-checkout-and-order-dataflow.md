---
doc_id: arch-002
doc_type: architecture
title: Checkout and Order Processing Dataflow
service: checkout-service
tags: [architecture, checkout-service, order-service, dataflow, saga]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Checkout and Order Processing Dataflow

## End-to-End Checkout Saga

1. `api-gateway` forwards `POST /v1/checkout` with propagated W3C `traceparent` (`trace_id`, `span_id`).
2. `checkout-service` validates cart pricing and reserves stock via `order-service` (`SELECT ... FOR UPDATE` ordered by `sku_id ASC` to prevent deadlocks, `rb-012`).
3. `checkout-service` generates a deterministic `idempotency_key` from `(order_id, attempt_epoch)` and invokes `payment-service`.
4. Upon payment authorization, `order-service` commits `status=CONFIRMED` in `postgres-primary` and publishes `OrderConfirmed` to `kafka-broker` topic `order-events`.

## Latency Budgets
- `api-gateway` overhead: <= 15 ms p95
- `checkout-service` orchestration: <= 35 ms p95
- `order-service` persistence: <= 45 ms p95
- `payment-service` external charge: <= 350 ms p95
