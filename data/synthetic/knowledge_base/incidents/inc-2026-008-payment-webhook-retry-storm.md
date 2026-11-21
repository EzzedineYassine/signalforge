---
doc_id: inc-2026-008
doc_type: incident
title: Incident Postmortem — Payment Webhook Retry Storm
service: payment-service
tags: [incident, payment-service, webhooks, retries, idempotency]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-008: Payment Webhook Retry Storm

## Summary and Impact

On 2026-11-03 (11:00–11:35 UTC), `payment-service` received 14x normal webhook volume after an external provider replayed queued settlement notifications simultaneously, elevating `db_connections` and `request_latency_ms`.

## Root Cause

Webhook ingestion performed synchronous full-order PDF receipt generation inline before acknowledging HTTP 200, causing provider retries after 3 seconds.

## Resolution
- Decoupled webhook acknowledgement by enqueuing verified webhook payloads onto `kafka-broker` topic `payment-events` (`arch-005`) and enforcing idempotent deduplication (`rb-011`).
