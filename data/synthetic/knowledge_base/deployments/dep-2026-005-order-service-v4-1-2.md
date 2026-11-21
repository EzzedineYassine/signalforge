---
doc_id: dep-2026-005
doc_type: deployment
title: Deployment Report — Order Service v4.1.2 Bounded Cache & Deadlock Fix
service: order-service
tags: [deployment, order-service, v4.1.2, memory_leak, deadlock]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Deployment Report: order-service v4.1.2

## Release Metadata
- **Version:** `v4.1.2` (commit `55b8a7c`)
- **Status:** `succeeded`
- **Deployed at:** `2026-10-10T11:15:00Z`

## Changes Included
- Replaced unbounded order-pricing cache dict with bounded LRU cache (`maxsize=2048`, TTL 300s) resolving `inc-2026-004`.
- Enforced deterministic `ORDER BY sku_id ASC` lock acquisition (`rb-012`).

## Post-Deploy Verification
- `memory_utilization` remained flat at `0.46–0.51` across 72 hours; `40P01` deadlock count dropped to zero.
