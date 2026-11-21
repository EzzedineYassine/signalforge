---
doc_id: inc-2026-004
doc_type: incident
title: Incident Postmortem — Order Service Unbounded Cache Memory Leak
service: order-service
tags: [incident, memory_leak, order-service, oom, cache]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-004: Order Service Unbounded Cache Memory Leak

## Summary and Impact

On 2026-10-09 (02:00–07:45 UTC), `order-service` `memory_utilization` climbed steadily from `0.45` to `0.97` over 5.5 hours, triggering heavy GC pauses (`cpu_utilization` +0.25, `request_latency_ms` +230 ms) and two pod OOMKilled events before rolling restarts stabilized the fleet.

## Root Cause

A feature flag introduced an in-process dictionary caching serialized order pricing snapshots keyed by `order_id` without TTL or LRU eviction. Memory grew linearly with cumulative orders processed.

## Remediation and Prevention
- Performed staggered rolling restart (`rb-004`) to restore service, then deployed bounded `maxsize=2048` cache fix (`dep-2026-005`).
- Added memory-slope burn alerts so gradual leaks trigger triage before exceeding `0.80` utilization.
