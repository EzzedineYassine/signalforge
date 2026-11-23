# SignalForge — Engineering Case Study

**Author:** Yassine Ezzedine  
**Domain:** Local-First AIOps, Multivariate Time-Series Anomaly Detection, Hybrid RAG, Dependency Graph Correlation, and Evidence-Grounded Agentic Investigation  
**Stack:** Python 3.12 · FastAPI · PostgreSQL 16 (SQLAlchemy 2.0 + Alembic) · PyTorch (CPU) · scikit-learn · Qdrant · Sentence-Transformers · BM25 · NetworkX · LangGraph · Ollama · React 18 · TypeScript · Vite · Recharts

---

## 1. Problem Statement

Modern microservice incidents rarely present as a single isolated log line. An elevated HTTP 5xx rate on `checkout-service` may stem from a `payment-service` deployment rolled out 25 minutes earlier that saturated `postgres-primary` connection pools — producing correlated symptoms across metrics, traces, logs, and deployments simultaneously.

Generic "chat with your runbooks" prototypes fail operational incident triage for four reasons:
1. **No live signal correlation:** Searching static Markdown alone ignores active metric anomalies, recent deployments, and upstream/downstream blast radius.
2. **Conflation of correlation with causation:** Naive LLM summaries present temporal proximity as proven root cause.
3. **Fabricated citations and silent guessing:** Without programmatic guardrails, small local LLMs invent runbook IDs, fabricate metric values, or hallucinate root causes when telemetry is missing.
4. **Cloud lock-in and resource bloat:** Multi-database observability stacks plus cloud APIs are hard to reproduce cleanly on a single developer machine.

**SignalForge** solves this by treating the LLM strictly as a bounded, structured synthesizer over **deterministic, pre-verified operational evidence** — never as an unconstrained oracle.

---

## 2. System Design & Key Engineering Decisions

SignalForge is organized into 7 cleanly separated layers with AST-enforced import boundaries (`services/` and `evaluation/` contain zero `fastapi`, `sqlalchemy`, `pickle`, or `joblib` imports):

1. **Unified PostgreSQL 16 Operational Store ([ADR-001](../decisions/ADR-001-telemetry-storage-and-capture.md)):**
   Stores `services`, `events`, `metric_points`, `trace_spans`, `deployments`, `incidents`, `anomalies`, `investigations`, and `ingestion_failures` in one Alembic-managed database (`0001`–`0004`) with deterministic SHA-256 deduplication and partial-batch failure isolation.
2. **Leakage-Free Multivariate Anomaly Detection:**
   Uses strict chronological `60% train (normal-only) / 20% validation (threshold calibration) / 20% held-out test (5 unseen faults)` splits, causal sliding windows with zero future lookahead, and 3 comparable detectors (`RollingZScoreDetector`, `IsolationForestDetector`, CPU PyTorch `LSTMAutoencoderDetector`).
3. **Hybrid Knowledge Retrieval (Qdrant + BM25 + RRF):**
   Section-aware Markdown chunker generating deterministic UUID5 `chunk_id`s, paired with CPU `all-MiniLM-L6-v2` embeddings in Qdrant and pure-JSON `BM25Okapi` fused via Reciprocal Rank Fusion (`rrf_k=60`) so both exact operational error codes (`SQLSTATE 40P01`, `v2.4.0`) and natural-language symptoms retrieve accurately.
4. **Trace-Derived NetworkX Topology & Correlation Engine:**
   Derives directed `caller -> callee` edges via a PostgreSQL SQLAlchemy 2.0 self-join on parent/child `trace_spans`, computes cycle-safe multi-hop BFS impact (`dependencies` and `affected_callers`), and correlates deployments (with configurable lookback window) alongside anomalies, error/warning events, and incidents with an explicit non-causation disclaimer.
5. **Bounded LangGraph Workflow & Deterministic Validator ([ADR-002](../decisions/ADR-002-local-ml-and-llm-strategy.md)):**
   Executes an acyclic `StateGraph` (`plan_investigation` → `collect_evidence` → `abstain_no_evidence` OR `synthesize_report` → `validate_evidence`). Enforces zero-evidence short-circuiting (`0` LLM calls), deterministic `CitationRef` metadata hydration from retrieved context, stripping of fabricated `doc_id`s/`evidence_id`s, dropping of unsupported hypotheses, `is_proven_causation=False`, and graceful fallback when Ollama is offline.
6. **Evidence-First React + TypeScript + Recharts Console:**
   6 live views (Operations Overview, Services & Dependencies, Telemetry & Anomalies, Incident Timeline, Knowledge Explorer, and AI Investigation Workspace) driven strictly by live FastAPI endpoints with explicit `SYNTHETIC` badges, executed node trails, abstention banners, and validation warning callouts.

---

## 3. Actual Measured Results

> **Transparency note:** All benchmark datasets and knowledge documents below are explicitly labeled **synthetic** (`is_synthetic: true`). The end-to-end investigation benchmark was executed in `--mode deterministic` (live NetworkX, live Qdrant + `all-MiniLM-L6-v2` Hybrid RAG, real LangGraph workflow, and deterministic validator guardrails using a prompt-grounded double) because local Ollama was offline during capture. Live Ollama reasoning quality has not yet been benchmarked and can be measured anytime with `scripts/run_e2e_evaluation.py --mode ollama`.

### 3.1 Multivariate Anomaly Detection (Held-Out Test Split, Synthetic `seed=42`, 5 Unseen Faults)
Source: [anomaly_detection_benchmark.md](../experiments/anomaly_detection_benchmark.md) · [results.json](../../experiments/benchmarks/anomaly_seed42/results.json)

| Detector | Precision | Recall | F1 | PR-AUC | Faults | Mean Delay (min) | FP / Hour | Fit (s) |
| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| `RollingZScoreDetector` (`w=30`) | 0.4502 | 0.9131 | 0.6031 | 0.5176 | 5 / 5 | 0.0 | 52.4602 | 0.0007 |
| `IsolationForestDetector` (`n=200`) | 0.9986 | 0.8318 | **0.9076** | **0.9948** | 5 / 5 | 9.8 | 0.0531 | 0.6658 |
| `LSTMAutoencoderDetector` (`h=32, z=16, e=25`) | **1.0000** | 0.8307 | 0.9075 | 0.9929 | 5 / 5 | 29.6 | **0.0000** | 149.3922 |

*Key insight:* Comparing baselines revealed that `IsolationForestDetector` matched LSTM F1 while training ~224x faster on CPU and catching gradual `memory_leak` ramps 100 minutes earlier (`48.0` vs `148.0` min), whereas `LSTMAutoencoderDetector` achieved zero false positives (`1.0000` precision) across `18.83` held-out normal hours.

### 3.2 Hybrid Knowledge Retrieval (24 Curated Queries, 37 Synthetic Docs / 143 Chunks, Live Qdrant + CPU)
Source: [retrieval_benchmark.md](../experiments/retrieval_benchmark.md) · [results.json](../../experiments/benchmarks/retrieval_kb/results.json)

| Mode | Recall@5 | MRR@10 | nDCG@10 | Citation Completeness | p50 (ms) | p95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25` | 0.9861 | 0.9514 | 0.9384 | 1.0000 | 3.15 | 7.57 |
| `dense` (`all-MiniLM-L6-v2`) | 0.9653 | 0.9792 | 0.9540 | 1.0000 | 52.63 | 123.31 |
| `hybrid` (RRF, `k=60`) | **0.9861** | **0.9792** | **0.9673** | **1.0000** | 53.66 | 70.02 |

*Key insight:* Hybrid RRF matched BM25's exact `1.0000` score on `exact_lookup` queries (SQLSTATEs, config keys, release tags), surpassed both standalone modes on natural-language `conceptual_symptom` queries (`0.9539` nDCG@10 vs `0.9438` dense and `0.8510` BM25), and added only `~1 ms` median latency over dense search.

### 3.3 End-to-End Pipeline, Guardrails & Local System Performance
Source: [end_to_end_evaluation.md](../experiments/end_to_end_evaluation.md) · [e2e_results.json](../../experiments/benchmarks/end_to_end/e2e_results.json) · [system_perf.json](../../experiments/benchmarks/end_to_end/system_perf.json)

- **Pipeline & Guardrail Verification (`--mode deterministic`, 8 synthetic scenarios + adversarial probe):**
  - Affected Service F1: `1.0000` (`8/8`) · Live KB Expected Doc Recall (`top_k=5` chunks): `0.8095`
  - Abstention Accuracy: `1.0000` (`8/8`, `0` LLM calls on empty window)
  - Adversarial Hallucination Probe: `7 / 7` injected faults intercepted (`1.0000` interception rate; `2/2` fake `doc_id`s stripped, `3/3` fake `evidence_id`s stripped, `1/1` unsupported hypothesis dropped, post-validation unsupported rate `0.0000`, citation correctness `1.0000`).
- **Local System Performance (`Windows 11`, Python `3.12.14`, `16` logical CPUs, live PostgreSQL 16 + Qdrant):**
  - Batch Ingestion: `1,002.40 events/s` (`1,000` events) · `1,880.26 metrics/s` (`2,000` points)
  - API Latency (`p50 / p95`): `GET /health` `7.17 / 10.53 ms`, `GET /api/v1/events` `13.98 / 17.24 ms`, `GET /api/v1/graph/dependencies` `15.01 / 18.39 ms`, `POST /api/v1/anomalies/detect` (`lstm_autoencoder`, 46 pts) `38.97 / 41.71 ms`, `POST /api/v1/correlation/timeline` `37.95 / 61.68 ms`
  - Deterministic Investigation Workflow (`POST /api/v1/investigations/run`): `p50 = 92.09 ms`, `p95 = 136.54 ms`
  - Process RSS Memory: `435.91 MB` → `467.32 MB`
- **Automated Verification Suite:** `260` Pytest tests passing against real PostgreSQL 16 + Qdrant, `0` Ruff lint errors, `0` TypeScript/Vite build warnings.

---

## 4. Honest Limitations & Future Work

1. **Synthetic Evaluation Corpus:** All benchmarks use synthetic telemetry and a 37-document synthetic knowledge base. Capturing and evaluating against multi-hour OpenTelemetry Demo failure injections (using our bundled `infrastructure/otel/collector-capture-config.yaml` and `scripts/ingest_otel_capture.py`) is the natural next step.
2. **Live Local LLM Benchmark Pending:** Only `--mode deterministic` pipeline/guardrail evaluation has been recorded so far; benchmarking live `qwen2.5:7b` (and comparing against larger quantized local models) with `--mode ollama` will measure true semantic synthesis accuracy and token latency.
3. **Single-Node Storage Scale:** Plain PostgreSQL 16 handles local/team-scale telemetry comfortably (`~1k–1.9k rows/s`), but high-volume production ingestion would benefit from TimescaleDB hypertables/partitioning and async background workers for batch anomaly scoring.
4. **Gradual Drift & Retrieval Deduplication:** Aggregating LSTM reconstruction error via top-k per-feature error (for faster single-feature `memory_leak` detection) and deduplicating chunk candidates by `doc_id` before truncating `top_k_docs` (to lift `0.8095` multi-doc workflow KB recall) are concrete, low-cost algorithmic upgrades.

---

## 5. Five-Minute Live Demo Walkthrough

1. **Start infrastructure & seed synthetic scenario:**
   ```powershell
   docker compose up -d --wait
   .venv\Scripts\alembic upgrade head
   .venv\Scripts\python scripts/seed_demo_scenario.py
   ```
2. **Launch API & React console:**
   ```powershell
   # Terminal 1
   .venv\Scripts\uvicorn apps.api.src.main:app --reload
   # Terminal 2
   cd apps/web && npm run dev
   ```
3. **Explore all 6 screens at `http://localhost:5173`:**
   - **Operations Overview (`/`):** Confirm live green API/DB health pill, 4 registered services, 5 events, 20 LSTM anomalies, and 1 open critical incident.
   - **Services & Dependencies (`/services`):** Inspect `api-gateway -> checkout-service -> payment-service -> postgres-primary` edges with live error rates and click `checkout-service` to traverse multi-hop callers and dependencies.
   - **Telemetry & Anomalies (`/telemetry`):** View `checkout-service` 45-minute metric series with red LSTM anomaly markers and re-run `isolation_forest` or `rolling_zscore` on demand.
   - **Incident Timeline (`/timeline`):** Correlate the open incident (`2026-11-20T14:30:00Z` → `14:45:00Z`) to see the `payment-service v2.4.1` deployment (`-25.0m` pre-window) ordered chronologically before LSTM anomalies and `SQLSTATE 53300` Postgres errors under the non-causation disclaimer.
   - **Knowledge Explorer (`/knowledge`):** Search `"SQLSTATE 53300 connection pool exhausted"` across `hybrid`, `dense`, and `bm25` modes and inspect `SYNTHETIC`-badged runbook excerpts (`rb-002`, `inc-2026-002`).
   - **AI Investigations (`/investigations`):** Run an investigation on `checkout-service` — observe the executed LangGraph node trail, grounded citations (with local Ollama running) or graceful abstained deterministic fallback (if Ollama is offline), and try an empty time window to watch the `abstain_no_evidence` short-circuit in action.
