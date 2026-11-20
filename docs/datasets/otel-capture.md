# OpenTelemetry Demo Offline Capture Guide

As documented in [ADR-001](../decisions/ADR-001-telemetry-storage-and-capture.md), SignalForge captures real OpenTelemetry microservice telemetry in short, dedicated minimal-mode sessions (~3 GB RAM on a 16 GB Windows laptop) and ingests the resulting OTLP JSONL files offline.

> **Note on bundled sample fixtures:** `data/samples/otel/{traces,metrics,logs}.jsonl` are lightweight synthetic sample fixtures modeling a `frontend -> checkout-service -> payment-service` flow so you can test the parser and ingestion CLI immediately without starting the OpenTelemetry Demo.

## 1. Prepare a Local Capture Directory

Create an output directory (ignored or kept local under `data/captures/`):

```powershell
New-Item -ItemType Directory -Force data\captures\otel-demo | Out-Null
```

## 2. Run the Capture OpenTelemetry Collector

Start a standalone `otel/opentelemetry-collector-contrib` container using `infrastructure/otel/otel-collector-capture.yaml`, mounting your local capture folder to `/var/otel-capture`:

```powershell
docker run --rm -d `
  --name signalforge-otel-capture `
  -p 4317:4317 `
  -p 4318:4318 `
  -v "${PWD}\infrastructure\otel\otel-collector-capture.yaml:/etc/otelcol-contrib/config.yaml:ro" `
  -v "${PWD}\data\captures\otel-demo:/var/otel-capture" `
  otel/opentelemetry-collector-contrib:latest
```

The collector accepts OTLP gRPC (`:4317`) and OTLP HTTP (`:4318`) and appends OTLP JSON lines to:
- `data/captures/otel-demo/traces.jsonl`
- `data/captures/otel-demo/metrics.jsonl`
- `data/captures/otel-demo/logs.jsonl`

## 3. Run OpenTelemetry Demo in Minimal Mode (2–3 Minutes)

In a separate clone of [`open-telemetry/opentelemetry-demo`](https://github.com/open-telemetry/opentelemetry-demo), start only the minimal microservice subset without its bundled observability UI/databases (`make start-minimal-no-o11y` or a minimal Docker Compose override) and point its `OTEL_EXPORTER_OTLP_ENDPOINT` to `http://host.docker.internal:4318` (or `:4317` for gRPC):

```powershell
# Example inside your opentelemetry-demo checkout
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://host.docker.internal:4318"
make start-minimal-no-o11y
```

Let load generation run for 2–3 minutes (optionally toggling a demo feature flag such as payment failure to capture error spans and logs).

## 4. Stop Demo & Collector to Reclaim RAM

Once capture files are populated in `data/captures/otel-demo/`:

```powershell
# Stop opentelemetry-demo from its checkout directory
docker compose down

# Stop the SignalForge capture collector
docker stop signalforge-otel-capture
```

## 5. Ingest Captured Snapshots into SignalForge

Ensure SignalForge's PostgreSQL container is running (`docker compose up -d --wait`) and migrations are applied (`.venv\Scripts\alembic upgrade head`), then run the CLI loader:

```powershell
.venv\Scripts\python scripts/ingest_otel_capture.py --input-dir data/captures/otel-demo
```

To try with the bundled sample fixtures first:

```powershell
.venv\Scripts\python scripts/ingest_otel_capture.py --input-dir data/samples/otel
```

Re-running the command against the same directory is strictly idempotent (`accepted: 0`, `duplicates: received`).
