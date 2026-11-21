---
doc_id: dep-2026-004
doc_type: deployment
title: Deployment Report — Payment Service v1.9.0 Async Webhook Queueing
service: payment-service
tags: [deployment, payment-service, v1.9.0, webhooks, kafka]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Deployment Report: payment-service v1.9.0

## Release Metadata
- **Version:** `v1.9.0` (commit `9c3d41e`)
- **Status:** `succeeded`
- **Deployed at:** `2026-11-04T15:00:00Z`

## Changes Included
- Switched `POST /v1/payments/callback` to enqueue verified webhooks onto `kafka-broker` topic `payment-events` (`inc-2026-008`), returning HTTP 200 in < 15 ms.
- Added automated `adyen-eu` failover readiness probe (`arch-003`).

## Post-Deploy Verification
- Webhook handler p99 latency dropped from `1,850 ms` to `12 ms`; zero idempotency regressions.
