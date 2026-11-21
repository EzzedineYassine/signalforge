---
doc_id: rb-004
doc_type: runbook
title: Gradual Memory Leak Triage and Heap Recovery
service: order-service
tags: [memory_leak, memory_utilization, order-service, gc, oom]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Gradual Memory Leak Triage and Heap Recovery

## Symptoms and Detection Signals

A memory leak displays a monotonic upward ramp in `memory_utilization` from normal baseline (`0.42–0.52`) toward `0.90+` over 1–6 hours without returning to baseline during overnight traffic troughs. Late-stage symptoms include rising `cpu_utilization` from garbage-collection thrashing, elevated `request_latency_ms`, and eventual pod OOMKilled restarts (`exit code 137`).

## Diagnostic Procedure

1. Compare `memory_utilization` trend against `request_throughput` over the last 6 hours via `GET /api/v1/metrics?service=order-service&metric_name=memory_utilization`. A rising memory slope during flat or declining throughput confirms retention leak rather than load growth.
2. Capture a heap allocation snapshot from one affected instance before restarting it:

```bash
# Trigger diagnostic memory profile dump on the hottest pod before recycle
kill -USR1 <order-service-pid>
```

3. Check recent commits/deployments for unbounded in-memory caches (e.g., missing `maxsize` on `lru_cache`), unclosed HTTP response streams, or retained trace/request contexts.

## Mitigation and Remediation

1. Perform a **rolling restart** (one pod at a time with 60-second readiness drain) to immediately restore memory headroom while preserving availability.
2. If the leak originated in a recent release, roll back via `rb-006` instead of relying on repeated restarts.
3. Temporarily increase pod memory limit by 25% only as a short-term bridge until the patched image deploys.
4. Confirm post-restart `memory_utilization` stabilizes flat below `0.55` across two diurnal cycles.
