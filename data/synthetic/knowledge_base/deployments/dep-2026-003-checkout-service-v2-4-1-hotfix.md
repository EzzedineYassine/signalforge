---
doc_id: dep-2026-003
doc_type: deployment
title: Deployment Report — Checkout Service v2.4.1 Schema Compilation Hotfix
service: checkout-service
tags: [deployment, checkout-service, v2.4.1, hotfix, succeeded]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Deployment Report: checkout-service v2.4.1 Hotfix

## Release Metadata
- **Version:** `v2.4.1` (commit `f21c8a0`)
- **Status:** `succeeded`
- **Deployed at:** `2026-10-23T09:30:00Z`

## Changes Included
- Moved JSON schema compilation to startup singleton initialization (eliminating `v2.4.0` CPU regression).
- Made `shipping_country_code` optional for digital-only carts and added integration test coverage.

## Post-Deploy Verification
- `cpu_utilization` remained at `0.41`, `error_rate` at `0.003`, and p95 `request_latency_ms` at `72 ms`.
