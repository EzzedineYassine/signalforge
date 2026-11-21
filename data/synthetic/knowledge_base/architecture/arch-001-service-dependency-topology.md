---
doc_id: arch-001
doc_type: architecture
title: E-Commerce Microservice Dependency Topology
service: api-gateway
tags: [architecture, topology, dependencies, api-gateway, checkout-service]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# E-Commerce Microservice Dependency Topology

## Synchronous Request Path

External client traffic enters through `api-gateway`, which performs TLS termination, JWT authentication, and adaptive rate limiting (`rb-008`) before routing synchronous RPC/HTTP requests:

- `api-gateway` -> `checkout-service` (`POST /v1/checkout`, 2500 ms timeout)
- `api-gateway` -> `order-service` (`GET /v1/orders/*`, 1500 ms timeout)
- `checkout-service` -> `order-service` (cart validation and order persistence)
- `checkout-service` -> `payment-service` (`ChargeCard`, protected by circuit breaker `rb-010`)

## Stateful Infrastructure Dependencies
- `order-service` and `payment-service` share `postgres-primary` via PgBouncer transaction pooling (`arch-004`).
- `order-service`, `checkout-service`, and `payment-service` publish asynchronous domain events to `kafka-broker` (`arch-005`).

## Blast Radius Summary
- Failure in `postgres-primary` directly impacts `order-service`, `payment-service`, and `checkout-service`.
- Failure in `payment-service` is isolated to checkout completion when `checkout-service` circuit breaker opens cleanly.
