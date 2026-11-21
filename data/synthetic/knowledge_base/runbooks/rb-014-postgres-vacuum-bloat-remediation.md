---
doc_id: rb-014
doc_type: runbook
title: PostgreSQL Table Bloat and Autovacuum Tuning
service: postgres-primary
tags: [postgres-primary, autovacuum, bloat, latency, maintenance]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# PostgreSQL Table Bloat and Autovacuum Tuning

## Symptoms

Gradual week-over-week degradation in `order-service` query latency (`request_latency_ms`) and rising sequential scan I/O despite stable row counts, caused by high dead-tuple ratios on hot tables (`orders`, `payment_attempts`).

## Diagnostics

```sql
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
       last_autovacuum
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 10;
```

## Remediation

1. Verify no long-running idle transaction is holding back the xmin horizon and blocking vacuum cleanup (`rb-002`).
2. Lower per-table `autovacuum_vacuum_scale_factor` to `0.02` on high-churn tables and run `VACUUM (ANALYZE, VERBOSE)` off-peak.
3. Avoid `VACUUM FULL` during business hours because it takes an `ACCESS EXCLUSIVE` lock.
