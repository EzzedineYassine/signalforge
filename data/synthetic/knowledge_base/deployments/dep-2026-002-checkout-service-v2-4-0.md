---
doc_id: dep-2026-002
doc_type: deployment
title: Deployment Report — Checkout Service v2.4.0 (Rolled Back)
service: checkout-service
tags: [deployment, checkout-service, v2.4.0, rolled_back, faulty_deployment]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Deployment Report: checkout-service v2.4.0 (Rolled Back)

## Release Metadata
- **Version:** `v2.4.0` (commit `e7b109d`)
- **Status:** `rolled_back`
- **Deployed at:** `2026-10-22T14:04:00Z` (reverted to `v2.3.9` at `14:24:00Z`)

## Changes Included
- Introduced strict JSON schema payload validation on `/v1/checkout` and updated digital-item tax routing.

## Rollback Reason
- Triggered `inc-2026-006`: per-request schema compilation spiked `cpu_utilization` (+0.31) and missing `shipping_country_code` raised `error_rate` to `0.26`. Rolled back under `rb-006`.
