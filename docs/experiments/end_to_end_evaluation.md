# End-to-End Root-Cause, RAG Guardrail & System Performance Evaluation

> **Provenance & Evaluation Mode Notice:** All results below were measured on SignalForge's explicitly labeled **synthetic** incident scenarios (`evaluation/root_cause/scenarios.py`) and **synthetic** 37-document knowledge base (`data/synthetic/knowledge_base/`).
>
> **Important distinction (`deterministic` vs. `ollama` mode):** The end-to-end investigation numbers below were recorded in `--mode deterministic` using live NetworkX topology traversal, live Qdrant + `sentence-transformers/all-MiniLM-L6-v2` (CPU) Hybrid RAG, the real compiled LangGraph `StateGraph` workflow, and deterministic evidence-validator guardrails paired with a prompt-grounded `FakeLLMClient` double (`deterministic-grounded-double`). This validates orchestration, topology impact, live Hybrid RAG recall, latency, and hallucination interception — it is **not** a measurement of live local LLM semantic reasoning quality. Local Ollama (`http://localhost:11434`, `qwen2.5:7b`) was not running during this benchmark capture; running `.venv\Scripts\python.exe scripts/run_e2e_evaluation.py --mode ollama` executes the exact same 8 scenarios against live local Ollama once started.

## 1. End-to-End Investigation & Guardrail Evaluation (`--mode deterministic`)

- **Source artifact:** [e2e_results.json](../../experiments/benchmarks/end_to_end/e2e_results.json)
- **Reproduction command:**
  ```powershell
  .venv\Scripts\python.exe scripts/run_e2e_evaluation.py --mode deterministic
  ```
- **Live Ollama reproduction command (when Ollama is running locally):**
  ```powershell
  ollama pull qwen2.5:7b
  .venv\Scripts\python.exe scripts/run_e2e_evaluation.py --mode ollama
  ```
- **Scope:** `8` controlled synthetic scenarios (`7` active fault scenarios + `1` zero-evidence abstention scenario) + `1` multi-fault adversarial hallucination stress probe over live Qdrant (`signalforge_e2e_eval`, deleted on teardown) + `BM25Index`.

### 1.1 Aggregate Measured Metrics

| Metric | Measured Value | Scope / Notes |
| --- | ---: | --- |
| Top-1 Root-Cause Keyword Hit (`deterministic` double) | 1.0000 | 7 / 7 fault scenarios (pipeline grounding check; not live LLM) |
| Top-3 Root-Cause Keyword Hit (`deterministic` double) | 1.0000 | 7 / 7 fault scenarios (pipeline grounding check; not live LLM) |
| Mean Affected Service F1 | 1.0000 | 8 / 8 scenarios via live NetworkX multi-hop impact traversal |
| Mean Live KB Expected Doc Recall (`top_k=5` chunks) | 0.8095 | 7 fault scenarios against live Qdrant + `all-MiniLM-L6-v2` + BM25 |
| Mean Citation Correctness | 1.0000 | 100% cited docs hydrated from retrieved context with complete metadata |
| Mean Unsupported Root-Cause Rate | 0.0000 | 0 hypotheses missing valid `supporting_evidence_ids` |
| Mean Evidence Coverage | 1.0000 | 100% emitted hypotheses & confirmed facts grounded in bundle evidence |
| Abstention Accuracy | 1.0000 | 8 / 8 scenarios (`insufficient_evidence_abstention` abstained cleanly) |
| Workflow Duration (`p50` / `p95`, live CPU RAG) | 57.30 ms / 82.10 ms | Across all 8 scenarios (excludes live LLM token generation) |

### 1.2 Per-Scenario Breakdown

| Scenario ID | Target Service | Expect Abstain | Abstained | Executed Path | Affected Svc F1 | Live KB Expected Doc Recall | Citation Correctness | Unsupported Rate | Duration (ms) |
| --- | --- | :---: | :---: | --- | ---: | ---: | ---: | ---: | ---: |
| `cpu_saturation` | `checkout-service` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 84.23 |
| `db_connection_exhaustion` | `postgres-primary` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 62.35 |
| `payment_service_failure` | `payment-service` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 52.25 |
| `memory_leak` | `order-service` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 0.6667 | 1.0000 | 0.0000 | 50.91 |
| `network_latency` | `api-gateway` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 0.6667 | 1.0000 | 0.0000 | 51.90 |
| `faulty_deployment` | `payment-service` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 0.6667 | 1.0000 | 0.0000 | 78.14 |
| `message_queue_backlog` | `kafka-broker` | No | No | `plan -> collect -> synth -> validate` | 1.0000 | 0.6667 | 1.0000 | 0.0000 | 73.55 |
| `insufficient_evidence_abstention` | `checkout-service` | Yes | Yes | `plan -> collect -> abstain_no_evidence` | 1.0000 | — | 1.0000 | 0.0000 | 40.79 |

*Observation on Live KB Expected Doc Recall (`0.8095`):* On the 4 scenarios expecting 3 reference docs (`memory_leak`, `network_latency`, `faulty_deployment`, `message_queue_backlog`), retrieving `top_k=5` chunks returns 2 of 3 expected docs (because multiple high-scoring chunks come from the top 2 runbook/postmortem docs before deduplication). Increasing workflow `top_k_docs` from `5` to `8–10` or deduplicating chunk candidates before truncating to `top_k` is a straightforward future improvement.

### 1.3 Adversarial Hallucination Interception Probe

Injected a hostile `FakeLLMClient` emitting fabricated `doc_id`s (`rb-999-fabricated`, `inc-9999-ghost`), fabricated `evidence_id`s (`ev-hallucinated-999`, `ev-fake-1`, `ev-fake-2`), an unsupported root-cause hypothesis, and an empty-window root-cause claim:

| Adversarial Check | Injected | Intercepted / Verified |
| --- | ---: | ---: |
| Fabricated `doc_id` citations stripped | 2 | 2 / 2 |
| Fabricated `evidence_id` references stripped | 3 | 3 / 3 |
| Unsupported hypotheses dropped (`0` valid evidence IDs) | 1 | 1 / 1 |
| Empty-evidence workflow short-circuited (`abstain_no_evidence`, `0` LLM calls) | 1 | `true` (`0` LLM calls) |
| Direct validator forced abstention on empty `EvidenceBundle` | 1 | `true` |
| **Post-validation Unsupported Root-Cause Rate** | — | **0.0000** |
| **Post-validation Citation Correctness** | — | **1.0000** |
| **Overall Guardrail Interception Rate** | 7 | **1.0000 (7 / 7)** |

---

## 2. Local System Performance Benchmark

- **Source artifact:** [system_perf.json](../../experiments/benchmarks/end_to_end/system_perf.json)
- **Reproduction command:**
  ```powershell
  .venv\Scripts\python.exe scripts/run_system_benchmark.py
  ```
- **Environment:** `Windows-11-10.0.26300-SP0`, Python `3.12.14`, `16` logical CPUs, PostgreSQL 16 (`signalforge_test` truncated on exit), Qdrant (`signalforge_perf_bench` deleted on exit).

### 2.1 Batch Ingestion Throughput (PostgreSQL 16 + Deduplication)

| Pipeline | Batch Size | Accepted | Elapsed (s) | Throughput |
| --- | ---: | ---: | ---: | ---: |
| Events (`ingest_event_batch`) | 1,000 | 1,000 | 0.9976 | **1,002.40 events/s** |
| Metrics (`ingest_metric_batch`) | 2,000 | 2,000 | 1.0637 | **1,880.26 metrics/s** |

### 2.2 API Endpoint Latencies (`20` Timed Requests Each via FastAPI `TestClient`)

| Endpoint | Iterations | p50 (ms) | p95 (ms) | Min (ms) | Max (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `GET /health` | 20 | 7.17 | 10.53 | 5.27 | 11.48 |
| `GET /api/v1/events` (`limit=50`) | 20 | 13.98 | 17.24 | 11.49 | 27.33 |
| `GET /api/v1/metrics` (`limit=100`) | 20 | 16.77 | 19.21 | 14.23 | 20.58 |
| `GET /api/v1/graph/dependencies` | 20 | 15.01 | 18.39 | 13.94 | 23.52 |
| `POST /api/v1/correlation/timeline` | 20 | 37.95 | 61.68 | 22.85 | 216.04 |
| `POST /api/v1/anomalies/detect` (`lstm_autoencoder`, `46` aligned pts) | 20 | 38.97 | 41.71 | 36.49 | 42.36 |

### 2.3 Investigation Workflow Latency & Process Memory Footprint

- **Deterministic Investigation Workflow (`POST /api/v1/investigations/run`, `5` runs, live PostgreSQL + live Qdrant hash + `FakeLLMClient`):** `p50 = 92.09 ms`, `p95 = 136.54 ms` (`min = 73.56 ms`, `max = 141.79 ms`).
- **Python Process RSS Memory (Windows `GetProcessMemoryInfo`):** `start = 435.91 MB` (after loading PyTorch CPU, scikit-learn, FastAPI, SQLAlchemy, LangGraph) → `end = 467.32 MB` after ingestion, LSTM inference, correlation, and 5 persisted investigations.

---

## 3. Honest Limitations

1. **Live local Ollama (`qwen2.5:7b`) not yet benchmarked here:** In `--mode deterministic`, `Top-1`/`Top-3` root-cause hit confirms that telemetry evidence flows intact into prompts and passes validation — it does **not** measure live LLM reasoning errors, instruction drift, or multi-second local token generation latency (typically several seconds per investigation on CPU).
2. **In-process `TestClient` vs. network Uvicorn:** API latencies were measured via Starlette/FastAPI `TestClient` against real Docker PostgreSQL 16 and Qdrant containers, omitting TCP/HTTP framing overhead of an external client over loopback (typically ~1–3 ms).
3. **Small synthetic data volume:** Benchmark tables held `1,000` events, `2,000` metrics, and `3` spans during timing; large multi-week production tables would require time-partitioned PostgreSQL tables or TimescaleDB to maintain flat query latency.
