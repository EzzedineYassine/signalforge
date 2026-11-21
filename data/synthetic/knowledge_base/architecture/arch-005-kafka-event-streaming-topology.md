---
doc_id: arch-005
doc_type: architecture
title: Kafka Event Streaming Topics and Consumer Groups
service: kafka-broker
tags: [architecture, kafka-broker, topics, dlq, queue_depth]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Kafka Event Streaming Topics and Consumer Groups

## Core Topics
- **`order-events`** (12 partitions, retention 7 days): Published by `order-service`; consumed by `order-fulfillment-consumers` and analytics pipelines.
- **`payment-events`** (8 partitions, retention 14 days): Published by `payment-service` for charge authorizations and asynchronous settlement webhooks (`inc-2026-008`).
- **`notification-events`** (6 partitions, retention 3 days): Consumed by `notification-worker` for customer receipts.
- **Dead-Letter Topics (`*-dlq`):** Retain unprocessable poison-pill payloads for 30 days so single malformed records never stall partition consumption (`rb-007`).

## Consumer Stability Standards
- All consumer groups use `CooperativeStickyAssignor` and static membership IDs (`rb-015`).
- Alerting thresholds: warning when `queue_depth > 200` for 5 minutes; critical when `queue_depth > 500`.
