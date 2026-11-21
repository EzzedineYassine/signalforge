---
doc_id: inc-2026-010
doc_type: incident
title: Incident Postmortem — API Gateway Stale TLS Intermediate Chain
service: api-gateway
tags: [incident, api-gateway, tls, certificates, outage]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-010: API Gateway Stale TLS Intermediate Chain

## Summary and Impact

On 2026-11-12 (06:00–06:19 UTC), automated certificate rotation installed a leaf certificate without its updated intermediate bundle on two of four `api-gateway` nodes, causing mobile SDK TLS handshake errors on ~50% of requests.

## Resolution
- Verified broken chain via `openssl s_client` (`rb-009`), pushed full-chain PEM bundle, and reloaded gateway listeners gracefully.
- Added pre-reload `openssl verify` smoke check to the certificate deployment hook.
