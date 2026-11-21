---
doc_id: arch-006
doc_type: architecture
title: Observability, Correlation IDs, and 7-Metric Telemetry Contract
service: order-service
tags: [architecture, observability, opentelemetry, metrics, tracing]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Observability, Correlation IDs, and 7-Metric Telemetry Contract

## Mandatory Signal Correlation

Every service emits OpenTelemetry logs, metrics, and distributed traces with canonical lowercase kebab-case `service.name` and UTC timestamps:
- HTTP/gRPC requests propagate `trace_id` and `span_id` into structured JSON logs (`events.trace_id`) and `trace_spans`.
- All deployments emit lifecycle records (`started`, `succeeded`, `failed`, `rolled_back`) to correlate regressions (`rb-006`).

## Standard 7-Metric Operational Contract
Each service exports 1-minute aggregates for anomaly detection:
`cpu_utilization`, `memory_utilization`, `request_latency_ms`, `request_throughput`, `error_rate`, `db_connections`, and `queue_depth`.
