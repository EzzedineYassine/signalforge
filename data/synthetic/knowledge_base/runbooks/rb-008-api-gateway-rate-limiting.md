---
doc_id: rb-008
doc_type: runbook
title: API Gateway Adaptive Rate Limiting and Load Shedding
service: api-gateway
tags: [api-gateway, rate_limiting, load_shedding, throughput, protection]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# API Gateway Adaptive Rate Limiting and Load Shedding

## When to Engage Load Shedding

Engage emergency rate limiting on `api-gateway` when sudden traffic bursts push `checkout-service` `cpu_utilization` above `0.90` or `postgres-primary` `db_connections` above `160`, threatening cascading failure across all tenants.

## Priority Tiers

- **Tier 0 (Never shed first):** `POST /v1/payments/callback` webhooks and `/health` probes.
- **Tier 1 (Protected):** Active `POST /v1/checkout` requests from authenticated sessions.
- **Tier 2 (Shed first):** Anonymous catalog browsing, search autocomplete, and bulk export endpoints.

## Operational Steps

1. Apply per-IP token bucket caps (`30 req/s`, burst `60`) on Tier 2 routes, returning HTTP `429 Too Many Requests` with `Retry-After: 15`.
2. Verify `api-gateway` emits structured warning logs with `event_type="rate_limit"` so 429 responses are distinguished from 5xx service errors.
3. Remove temporary overrides gradually in 25% increments once downstream `cpu_utilization` remains below `0.60` for 15 minutes.
