---
doc_id: arch-004
doc_type: architecture
title: PostgreSQL Primary Storage and PgBouncer Pooling Architecture
service: postgres-primary
tags: [architecture, postgres-primary, pgbouncer, db_connections, pooling]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# PostgreSQL Primary Storage and PgBouncer Pooling Architecture

## Connection Budget Allocation

`postgres-primary` (PostgreSQL 16) runs with `max_connections = 200` (15 reserved for superuser/monitoring, `185` available to PgBouncer):
- `order-service` transaction pool: max 80 server connections
- `payment-service` transaction pool: max 55 server connections
- `checkout-service` read/session pool: max 30 server connections
- Ad-hoc migrations & maintenance: max 20 connections

## Guardrails Against Exhaustion
- Mandatory `idle_in_transaction_session_timeout = 60s` and `statement_timeout = 15s` prevent stalled clients from pinning connections (`rb-002`, `inc-2026-002`).
- Application SQLAlchemy pools must set `pool_pre_ping=True` and keep total `replicas * (pool_size + max_overflow)` aligned with PgBouncer client limits.
- Read-heavy reporting queries target the asynchronous streaming replica (`rb-013`).
