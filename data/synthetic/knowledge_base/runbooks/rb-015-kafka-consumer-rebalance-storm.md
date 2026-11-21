---
doc_id: rb-015
doc_type: runbook
title: Kafka Consumer Group Rebalance Storm Recovery
service: kafka-broker
tags: [kafka-broker, rebalance, consumer_group, queue_depth, heartbeat]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# Kafka Consumer Group Rebalance Storm Recovery

## Symptoms

`kafka-broker` emits repeated `Preparing to rebalance group` and `Revoking previously assigned partitions` logs every 30–60 seconds while `queue_depth` climbs steadily because consumers spend more time rebalancing than processing messages.

## Root Cause Checks

1. Inspect whether individual message batches exceed `max.poll.interval.ms` (default 300s, sometimes misconfigured to 30s), causing the broker to evict healthy slow consumers.
2. Check for CrashLooping consumer pods triggering continuous membership churn.

## Remediation

1. Enable cooperative sticky assignment (`CooperativeStickyAssignor`) and static group membership (`group.instance.id`) to eliminate stop-the-world partition revocations during rolling restarts.
2. Reduce `max.poll.records` so each batch completes well within 25% of `max.poll.interval.ms`.
3. Stabilize any crashing consumer pod before scaling the group back up, then verify partition assignments hold steady for 15 minutes.
