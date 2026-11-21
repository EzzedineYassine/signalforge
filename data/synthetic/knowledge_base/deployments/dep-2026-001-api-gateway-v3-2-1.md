---
doc_id: dep-2026-001
doc_type: deployment
title: Deployment Report — API Gateway v3.2.1 Keep-Alive Hardening
service: api-gateway
tags: [deployment, api-gateway, v3.2.1, keepalive, tls]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Deployment Report: api-gateway v3.2.1

## Release Metadata
- **Version:** `v3.2.1` (commit `a4c91f2`)
- **Status:** `succeeded`
- **Deployed at:** `2026-10-16T10:00:00Z`

## Changes Included
- Added pre-reload validation enforcing `keepalive >= 64` on all upstream service clusters following `inc-2026-005`.
- Upgraded TLS chain verification hook (`rb-009`).

## Post-Deploy Verification
- p95 `request_latency_ms` held steady at `64 ms`; zero 5xx regression over 24 hours.
