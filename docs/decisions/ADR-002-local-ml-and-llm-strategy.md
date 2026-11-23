# ADR-002: Local CPU ML, Lightweight CPU Embeddings, and Replaceable Ollama LLM Strategy

- **Status:** Accepted
- **Date:** 2026-11-22

## Context

SignalForge must run completely locally on a developer laptop with **16 GB system RAM** and an **8 GB VRAM consumer GPU** (or CPU-only machines) while simultaneously hosting Docker Desktop (`postgres:16-alpine` + `qdrant/qdrant`), FastAPI, the React/Vite frontend, multivariate anomaly detection, hybrid RAG embeddings, and structured LLM incident synthesis.

Three key resource and security constraints shaped the ML/AI runtime architecture:
1. **VRAM contention:** Running PyTorch anomaly models, Sentence-Transformers, and a 7B local LLM on the same 8 GB GPU simultaneously causes out-of-memory failures and forces fragile CUDA driver coupling across developer machines.
2. **Wheel size & reproducibility:** Default PyPI `torch` installs pull a ~2.5 GB CUDA runtime wheel even when inference is tiny, slowing clean checkouts and CI/local installs.
3. **Security & auditability:** Unsafe Python serialization (`pickle`, `joblib`) is prohibited across SignalForge's domain packages (`services/`), and LLM synthesis must remain cleanly testable without requiring a live GPU or external cloud API key.

## Decision

1. **CPU-only PyTorch (`torch==2.15.0+cpu`) for Multivariate Anomaly Detection:**
   - Force `device="cpu"` in `LSTMAutoencoderDetector` (`hidden_size=32`, `latent_size=16`, 1 layer, ~54 KB `weights.pt` loaded strictly with `weights_only=True`).
   - Pair it with lightweight statistical (`RollingZScoreDetector`) and classical ML (`IsolationForestDetector`) baselines serialized strictly via JSON configs (`zero pickle/joblib`).
2. **CPU Sentence-Transformers (`sentence-transformers/all-MiniLM-L6-v2`) + JSON-Persisted BM25:**
   - Force `device="cpu"` in `SentenceTransformerEmbedder` (`384`-dim L2-normalized embeddings, ~90 MB model footprint, lazy-loaded on first use).
   - Pair dense Qdrant cosine retrieval with in-memory `BM25Okapi` persisted strictly as pure JSON (`data/indexes/bm25_index.json`) and fused via Reciprocal Rank Fusion (`rrf_k=60`).
3. **Replaceable `LLMClient` Protocol Backed by Local Ollama (`num_ctx=8192`):**
   - Define a strict `LLMClient` protocol (`generate_structured(system_prompt, user_prompt, response_model) -> T`) implemented by `OllamaClient` (calling local `POST /api/chat` with `stream=False`, `temperature=0.1`, `num_ctx=8192`, `format=response_model.model_json_schema()`, and explicit `timeout_seconds=60.0`) and `FakeLLMClient` for deterministic testing.
   - Keep **100% of GPU VRAM reserved exclusively for Ollama** (`qwen2.5:7b` Q4 quantization fits comfortably within 8 GB VRAM at `num_ctx=8192`).
   - Short-circuit the LLM completely when `has_actionable_evidence` is `False`, and degrade cleanly to a deterministic evidence-only abstained report whenever `LLMClientError` occurs.

## Alternatives Considered

- **Cloud LLM APIs (OpenAI / Anthropic / Gemini):** Would improve zero-shot reasoning accuracy and remove local VRAM constraints, but violates SignalForge's local-first, zero-API-key reproducibility goal and introduces external billing/network dependencies.
- **GPU-accelerated PyTorch + GPU embeddings + Ollama sharing one 8 GB GPU:** Causes frequent CUDA OOM crashes when `qwen2.5:7b` allocates KV cache alongside PyTorch CUDA contexts, while offering negligible speed benefit for a 54 KB LSTM scoring 46–2,016 rows (~39–52 ms on CPU) or `all-MiniLM-L6-v2` encoding single queries (~53 ms median on CPU).
- **Heavy agent frameworks with unconstrained ReAct loops:** Unbounded autonomous tool loops quickly exceed `8192`-token contexts on 7B models, increase latency unpredictably, and amplify hallucination risk. A strictly bounded, acyclic LangGraph `StateGraph` (maximum 4 nodes) provides deterministic execution traces and reliable prompt budgeting.

## Consequences

### Positive
- **Fits cleanly inside 16 GB RAM / 8 GB VRAM:** Measured backend process RSS (with PyTorch CPU, scikit-learn, FastAPI, SQLAlchemy, and LangGraph loaded) sits at `435.91–467.32 MB`, leaving ample headroom for Docker containers and local Ollama.
- **Fast local CPU latency:** LSTM inference takes `38.97 ms` p50 on 46 aligned points (`51.51 ms` per 1,000 rows in benchmark); hybrid retrieval runs in `53.66 ms` p50 on CPU.
- **Zero-dependency testability & graceful degradation:** All `260` automated tests pass deterministically with zero cloud keys and without requiring a live Ollama daemon, while `--mode ollama` and production API calls seamlessly invoke local Ollama when running.
- **Strict safety boundaries:** Eliminating `pickle`/`joblib` and enforcing deterministic citation/evidence validation prevents arbitrary deserialization and fabricated citations.

### Negative / Trade-offs
- **CPU LSTM training time:** Fitting 25 epochs on 6,048 rows takes `149.39 s` on CPU (versus `0.67 s` for Isolation Forest), making offline/scheduled retraining preferable to synchronous API-request training.
- **7B local model ceiling:** `qwen2.5:7b` with an `8192`-token context window requires compact prompt serialization (`build_investigation_user_prompt` caps evidence items and snippets) and has lower multi-hop synthesis capability than frontier cloud models — mitigated by doing deterministic correlation, graph traversal, and evidence validation outside the LLM.
- **No `pickle` for Isolation Forest:** Under the no-`pickle` rule, `IsolationForestDetector` persists JSON configuration and reconstructs deterministically via refit rather than loading binary tree objects.
