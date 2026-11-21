---
doc_id: inc-2026-002
doc_type: incident
title: Incident Postmortem — Postgres Primary Connection Pool Exhaustion
service: postgres-primary
tags: [incident, db_connection_exhaustion, postgres-primary, order-service, pgbouncer]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-002: Postgres Primary Connection Pool Exhaustion

## Summary and Impact

On 2026-09-21 (16:20–17:05 UTC), `db_connections` on `postgres-primary` spiked from `42` to `198` (99% of `max_connections`), causing `order-service` `error_rate` to reach `0.24` and `request_latency_ms` to exceed `620 ms`.

## Root Cause

A background invoice reconciliation job in `order-service` opened a database transaction before making a synchronous HTTP call to an external tax API that stalled for 90 seconds. Connections remained `idle in transaction`, starving PgBouncer and blocking normal checkout inserts.

## Resolution and Follow-ups
- Terminated 112 `idle in transaction` sessions per `rb-002`, restoring normal pool levels within 4 minutes.
- Refactored invoice reconciliation to perform external HTTP calls strictly outside DB transactions.
- Enforced `idle_in_transaction_session_timeout = '60s'` on `postgres-primary` (`dep-2026-006`).
