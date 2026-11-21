---
doc_id: inc-2026-007
doc_type: incident
title: Incident Postmortem — Kafka Order Events Consumer Backlog
service: kafka-broker
tags: [incident, message_queue_backlog, kafka-broker, queue_depth, lag]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# INC-2026-007: Kafka Order Events Consumer Backlog

## Summary and Impact

On 2026-10-28 (16:15–17:20 UTC), `queue_depth` on `kafka-broker` topic `order-events` surged from `22` to `690` messages, delaying order confirmation emails and downstream analytics indexing by up to 45 minutes.

## Root Cause

A malformed UTF-8 address payload triggered an unhandled `UnicodeDecodeError` in partition 4 of `order-fulfillment-consumers`, causing repeated consumer crashes and group rebalance storms (`rb-015`).

## Resolution
- Isolated the poison-pill offset to `order-events-dlq` per `rb-007`, stabilized consumer group membership with `CooperativeStickyAssignor`, and scaled consumers from 4 to 8 replicas until lag cleared.
