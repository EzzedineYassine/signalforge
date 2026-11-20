# ADR-001: Single PostgreSQL Telemetry Store and Offline OpenTelemetry Capture

- **Status:** Accepted
- **Date:** 2026-11-20

## Context
SignalForge is a local-first AIOps platform developed and executed primarily on a single 16 GB RAM developer laptop. In Phase 2, SignalForge expands beyond log/operational events to ingest numeric time-series metrics and distributed trace spans alongside services, deployments, and incidents.

We need two foundational architectural decisions:
1. Where to store logs/events, metrics, traces, deployments, and incidents so cross-signal queries and reproducible tests remain fast and simple.
2. How to source realistic multi-service telemetry without exceeding local laptop memory limits during day-to-day development.

## Decision
1. **Unified PostgreSQL 16 storage:** Store all operational signals (`events`, `metric_points`, `trace_spans`, `services`, `deployments`, `incidents`, and `ingestion_failures`) in a single local PostgreSQL 16 instance, using native `UUID`, UTC `TIMESTAMPTZ`, `DOUBLE PRECISION`, `JSONB`, B-tree composite time-descending indexes, and Alembic-managed migrations.
2. **Offline minimal-mode OpenTelemetry Demo capture:** Capture real microservice telemetry from the OpenTelemetry Demo in short, dedicated minimal-mode Docker sessions (~3 GB RAM) and persist captured snapshots to disk. Stop the demo stack after capture and ingest the snapshots into SignalForge's ingestion pipeline for reproducible offline analysis and testing.

## Alternatives
- **Dedicated observability stack (Prometheus + Jaeger/Tempo + Loki + TimescaleDB):** Mirrors large-scale production topologies, but running multiple specialized storage engines continuously alongside Docker Desktop, PostgreSQL, and future local AI/analytics workloads would consume excessive RAM and operational overhead on a 16 GB machine, while complicating transactional joins across signals.
- **Always-on full OpenTelemetry Demo stack:** The full demo footprint (~6–8+ GB RAM) leaves insufficient headroom on a 16 GB Windows host for IDE, PostgreSQL, tests, and local analysis, and introduces non-deterministic live-traffic noise across test runs.
- **Purely synthetic telemetry only:** Lightweight, but misses realistic microservice trace hierarchies, attribute schemas, and failure propagation patterns present in real OTel instrumentation.

## Consequences
### Positive
- Minimal baseline runtime footprint: a single `postgres:16-alpine` container powers all five operational signal types and all integration tests.
- Strong referential integrity and single-database SQL joins across services, events, metrics, traces, deployments, and incidents.
- Deterministic, repeatable offline replay of real OpenTelemetry Demo captures without keeping the demo containers running.
- Schema evolution remains unified under Alembic with deterministic constraints and reversibility tests.

### Negative / Trade-offs
- Plain PostgreSQL is not tuned for high-cardinality, millions-of-points-per-second production metric ingestion; retention policies, table partitioning, or specialized TSDB/trace backends would be needed for large-scale production deployments.
- Offline snapshot capture requires explicit capture sessions to refresh telemetry datasets rather than streaming live demo traffic continuously.
