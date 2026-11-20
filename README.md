# SignalForge

SignalForge is a local-first AI-powered IT Operations Intelligence Platform (AIOps) built in Python.

**Current Status — Milestone 1 & Phase 2 (Foundation & Multi-Signal Telemetry Ingestion):**
- PostgreSQL 16 schema managed via Alembic (`services`, `events`, `incidents`, `deployments`, `ingestion_failures`, `metric_points`, `trace_spans`)
- Structured JSON logging and environment-based configuration
- Validation, UTC normalization, deterministic deduplication, and OTLP JSON parsing (`services/ingestion/`)
- FastAPI HTTP API:
  - `GET /health`
  - `POST /api/v1/events/ingest`, `GET /api/v1/events`
  - `POST /api/v1/metrics/ingest`, `POST /api/v1/metrics/ingest/csv`, `GET /api/v1/metrics`
  - `POST /api/v1/traces/ingest`, `GET /api/v1/traces`
  - `POST /api/v1/deployments/ingest`, `GET /api/v1/deployments`
  - `GET /api/v1/services`
  - `GET /api/v1/incidents`
- Offline OpenTelemetry Collector capture configuration and CLI loader (`scripts/ingest_otel_capture.py`)

## Prerequisites (Windows)
- Python 3.12 (or `uv`, which provisions Python 3.12 automatically)
- [`uv`](https://docs.astral.sh/uv/)
- Docker Desktop (Linux containers enabled)

## Clean-Checkout Setup

```powershell
# 1. Create a Python 3.12 virtual environment and install dependencies
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"

# 2. Configure local environment variables
copy .env.example .env

# 3. Start PostgreSQL 16 (creates both `signalforge` and `signalforge_test`)
docker compose up -d --wait

# 4. Apply Alembic migrations to the development database
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

## Linting & Tests

Tests run against the real PostgreSQL `signalforge_test` database using Alembic migrations:

```powershell
.venv\Scripts\ruff check .
.venv\Scripts\pytest -v
```
