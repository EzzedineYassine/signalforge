# SignalForge

SignalForge is a local-first AI-powered IT Operations Intelligence Platform (AIOps) built in Python.

**Current Status — Phases 1–7 (Full Backend, ML Anomaly Detection, Hybrid RAG, Dependency Graph, LangGraph Investigations & React Console):**
- PostgreSQL 16 schema managed via Alembic (`services`, `events`, `incidents`, `deployments`, `ingestion_failures`, `metric_points`, `trace_spans`, `anomalies`, `investigations`)
- Qdrant vector store (`localhost:6333`) + JSON-persisted BM25 lexical index for hybrid operations knowledge retrieval
- Structured JSON logging and environment-based configuration (including local Ollama & explicit CORS settings)
- Validation, UTC normalization, deterministic deduplication, and OTLP JSON parsing (`services/ingestion/`)
- Multivariate anomaly detection (`services/anomaly_detection/`): leakage-free preprocessing, operational evaluation metrics, `RollingZScoreDetector`, `IsolationForestDetector`, and PyTorch CPU `LSTMAutoencoderDetector`
- Hybrid knowledge retrieval & evaluation (`services/retrieval/`, `evaluation/retrieval/`): section-aware Markdown chunking, CPU `all-MiniLM-L6-v2` embeddings, Qdrant cosine search, BM25Okapi keyword search, Reciprocal Rank Fusion (RRF), and reproducible retrieval benchmark
- Service dependency graph & multi-signal incident correlation (`services/graph/`): NetworkX directed topology derived from parent/child `trace_spans`, cycle-safe multi-hop impact traversal, and chronological deployment/anomaly/event/incident timeline correlation
- Bounded LangGraph incident investigation workflow (`services/investigation/`): replaceable Ollama structured-output client (`num_ctx=8192`), deterministic evidence collection + hybrid RAG retrieval, zero-evidence abstention short-circuit, deterministic citation/evidence validation, graceful LLM-failure fallback, and PostgreSQL persistence
- React + TypeScript + Vite + Recharts engineering console (`apps/web/`): Operations Overview, Services & Dependencies, Telemetry & Anomalies, Incident Timeline, Knowledge Explorer, and AI Investigation Workspace
- FastAPI HTTP API:
  - `GET /health`
  - `POST /api/v1/events/ingest`, `GET /api/v1/events`
  - `POST /api/v1/metrics/ingest`, `POST /api/v1/metrics/ingest/csv`, `GET /api/v1/metrics`
  - `POST /api/v1/traces/ingest`, `GET /api/v1/traces`
  - `POST /api/v1/deployments/ingest`, `GET /api/v1/deployments`
  - `GET /api/v1/services`
  - `GET /api/v1/incidents`
  - `POST /api/v1/anomalies/detect`, `GET /api/v1/anomalies`
  - `POST /api/v1/knowledge/index`, `POST /api/v1/knowledge/search`
  - `GET /api/v1/graph/dependencies`, `GET /api/v1/graph/services/{service}/impact`, `POST /api/v1/correlation/timeline`
  - `POST /api/v1/investigations/run`, `GET /api/v1/investigations`, `GET /api/v1/investigations/{investigation_id}`
- Offline OpenTelemetry Collector capture configuration and CLI loader (`scripts/ingest_otel_capture.py`)

## Prerequisites (Windows)
- Python 3.12 (or `uv`, which provisions Python 3.12 automatically)
- [`uv`](https://docs.astral.sh/uv/)
- Docker Desktop (Linux containers enabled)

## Clean-Checkout Setup

```powershell
# 1. Create a Python 3.12 virtual environment
uv venv --python 3.12 .venv

# 2. Install CPU-only PyTorch first (avoids pulling the 2.5 GB CUDA wheel)
uv pip install --python .venv\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu "torch>=2.3,<3.0"

# 3. Install SignalForge and remaining dependencies
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"

# 4. Configure local environment variables
copy .env.example .env

# 5. Start PostgreSQL 16 (creates `signalforge` and `signalforge_test`) and Qdrant
docker compose up -d --wait

# 6. Apply Alembic migrations to the development database
.venv\Scripts\alembic upgrade head
```

## Running the API

```powershell
.venv\Scripts\uvicorn apps.api.src.main:app --reload
```

Verify health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## Sample Event Ingestion & Query Verification

Ingest `data/samples/sample_events.json` twice to verify idempotent deduplication:

```powershell
# First call: accepts 7 events, 0 duplicates
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/events/ingest `
  -ContentType "application/json" `
  -InFile data\samples\sample_events.json

# Second call: accepts 0 events, 7 duplicates
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/events/ingest `
  -ContentType "application/json" `
  -InFile data\samples\sample_events.json
```

Query ingested data:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/services"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/events?service=payment-service&severity=ERROR"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/events?trace_id=trace-9f8e7d6c5b4a"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/incidents"
```

## Phase 2: Multi-Signal Ingestion & OpenTelemetry OTLP Capture

Load the bundled OTLP sample fixtures (`data/samples/otel/`) idempotently into PostgreSQL:

```powershell
# Run 1: ingests sample traces, metrics, and logs
.venv\Scripts\python scripts/ingest_otel_capture.py --input-dir data/samples/otel

# Run 2: reports 0 accepted and 100% duplicates
.venv\Scripts\python scripts/ingest_otel_capture.py --input-dir data/samples/otel
```

Query captured signals via the API:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/traces?trace_id=4bf92f3577b34da6a3ce929d0e0e4736"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/metrics?service=payment-service"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/deployments"
```

For instructions on capturing live snapshots from `opentelemetry-demo` in minimal mode, see [docs/datasets/otel-capture.md](docs/datasets/otel-capture.md).

## Phase 3: Multivariate Anomaly Detection

Run the reproducible 3-way synthetic benchmark (`seed=42`, 7 days, 1-minute resolution, 60% normal train / 20% validation calibration / 20% held-out test with 5 unseen faults):

```powershell
.venv\Scripts\python scripts/run_anomaly_benchmark.py
```

Measured held-out test-split results on the synthetic benchmark (see [docs/experiments/anomaly_detection_benchmark.md](docs/experiments/anomaly_detection_benchmark.md) and [results.json](experiments/benchmarks/anomaly_seed42/results.json)):

| Detector | Precision | Recall | F1 | PR-AUC | Faults | Mean Delay (min) | FP / Hour | Fit (s) |
| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| `RollingZScoreDetector` (`w=30`) | 0.4502 | 0.9131 | 0.6031 | 0.5176 | 5 / 5 | 0.0 | 52.4602 | 0.0007 |
| `IsolationForestDetector` (`n=200`) | 0.9986 | 0.8318 | 0.9076 | 0.9948 | 5 / 5 | 9.8 | 0.0531 | 0.6658 |
| `LSTMAutoencoderDetector` (`h=32, z=16, e=25`) | 1.0000 | 0.8307 | 0.9075 | 0.9929 | 5 / 5 | 29.6 | 0.0000 | 149.3922 |

Run detection over ingested 7-feature `metric_points` and query persisted anomalies:

```powershell
# Run detection (supports lstm_autoencoder, rolling_zscore, isolation_forest)
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/anomalies/detect" `
  -ContentType "application/json" `
  -Body '{"service":"checkout-service","start_time":"2026-11-20T12:00:00Z","end_time":"2026-11-20T13:00:00Z","detector":"lstm_autoencoder"}'

# Query persisted anomalies
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/anomalies?service=checkout-service&is_anomaly=true"
```

## Phase 4: Knowledge Intelligence (Hybrid Qdrant + BM25 RAG)

Index the synthetic operations knowledge base (`data/synthetic/knowledge_base/`, 37 Markdown docs / 143 chunks) idempotently into Qdrant + BM25 and search in `"hybrid"`, `"dense"`, or `"bm25"` mode:

```powershell
# Index synthetic runbooks, postmortems, architecture docs, and deployment reports
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/knowledge/index" `
  -ContentType "application/json" `
  -Body '{"source_dir":"data/synthetic/knowledge_base","max_chars":1200,"overlap_chars":150}'

# Hybrid RRF search with optional service and doc_type filters
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/knowledge/search" `
  -ContentType "application/json" `
  -Body '{"query":"database connection pool exhausted postgres","top_k":5,"mode":"hybrid","service":"postgres-primary"}'
```

Run the reproducible BM25 vs. Dense vs. Hybrid retrieval benchmark (`24` curated queries, isolated temporary Qdrant collection `signalforge_knowledge_bench` deleted on completion):

```powershell
.venv\Scripts\python scripts/run_retrieval_benchmark.py
```

Measured results on the 37-document synthetic knowledge base (`all-MiniLM-L6-v2` on CPU, `top_k=10`, document-level deduplicated metrics; see [docs/experiments/retrieval_benchmark.md](docs/experiments/retrieval_benchmark.md) and [results.json](experiments/benchmarks/retrieval_kb/results.json)):

| Mode | Recall@5 | MRR@10 | nDCG@10 | Citation Completeness | p50 (ms) | p95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25` | 0.9861 | 0.9514 | 0.9384 | 1.0000 | 3.15 | 7.57 |
| `dense` (`all-MiniLM-L6-v2`) | 0.9653 | 0.9792 | 0.9540 | 1.0000 | 52.63 | 123.31 |
| `hybrid` (RRF, `k=60`) | 0.9861 | 0.9792 | 0.9673 | 1.0000 | 53.66 | 70.02 |

## Phase 5: Service Dependency Graph & Incident Correlation

Derive the directed service dependency graph (`caller -> callee`) from parent/child `trace_spans`, traverse multi-hop dependencies and affected callers, and correlate deployments (with lookback window), anomalies, error/warning events, and incidents into a single chronological timeline:

```powershell
# Inspect aggregated service dependency graph (optional start_time / end_time)
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/graph/dependencies"

# Compute multi-hop dependencies and affected callers around a service
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/graph/services/checkout-service/impact?max_depth=3"

# Build a correlated timeline across related services (deployments include lookback_minutes)
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/correlation/timeline" `
  -ContentType "application/json" `
  -Body '{"service":"checkout-service","start_time":"2026-11-20T14:00:00Z","end_time":"2026-11-20T14:30:00Z","lookback_minutes":60,"max_depth":3}'
```

## Phase 6: Bounded LangGraph Incident Investigation Orchestration

Run an evidence-grounded incident investigation (collecting correlated telemetry, retrieving hybrid RAG runbooks/postmortems, synthesizing structured JSON via local Ollama with deterministic citation/evidence validation and automatic abstention on empty evidence or LLM unavailability), and query persisted audit reports:

```powershell
# Run and persist a bounded LangGraph investigation
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/investigations/run" `
  -ContentType "application/json" `
  -Body '{"question":"Why did checkout-service 5xx errors spike?","service":"checkout-service","start_time":"2026-11-20T14:00:00Z","end_time":"2026-11-20T14:30:00Z","lookback_minutes":60,"max_depth":3,"top_k_docs":5}'

# List persisted investigations (supports service, abstained, limit, offset)
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/investigations?service=checkout-service"

# Fetch a single persisted investigation by UUID
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/investigations/<investigation_id>"
```

## Phase 7: React + TypeScript Operations Console & Demo Scenario

Start the FastAPI backend (`http://127.0.0.1:8000`), optionally seed the idempotent **synthetic** e-commerce incident demo scenario (traces, metrics, LSTM anomalies, deployment, error events, open incident, and Qdrant KB index), and launch the Vite frontend (`http://127.0.0.1:5173`):

```powershell
# 1. Seed the idempotent synthetic demo scenario into PostgreSQL + Qdrant
.venv\Scripts\python scripts/seed_demo_scenario.py

# 2. Start the backend API (in terminal 1)
.venv\Scripts\uvicorn apps.api.src.main:app --reload

# 3. Install and start the React + TypeScript + Recharts console (in terminal 2)
cd apps/web
npm install
npm run dev
```

Frontend verification commands (inside `apps/web/`):

```powershell
npx tsc --noEmit
npm run build
```

## Linting & Tests

Tests run against the real PostgreSQL `signalforge_test` database (via Alembic migrations) and an isolated Qdrant test collection (`signalforge_knowledge_test`, cleaned up on teardown):

```powershell
.venv\Scripts\ruff check .
.venv\Scripts\pytest -v
```
