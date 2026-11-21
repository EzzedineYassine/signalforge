---
doc_id: inc-2026-005
doc_type: incident
title: Incident Postmortem — API Gateway East-West Network Latency Spike
service: api-gateway
tags: [incident, network_latency, api-gateway, keepalive, latency]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-005: API Gateway East-West Network Latency Spike

## Summary and Impact

On 2026-10-15 (09:12–09:48 UTC), `api-gateway` p95 `request_latency_ms` jumped from `68 ms` to `435 ms` and `queue_depth` increased by +110 while backend `checkout-service` CPU remained below `0.45`.

## Root Cause

A configuration reload inadvertently reset upstream keep-alive connections (`keepalive_requests=1`), forcing a fresh TLS/TCP handshake to `checkout-service` and `order-service` on every request and exhausting ephemeral ports in `eu-west-1a`.

## Resolution
- Restored upstream keep-alive pool settings (`keepalive=128`, `keepalive_requests=10000`) per `rb-005` and shifted traffic briefly away from `eu-west-1a`.
- Added automated gateway config lint check verifying upstream keep-alive directives before reload.
