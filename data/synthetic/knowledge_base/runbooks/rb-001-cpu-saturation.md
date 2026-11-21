---
doc_id: rb-001
doc_type: runbook
title: CPU Saturation Triage and Mitigation
service: checkout-service
tags: [cpu_saturation, cpu_utilization, request_latency_ms, checkout-service, scaling]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# CPU Saturation Triage and Mitigation

## Symptoms and Detection Signals

Sustained CPU saturation on `checkout-service` manifests when `cpu_utilization` exceeds `0.85` for more than 5 consecutive minutes accompanied by elevated `request_latency_ms` (> 250 ms) and declining `request_throughput`.

### Correlated Telemetry Indicators
- Anomaly detector flags `cpu_utilization` as `top_feature`.
- Distributed traces show long gaps between child span end and parent span completion (`PlaceOrder`), indicating thread-pool or event-loop starvation.
- `error_rate` climbs above `0.03` due to upstream `api-gateway` 504 timeouts.

## Diagnostic Procedure

1. Confirm whether saturation correlates with an organic traffic surge (`request_throughput` rising proportionally) or a code regression (`cpu_utilization` spiking at flat traffic).
2. Inspect hot worker processes and active threads:

```bash
# Inspect top CPU-consuming worker threads inside checkout-service
ps -eo pid,ppid,pcpu,pmem,cmd --sort=-pcpu | head -n 15
```

3. Check recent deployments on `checkout-service` via `GET /api/v1/deployments?service=checkout-service` to rule out a faulty release (`dep-2026-002` style regex or serialization loop).
4. Review garbage collection logs if `memory_utilization` is simultaneously above `0.80` (cross-check `rb-004`).

## Mitigation and Remediation

- **Traffic-driven saturation:** Scale `checkout-service` replicas horizontally by +50% and enable `api-gateway` adaptive rate limiting (`rb-008`) to protect downstream `payment-service` and `postgres-primary`.
- **Regression-driven saturation:** Trigger immediate rollback to the previous stable version following `rb-006`.
- **Verification:** Confirm `cpu_utilization` drops below `0.65`, p95 `request_latency_ms` returns under `120 ms`, and `error_rate` stabilizes below `0.01` for 10 consecutive minutes.
