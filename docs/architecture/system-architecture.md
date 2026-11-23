# SignalForge System Architecture

SignalForge is a local-first AI-powered IT Operations Intelligence Platform (AIOps) engineered to correlate multi-signal operational telemetry, detect multivariate anomalies, retrieve grounded operational knowledge, and orchestrate bounded incident investigations on a single developer workstation (16 GB RAM / optional 8 GB VRAM).

## 1. Seven-Layer Modular Architecture

| Layer | Directory | Responsibility |
| --- | --- | --- |
| **1. Telemetry Ingestion & Normalization** | `services/ingestion/` | Validates, normalizes service names & UTC timestamps, computes deterministic SHA-256 deduplication hashes, parses OTLP JSON captures, and isolates invalid records into `ingestion_failures`. |
| **2. Unified Relational Storage** | `apps/api/src/models/`, `database/migrations/` | Single PostgreSQL 16 database managed via 4 reversible Alembic migrations (`0001`–`0004`) across 9 tables. |
| **3. Multivariate Anomaly Detection** | `services/anomaly_detection/` | Leakage-free chronological preprocessing, 7-feature sliding windows, `RollingZScoreDetector`, `IsolationForestDetector`, and CPU PyTorch `LSTMAutoencoderDetector` with JSON/state-dict serialization (zero `pickle`). |
| **4. Hybrid Knowledge RAG** | `services/retrieval/` | Section-aware Markdown chunker (deterministic UUID5 `chunk_id`s), CPU `all-MiniLM-L6-v2` embeddings, Qdrant cosine search, JSON-persisted `BM25Okapi` index, and Reciprocal Rank Fusion (RRF). |
| **5. Service Dependency Graph & Correlation** | `services/graph/` | Pure NetworkX directed graph (`caller -> callee`) derived via SQLAlchemy self-join on parent/child `trace_spans`, cycle-safe multi-hop BFS impact traversal, and chronological timeline correlation with explicit non-causation disclaimers. |
| **6. Bounded Investigation Orchestration** | `services/investigation/` | Acyclic LangGraph `StateGraph`, replaceable `LLMClient` (`OllamaClient` structured JSON output + `FakeLLMClient`), zero-evidence abstention short-circuit, deterministic citation hydration, and hallucination stripping. |
| **7. API & Engineering Console** | `apps/api/`, `apps/web/` | FastAPI REST API (`22` endpoints, explicit CORS) + 6-screen React 18 / TypeScript / Vite / Recharts console. |

### Architectural Boundary Enforcement

All pure domain packages (`services/anomaly_detection/`, `services/retrieval/`, `services/graph/`, `services/investigation/`, and `evaluation/`) are strictly decoupled from web frameworks, ORM layers, and unsafe binary serialization. Automated AST tests enforce zero imports of `fastapi`, `sqlalchemy`, `psycopg`, `alembic`, `starlette`, `pickle`, or `joblib` inside these packages.

---

## 2. End-to-End System Architecture

```mermaid
flowchart TB
    subgraph Sources["Telemetry & Knowledge Sources"]
        OTEL["OTLP JSON Captures / Sample Payloads"]
        DEMO["Synthetic Demo Seeder & Benchmark Generators"]
        KB["Markdown Knowledge Base (37 Synthetic Docs)"]
    end

    subgraph Ingestion["Layer 1: Ingestion & Validation"]
        NORM["Normalizer & Dedup Hash (UTC, kebab-case, SHA-256)"]
        CHUNK["Section-Aware Markdown Chunker (UUID5 chunks)"]
    end

    subgraph Storage["Layer 2 & 4: Local Datastores"]
        PG["PostgreSQL 16 (9 Alembic-Managed Tables)"]
        QD["Qdrant Vector DB (COSINE, 384-dim)"]
        BM25["BM25Okapi JSON Index"]
    end

    subgraph Intelligence["Layers 3, 4 & 5: Analytical Engines"]
        ML["Anomaly Detectors (Rolling Z-Score, Isolation Forest, CPU LSTM AE)"]
        RAG["HybridRetriever (Dense + BM25 + RRF k=60)"]
        GRAPH["NetworkX Dependency Graph & Timeline Correlator"]
    end

    subgraph Agent["Layer 6: Investigation Orchestration"]
        LG["Bounded LangGraph StateGraph"]
        OLLAMA["Local Ollama (/api/chat, JSON schema, num_ctx=8192)"]
        VAL["Deterministic Evidence & Citation Validator"]
    end

    subgraph Delivery["Layer 7: API & Console"]
        API["FastAPI HTTP API (localhost:8000)"]
        WEB["React + TypeScript + Recharts Console (localhost:5173)"]
    end

    OTEL --> NORM
    DEMO --> NORM
    KB --> CHUNK
    NORM --> PG
    CHUNK --> QD
    CHUNK --> BM25

    PG --> ML
    ML --> PG
    PG --> GRAPH
    QD --> RAG
    BM25 --> RAG

    GRAPH --> LG
    RAG --> LG
    LG --> OLLAMA
    OLLAMA --> VAL
    LG --> VAL
    VAL --> PG

    API --> PG
    API --> ML
    API --> RAG
    API --> GRAPH
    API --> LG
    WEB --> API
```

---

## 3. Bounded LangGraph Investigation Workflow

The investigation workflow in `services/investigation/workflow.py` is a strictly acyclic, bounded `StateGraph` (maximum 4 nodes executed on any request, zero open-ended tool loops):

```mermaid
flowchart LR
    START(["START"]) --> PLAN["plan_investigation (Normalize & validate inputs)"]
    PLAN --> COLLECT["collect_evidence (Correlated signals + dependency edges + Hybrid RAG)"]
    COLLECT --> ROUTE{"has_actionable_evidence?"}
    ROUTE -- "No (0 events/anomalies/deploys/incidents)" --> ABSTAIN["abstain_no_evidence (Skip LLM, deterministic abstention)"]
    ROUTE -- "Yes" --> SYNTH["synthesize_report (Structured JSON via LLMClient)"]
    SYNTH --> VALIDATE["validate_evidence (Hydrate citations, strip fake IDs, handle LLM error fallback)"]
    ABSTAIN --> END_NODE(["END"])
    VALIDATE --> END_NODE
```

**Safety guarantees enforced by the DAG:**
1. **Zero-evidence short-circuit:** When no actionable telemetry exists (`event`, `anomaly`, `deployment`, `incident`), the LLM is never called (`0` LLM calls) and an abstained report (`model_name="deterministic-abstention"`) is persisted.
2. **Deterministic citation hydration:** The LLM outputs only `cited_doc_ids`; `validate_evidence` hydrates `CitationRef` metadata (`title`, `section_heading`, `source_path`, `is_synthetic`) directly from `EvidenceBundle.retrieved_citations`, making fabricated titles or paths impossible.
3. **Evidence-backed hypotheses:** Fabricated `evidence_id`s are stripped with warnings, any hypothesis left with `0` valid `supporting_evidence_ids` is dropped, and every hypothesis enforces `is_proven_causation = False`.
4. **Graceful LLM failure fallback:** If Ollama is stopped, times out, or returns invalid JSON, `synthesize_report` catches `LLMClientError` and `validate_evidence` produces an HTTP `200` abstained report retaining all deterministic facts, anomalies, correlations, and retrieved citations.

---

## 4. PostgreSQL 16 Schema Overview (9 Tables)

All 9 tables are managed strictly through reversible Alembic migrations (`0001_initial_schema` → `0002_telemetry_signals` → `0003_anomalies` → `0004_investigations`):

```mermaid
erDiagram
    services {
        uuid id PK
        varchar name UK
        varchar environment
        varchar owner_team
        boolean is_auto_discovered
        timestamptz created_at
        timestamptz updated_at
    }
    events {
        uuid id PK
        uuid service_id FK
        varchar external_id
        varchar dedup_hash UK
        timestamptz timestamp_utc
        varchar event_type
        varchar severity
        text message
        varchar trace_id
        varchar span_id
        jsonb attributes
    }
    metric_points {
        uuid id PK
        uuid service_id FK
        varchar metric_name
        timestamptz timestamp_utc
        double value
        varchar unit
        jsonb labels
        varchar dedup_hash UK
    }
    trace_spans {
        uuid id PK
        uuid service_id FK
        varchar trace_id
        varchar span_id
        varchar parent_span_id
        varchar operation_name
        varchar span_kind
        varchar status_code
        timestamptz start_time_utc
        timestamptz end_time_utc
        double duration_ms
    }
    deployments {
        uuid id PK
        uuid service_id FK
        varchar version
        varchar commit_sha
        varchar status
        varchar deployed_by
        timestamptz deployed_at
        jsonb metadata_json
    }
    incidents {
        uuid id PK
        varchar external_id UK
        varchar title
        uuid service_id FK
        varchar severity
        varchar status
        timestamptz started_at
        timestamptz resolved_at
    }
    anomalies {
        uuid id PK
        uuid service_id FK
        varchar detector_name
        timestamptz timestamp_utc
        double score
        double threshold
        boolean is_anomaly
        jsonb metric_names
        jsonb details
        varchar dedup_hash UK
    }
    investigations {
        uuid id PK
        uuid service_id FK
        text question
        timestamptz window_start
        timestamptz window_end
        varchar model_name
        boolean abstained
        jsonb report_json
        jsonb executed_nodes
        double duration_ms
        timestamptz created_at
    }
    ingestion_failures {
        uuid id PK
        uuid batch_id
        int record_index
        varchar error_code
        text error_detail
        jsonb raw_payload
        timestamptz failed_at
    }

    services ||--o{ events : "emits"
    services ||--o{ metric_points : "reports"
    services ||--o{ trace_spans : "executes"
    services ||--o{ deployments : "receives"
    services ||--o{ incidents : "linked_to"
    services ||--o{ anomalies : "scored_on"
    services ||--o{ investigations : "analyzed_by"
```
