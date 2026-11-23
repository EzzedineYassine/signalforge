# SignalForge HTTP API Reference

Base URL (local development): `http://127.0.0.1:8000`  
Interactive OpenAPI docs: `http://127.0.0.1:8000/docs`  
CORS: Configured explicitly via `CORS_ORIGINS` (default `http://localhost:5173,http://127.0.0.1:5173`).

All timestamp inputs **must include an explicit timezone offset** (e.g., `2026-11-20T14:00:00Z`) and are normalized to UTC; naive timestamps are rejected with HTTP `422`. Service names are normalized to lowercase kebab-case (`^[a-z0-9][a-z0-9-]{0,127}$`).

---

## 1. Health

### `GET /health`
Verifies API liveness and PostgreSQL connectivity (`SELECT 1`).
- **200 OK:** `{"status": "ok", "database": "connected"}`
- **503 Service Unavailable:** `{"status": "degraded", "database": "disconnected"}`

---

## 2. Services

### `GET /api/v1/services`
Returns registered and auto-discovered services ordered alphabetically by `name`, with aggregated event count and latest event timestamp.
- **Query params:** `limit` (`1..500`, default `50`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [{"id", "name", "environment", "owner_team", "is_auto_discovered", "event_count", "last_seen_at", "created_at"}]}`.

---

## 3. Events (Logs & Operational Events)

### `POST /api/v1/events/ingest`
Idempotently validates, normalizes, and persists a batch of operational events. Invalid records are isolated into `ingestion_failures` without aborting valid records.
- **Request body:** `{"events": [...]}` (`1..1000` records; required per record: `service`, `timestamp`, `message`; optional: `external_id`, `event_type`, `severity`, `trace_id`, `span_id`, `attributes`).
- **200 OK:** `{"batch_id", "received", "accepted", "duplicates", "rejected", "errors": [{"index", "error_code", "error_detail"}]}`.
- **422:** Empty or malformed envelope. **500:** Database error.

### `GET /api/v1/events`
Queries persisted events ordered by `timestamp_utc DESC, id DESC`.
- **Query params:** `service`, `start_time`, `end_time`, `severity` (`DEBUG|INFO|WARNING|ERROR|CRITICAL`), `event_type`, `trace_id`, `limit` (`1..500`, default `50`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [EventRead, ...]}`.
- **422:** Invalid filter syntax, naive timestamp, or `start_time > end_time`.

---

## 4. Metrics

### `POST /api/v1/metrics/ingest`
Idempotently ingests numeric metric points (deduplicated by SHA-256 over `service`, `metric_name`, UTC timestamp, and canonical sorted `labels`). Rejects `NaN`/`Infinity`.
- **Request body:** `{"metrics": [...]}` (`1..5000` records; required: `service`, `metric_name`, `timestamp`, `value`; optional: `unit`, `labels`).
- **200 OK:** `{"batch_id", "received", "accepted", "duplicates", "rejected", "errors": [...]}`.

### `POST /api/v1/metrics/ingest/csv`
Idempotently ingests UTF-8 CSV metric rows.
- **Request body:** Raw `text/csv` (UTF-8) with required header columns `service,metric_name,timestamp,value` and optional `unit,labels` (JSON object string). Max `5000` rows.
- **200 OK:** `MetricIngestResponse` (partial row-level failures returned in `errors`).
- **422:** Empty body, invalid UTF-8, missing required headers, or row limit exceeded.

### `GET /api/v1/metrics`
Queries metric points ordered chronologically (`timestamp_utc ASC, id ASC`).
- **Query params:** `service`, `metric_name`, `start_time`, `end_time`, `limit` (`1..5000`, default `100`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [MetricPointRead, ...]}`.

---

## 5. Distributed Trace Spans

### `POST /api/v1/traces/ingest`
Idempotently ingests trace spans (deduplicated on unique `(trace_id, span_id)`).
- **Request body:** `{"spans": [...]}` (`1..5000` records; required: `service`, `trace_id`, `span_id`, `operation_name`, `start_time`, `end_time`; optional: `parent_span_id`, `span_kind`, `status_code`, `duration_ms`, `attributes`).
- **200 OK:** `{"batch_id", "received", "accepted", "duplicates", "rejected", "errors": [...]}`.

### `GET /api/v1/traces`
Queries trace spans ordered by `start_time_utc DESC, id DESC`.
- **Query params:** `trace_id`, `service`, `status_code` (`UNSET|OK|ERROR`), `start_time`, `end_time`, `limit` (`1..1000`, default `100`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [TraceSpanRead, ...]}`.

---

## 6. Deployments

### `POST /api/v1/deployments/ingest`
Validates and persists deployment events.
- **Request body:** `{"deployments": [...]}` (`1..1000` records; required: `service`, `version`, `deployed_at`, `status` in `started|succeeded|failed|rolled_back`; optional: `commit_sha`, `deployed_by`, `metadata`).
- **200 OK:** `{"batch_id", "received", "accepted", "duplicates", "rejected", "errors": [...]}`.

### `GET /api/v1/deployments`
Queries deployments ordered by `deployed_at DESC, id DESC`.
- **Query params:** `service`, `status`, `start_time`, `end_time`, `limit` (`1..500`, default `50`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [DeploymentRead, ...]}`.

---

## 7. Incidents

### `GET /api/v1/incidents`
Queries persisted incidents ordered by `started_at DESC, id DESC`.
- **Query params:** `service`, `status`, `severity`, `start_time`, `end_time`, `limit` (`1..500`, default `50`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [IncidentRead, ...]}`.

---

## 8. Multivariate Anomaly Detection

### `POST /api/v1/anomalies/detect`
Pivots 7 aligned features (`cpu_utilization`, `memory_utilization`, `request_latency_ms`, `request_throughput`, `error_rate`, `db_connections`, `queue_depth`) from `metric_points`, scores them with a calibrated detector, and idempotently persists detected anomalies into `anomalies`.
- **Request body:** `{"service": str, "start_time": datetime, "end_time": datetime, "detector": "lstm_autoencoder" | "isolation_forest" | "rolling_zscore" (default "lstm_autoencoder"), "persist_all": bool (default false)}`.
- **200 OK:** `{"service", "detector", "points_evaluated", "anomalies_detected", "persisted", "duplicates", "threshold", "max_score", "items": [AnomalyRead, ...]}`.
- **404:** Service not found. **422:** Insufficient aligned 7-feature metric points or invalid time range.

### `GET /api/v1/anomalies`
Queries persisted anomaly rows ordered by `timestamp_utc DESC, id DESC`.
- **Query params:** `service`, `detector`, `is_anomaly` (`bool`), `start_time`, `end_time`, `limit` (`1..500`, default `50`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [AnomalyRead, ...]}`.

---

## 9. Hybrid Knowledge Base (Qdrant + BM25)

### `POST /api/v1/knowledge/index`
Loads Markdown documents safely inside the repository root, splits into section-aware deterministic UUID5 chunks, embeds with CPU `all-MiniLM-L6-v2`, and idempotently upserts into Qdrant + JSON-persisted BM25.
- **Request body:** `{"source_dir": "data/synthetic/knowledge_base", "max_chars": 1200 (100..8000), "overlap_chars": 150 (0..2000, < max_chars)}`.
- **200 OK:** `{"source_dir", "documents_indexed", "chunks_indexed", "collection", "embedding_model"}`.
- **404:** Source directory missing. **422:** Path traversal outside repo or invalid chunking/frontmatter. **503:** Qdrant unavailable.

### `POST /api/v1/knowledge/search`
Executes hybrid RRF, dense cosine, or BM25 lexical search over indexed chunks. Returns `[]` cleanly before any chunks are indexed.
- **Request body:** `{"query": str, "top_k": 5 (1..50), "mode": "hybrid" | "dense" | "bm25", "service": str | null, "doc_type": "runbook" | "incident" | "architecture" | "deployment" | null}`.
- **200 OK:** `{"query", "mode", "total", "results": [RetrievedChunkResponse, ...]}`.
- **422:** Empty query or invalid parameters. **503:** Qdrant unavailable.

---

## 10. Service Dependency Graph & Incident Correlation

### `GET /api/v1/graph/dependencies`
Builds the directed service dependency graph (`caller -> callee`) via a PostgreSQL self-join on parent/child `trace_spans`, excluding self-loops while preserving all registered service nodes.
- **Query params:** Optional `start_time`, `end_time`.
- **200 OK:** `{"node_count", "edge_count", "nodes": [...], "edges": [{"caller", "callee", "call_count", "error_count", "error_rate", "avg_duration_ms"}]}`.

### `GET /api/v1/graph/services/{service}/impact`
Runs cycle-safe multi-hop BFS traversal returning outgoing `dependencies` (what `service` calls) and incoming `affected_callers` (what calls `service`).
- **Path param:** `service`.
- **Query params:** `max_depth` (`1..10`, default `3`), optional `start_time`, `end_time`.
- **200 OK:** `{"target_service", "max_depth", "dependencies": [{"service", "hop_distance"}], "affected_callers": [{"service", "hop_distance"}], "all_related_services": [...]}`.
- **404:** Service not registered. **422:** Invalid service format or out-of-range params.

### `POST /api/v1/correlation/timeline`
Correlates deployments (`[start_time - lookback_minutes, end_time]`), positive anomalies, `WARNING`/`ERROR`/`CRITICAL` events, and incidents (`[start_time, end_time]`) across `{target_service} | related_services`.
- **Request body:** `{"service": str, "start_time": datetime, "end_time": datetime, "lookback_minutes": 60 (0..1440), "max_depth": 3 (1..10)}`.
- **200 OK:** `{"target_service", "window_start", "window_end", "lookback_minutes", "deployment_window_start", "max_depth", "related_services", "correlated_services", "impact", "deployment_count", "anomaly_count", "event_count", "incident_count", "timeline": [TimelineItemResponse, ...], "correlation_note"}`.
- **404:** Unknown service. **422:** Naive timestamps, inverted window, or invalid bounds.

---

## 11. Bounded LangGraph Investigations

### `POST /api/v1/investigations/run`
Executes the bounded LangGraph workflow (`plan_investigation` → `collect_evidence` → `abstain_no_evidence` OR `synthesize_report` → `validate_evidence`), persists the audited report in `investigations`, and returns it. Degrades gracefully to an abstained deterministic evidence-only report (HTTP `200`) if Ollama is unavailable.
- **Request body:** `{"question": str, "service": str, "start_time": datetime, "end_time": datetime, "lookback_minutes": 60 (0..1440), "max_depth": 3 (1..10), "top_k_docs": 5 (1..20)}`.
- **200 OK:** `{"id", "service_id", "service_name", "created_at", "executed_nodes", "duration_ms", "abstained", "model_name", "report": InvestigationReport}`.
- **404:** Unknown service. **422:** Blank question, naive timestamps, or inverted range.

### `GET /api/v1/investigations`
Lists persisted investigations ordered by `created_at DESC, id DESC`.
- **Query params:** `service`, `abstained` (`bool`), `limit` (`1..200`, default `50`), `offset` (`>= 0`, default `0`).
- **200 OK:** `{"total", "limit", "offset", "items": [InvestigationRead, ...]}`.

### `GET /api/v1/investigations/{investigation_id}`
Retrieves a single persisted investigation report by UUID.
- **200 OK:** `InvestigationRead`.
- **404:** Investigation UUID not found. **422:** Invalid UUID format.
