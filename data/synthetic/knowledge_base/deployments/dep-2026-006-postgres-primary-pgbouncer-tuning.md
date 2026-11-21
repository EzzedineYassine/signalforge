---
doc_id: dep-2026-006
doc_type: deployment
title: Deployment Report — Postgres Primary Timeout & PgBouncer Pool Tuning
service: postgres-primary
tags: [deployment, postgres-primary, pgbouncer, db_connections, timeouts]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Deployment Report: postgres-primary PgBouncer & Timeout Tuning

## Release Metadata
- **Version:** `pg-config-2026.09.22` (commit `31e0ab4`)
- **Status:** `succeeded`
- **Deployed at:** `2026-09-22T08:00:00Z`

## Changes Included
- Set `idle_in_transaction_session_timeout = '60s'`, `statement_timeout = '15s'`, and default migration `lock_timeout = '3s'` (`inc-2026-002`, `arch-004`).
- Configured per-service PgBouncer pool caps (`order-service=80`, `payment-service=55`, `checkout-service=30`) and lowered `autovacuum_vacuum_scale_factor` to `0.02` on `orders` (`rb-014`).

## Post-Deploy Verification
- Peak `db_connections` stabilized at `54` during daily traffic highs with zero connection-slot exhaustion alerts.
