---
doc_id: rb-006
doc_type: runbook
title: Faulty Deployment Detection and Immediate Rollback
service: checkout-service
tags: [faulty_deployment, rollback, deployment, error_rate, checkout-service]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Faulty Deployment Detection and Immediate Rollback

## Symptoms and Detection Signals

A faulty deployment exhibits an abrupt step-function change across multiple metrics (`error_rate`, `cpu_utilization`, `memory_utilization`, `request_latency_ms`) starting within 1–10 minutes after a `succeeded` or `started` deployment event recorded in `GET /api/v1/deployments`.

## Diagnostic Procedure

1. Correlate anomaly onset timestamp against the latest deployment window:

```bash
curl -s "http://127.0.0.1:8000/api/v1/deployments?service=checkout-service&limit=5"
```

2. Compare error signatures in `GET /api/v1/events?service=checkout-service&severity=ERROR` before vs after `deployed_at`. New exception classes, serialization errors, or configuration key failures confirm a release regression.
3. Check whether the release included a database schema migration (`alembic current`). Do **not** run destructive `alembic downgrade` unless verified backward-compatible; prefer rolling back application code to a schema-compatible version.

## Mitigation and Remediation

1. **Halt progressive rollout immediately** and revert the service deployment to the last known-good version tag.
2. Record a `rolled_back` deployment event in SignalForge (`POST /api/v1/deployments/ingest`) with commit SHA and operator attribution.
3. Monitor `error_rate` (< `0.01`), `cpu_utilization` (< `0.65`), and `request_latency_ms` (< `120 ms`) for 15 minutes post-rollback.
4. Open a synthetic incident postmortem linking the deployment SHA, offending anomaly window, and missing pre-deploy test coverage.
