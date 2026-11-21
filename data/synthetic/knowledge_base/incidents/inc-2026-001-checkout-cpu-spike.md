---
doc_id: inc-2026-001
doc_type: incident
title: Incident Postmortem — Checkout Service CPU Saturation During Promo Surge
service: checkout-service
tags: [incident, cpu_saturation, checkout-service, regex, latency]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-001: Checkout Service CPU Saturation During Promo Surge

## Summary and Impact

On 2026-09-14 (13:10–13:52 UTC), `checkout-service` experienced severe CPU saturation (`cpu_utilization` peaked at `0.97`), pushing p95 `request_latency_ms` from `75 ms` to `410 ms` and causing a `5.8%` checkout timeout rate.

## Timeline (UTC)
- **13:10:** Flash-sale coupon traffic increased `request_throughput` by 2.1x.
- **13:14:** Anomaly detector flagged `cpu_utilization` breach (`top_feature=cpu_utilization`).
- **13:22:** CPU profiling revealed catastrophic backtracking in promo-code validation regex on malformed coupon strings.
- **13:38:** Emergency scale-out (+4 replicas) and hotfix removing nested regex quantifier deployed.
- **13:52:** `cpu_utilization` returned to `0.48` and `error_rate` dropped below `0.005`.

## Root Cause and Action Items
- **Root cause:** Unbounded regex backtracking in coupon parser triggered under high promo volume.
- **Action items:** Add linear-time regex validation tests, enforce 50 ms CPU guardrails in unit benchmarks, and link `rb-001` in checkout alert annotations.
