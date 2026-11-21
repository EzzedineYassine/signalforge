---
doc_id: inc-2026-009
doc_type: incident
title: Incident Postmortem — Postgres Schema Migration Lock Queue
service: postgres-primary
tags: [incident, postgres-primary, locks, migration, latency]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-009: Postgres Schema Migration Lock Queue

## Summary and Impact

On 2026-11-08 (10:02–10:14 UTC), an `ALTER TABLE orders ADD COLUMN ...` migration waited behind an analytical query on `postgres-primary`, queuing all subsequent `SELECT`/`INSERT` statements from `order-service` and exhausting connection slots (`db_connections=185`).

## Resolution
- Cancelled the blocked migration DDL and analytical query (`rb-002`), immediately releasing the lock queue.
- Adopted mandatory `SET lock_timeout = '3s'` and `CREATE INDEX CONCURRENTLY` policy for all production Alembic migrations.
