---
doc_id: rb-012
doc_type: runbook
title: Order Service Database Deadlock Recovery
service: order-service
tags: [order-service, postgres-primary, deadlock, locks, transactions]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Order Service Database Deadlock Recovery

## Symptoms

`order-service` logs PostgreSQL `SQLSTATE 40P01 (deadlock detected)` errors during concurrent inventory reservation and order status updates. `error_rate` rises modestly (`0.02–0.08`) under peak `request_throughput`.

## Root Cause Analysis

Deadlocks typically occur when concurrent transactions acquire row locks on `order_items` and `inventory_skus` in inconsistent SKU order.

## Mitigation

1. Confirm `order-service` retries transient `40P01` serialization/deadlock errors up to 3 times with randomized backoff (`25–150 ms`).
2. Inspect PostgreSQL deadlock graph logs to identify the exact two statements locking rows in opposite order.
3. Enforce deterministic `ORDER BY sku_id ASC` before `SELECT ... FOR UPDATE` in multi-item checkout transactions.
