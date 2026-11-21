---
doc_id: rb-005
doc_type: runbook
title: East-West Network Latency and Jitter Degradation
service: api-gateway
tags: [network_latency, request_latency_ms, queue_depth, api-gateway, dns]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# East-West Network Latency and Jitter Degradation

## Symptoms and Detection Signals

Network latency degradation produces a sharp step increase and high variance in `request_latency_ms` (+250 to +500 ms) and rising inflight `queue_depth` on `api-gateway`, while `cpu_utilization` and `memory_utilization` on downstream services remain near normal. Traces show inflated duration between `CLIENT` span start on `api-gateway` and `SERVER` span start on `checkout-service` or `order-service`.

## Diagnostic Procedure

1. Inspect trace parent-child timestamp deltas to isolate network transit delay from application processing time.
2. Check packet retransmissions, socket drops, and DNS lookup latency on gateway nodes:

```bash
netstat -s | grep -E "retransmitted|segments retransmited|SYNs to LISTEN"
```

3. Verify whether degradation is localized to a single availability zone (`eu-west-1a` vs `eu-west-1b`) or affecting all zones uniformly.
4. Check `api-gateway` upstream keep-alive pool exhaustion (excessive TCP handshake churn mimics network latency).

## Mitigation and Remediation

1. Drain traffic out of any impaired availability zone via load-balancer zone shift.
2. Enable HTTP/1.1 or HTTP/2 upstream keep-alive connection reuse if TCP SYN rates spiked.
3. Temporarily raise `api-gateway` non-critical read timeouts by 500 ms only if downstream queues are stable; keep `payment-service` timeouts strict to prevent thread exhaustion.
4. Verify p95 `request_latency_ms` returns under `120 ms` and TCP retransmission rate drops below `0.1%`.
