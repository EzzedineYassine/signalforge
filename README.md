# SignalForge

SignalForge is a local-first AI-powered IT Operations Intelligence Platform (AIOps) built in Python.

**Current Status — Phases 1–3 (Foundation, Multi-Signal Ingestion & Multivariate Anomaly Detection):**
- PostgreSQL 16 schema managed via Alembic (`services`, `events`, `incidents`, `deployments`, `ingestion_failures`, `metric_points`, `trace_spans`, `anomalies`)
- Structured JSON logging and environment-based configuration
- Validation, UTC normalization, deterministic deduplication, and OTLP JSON parsing (`services/ingestion/`)
- Multivariate anomaly detection (`services/anomaly_detection/`): leakage-free preprocessing, operational evaluation metrics, `RollingZScoreDetector`, `IsolationForestDetector`, and PyTorch CPU `LSTMAutoencoderDetector`
- FastAPI HTTP API:
  - `GET /health`
  - `POST /api/v1/events/ingest`, `GET /api/v1/events`
  - `POST /api/v1/metrics/ingest`, `POST /api/v1/metrics/ingest/csv`, `GET /api/v1/metrics`
  - `POST /api/v1/traces/ingest`, `GET /api/v1/traces`
  - `POST /api/v1/deployments/ingest`, `GET /api/v1/deployments`
  - `GET /api/v1/services`
  - `GET /api/v1/incidents`
  - `POST /api/v1/anomalies/detect`, `GET /api/v1/anomalies`
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

# 5. Start PostgreSQL 16 (creates both `signalforge` and `signalforge_test`)
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

## Linting & Tests

Tests run against the real PostgreSQL `signalforge_test` database using Alembic migrations:

```powershell
.venv\Scripts\ruff check .
.venv\Scripts\pytest -v
```
