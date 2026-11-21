---
doc_id: rb-002
doc_type: runbook
title: PostgreSQL Connection Pool Exhaustion Runbook
service: postgres-primary
tags: [db_connection_exhaustion, db_connections, postgres-primary, pgbouncer, pool]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# PostgreSQL Connection Pool Exhaustion Runbook

## Symptoms and Detection Signals

Connection pool exhaustion occurs when `db_connections` on `postgres-primary` or PgBouncer approaches `max_connections` (synthetic baseline cap: `200`, warning at `140`, critical at `170`). `order-service` and `checkout-service` log `QueuePool limit overflow` or `remaining connection slots are reserved` errors while `request_latency_ms` exceeds `400 ms` and `error_rate` spikes past `0.15`.

## Diagnostic Procedure

### Inspect Active and Idle-in-Transaction Sessions

```sql
SELECT state, application_name, count(*) AS sessions,
       max(now() - state_change) AS longest_wait
FROM pg_stat_activity
WHERE datname = 'orders_prod'
GROUP BY state, application_name
ORDER BY sessions DESC;
```

### Identify Blocking Locks

```sql
SELECT pid, usename, state, wait_event_type, wait_event,
       left(query, 120) AS query_snippet
FROM pg_stat_activity
WHERE wait_event IS NOT NULL
ORDER BY query_start ASC
LIMIT 20;
```

## Mitigation and Remediation

1. Terminate runaway `idle in transaction` connections older than 5 minutes using `pg_terminate_backend(pid)` after verifying the PID is not a schema migration.
2. Temporarily lower application worker concurrency on `order-service` if a deploy increased per-pod SQLAlchemy `pool_size + max_overflow` beyond the PgBouncer transaction-pool budget (`arch-004`).
3. Verify `idle_in_transaction_session_timeout = '60s'` and `statement_timeout = '15s'` are active on `postgres-primary`.
4. Confirm `db_connections` falls below `80` and `order-service` `error_rate` returns under `0.01`.
