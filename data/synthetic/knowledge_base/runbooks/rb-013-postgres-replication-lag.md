---
doc_id: rb-013
doc_type: runbook
title: PostgreSQL Read Replica Lag Investigation and Mitigation
service: postgres-primary
tags: [postgres-primary, replication_lag, wal, read_replica, database]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# PostgreSQL Read Replica Lag Investigation and Mitigation

## Symptoms

Analytics and read-heavy order history queries return stale order states while replication replay delay (`pg_stat_replication` `replay_lag`) exceeds `10 seconds`.

## Diagnostics

```sql
SELECT client_addr, state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
       replay_lag
FROM pg_stat_replication;
```

## Remediation

1. Check for bulk backfill jobs or unthrottled `UPDATE`/`DELETE` batches generating excessive WAL volume on `postgres-primary`.
2. Throttle batch writes to 1,000 rows per transaction with 100 ms sleep intervals.
3. Route read-after-write critical `order-service` lookups temporarily to `postgres-primary` (monitoring `db_connections` closely per `rb-002`) until `replay_lag` returns under `1 second`.
