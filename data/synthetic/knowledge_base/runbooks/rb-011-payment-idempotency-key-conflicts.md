---
doc_id: rb-011
doc_type: runbook
title: Payment Idempotency Key Conflicts and Double-Charge Prevention
service: payment-service
tags: [payment-service, idempotency, retries, billing, deduplication]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Payment Idempotency Key Conflicts and Double-Charge Prevention

## Symptoms

`payment-service` emits `HTTP 409 Conflict` or `idempotency_key_mismatch` warnings when `checkout-service` retries a timed-out charge with modified payload fields (such as updated cart totals or currency formatting).

## Investigation

1. Retrieve all events sharing the same `trace_id` or `order_id` attribute across `checkout-service` and `payment-service`.
2. Confirm in `postgres-primary` whether an authoritative ledger record already exists for the `(order_id, idempotency_key)` pair.

## Resolution

1. Never generate a fresh idempotency key for an unconfirmed timeout until the original provider charge status (`succeeded`, `pending`, `failed`) is reconciled via provider lookup.
2. If the original charge succeeded at the processor, replay the stored receipt to `order-service` rather than issuing a second charge.
3. Audit `checkout-service` retry middleware to ensure request payloads remain byte-identical across retries.
