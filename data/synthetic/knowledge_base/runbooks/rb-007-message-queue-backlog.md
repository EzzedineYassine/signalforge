---
doc_id: rb-007
doc_type: runbook
title: Kafka Message Queue Backlog and Consumer Lag Remediation
service: kafka-broker
tags: [message_queue_backlog, queue_depth, kafka-broker, consumer_lag, orders]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Kafka Message Queue Backlog and Consumer Lag Remediation

## Symptoms and Detection Signals

Message queue backlog manifests as a sustained surge in `queue_depth` (> 200 messages above baseline, climbing past 500+), elevated end-to-end processing `request_latency_ms`, and rising consumer memory usage as unacknowledged batches accumulate. Producers (`order-service`, `checkout-service`) continue publishing while downstream consumers fall behind.

## Diagnostic Procedure

1. Inspect consumer group lag per topic and partition (`order-events`, `payment-events`, `notification-events`):

```bash
kafka-consumer-groups.sh --bootstrap-server kafka-broker:9092   --describe --group order-fulfillment-consumers
```

2. Distinguish between three root causes:
   - **Poison-pill message:** A single partition stuck at the exact same offset with repeated consumer exceptions.
   - **Downstream slowdown:** All partitions lagging uniformly while `postgres-primary` `db_connections` or `request_latency_ms` is elevated.
   - **Consumer rebalance storm:** Rapid group rebalances resetting consumption (`rb-015`).

## Mitigation and Remediation

1. If downstream `postgres-primary` is healthy, scale consumer replicas up to the topic partition count (max 12 partitions for `order-events`, `arch-005`).
2. For a poison-pill message, route failing records to the dead-letter topic (`order-events-dlq`) and advance the blocked consumer offset only after capturing the raw payload.
3. Temporarily increase `max.poll.records` cautiously (e.g., 100 -> 250) only if `max.poll.interval.ms` provides sufficient processing margin.
4. Confirm `queue_depth` trends monotonically downward and returns below `50` messages.
