# Hybrid Knowledge Retrieval Benchmark (Synthetic Operations KB)

> **Data provenance notice:** All results below were measured on SignalForge's explicitly labeled **synthetic** operations knowledge base (`data/synthetic/knowledge_base/`, `37` Markdown documents / `143` chunks) using `24` curated benchmark queries (`evaluation/retrieval/benchmark_queries.json`, `"is_synthetic_corpus": true`). These numbers establish a reproducible baseline for comparing lexical, dense, and hybrid retrieval over SignalForge's synthetic corpus and are not production accuracy guarantees.

## 1. Experimental Setup

- **Source artifact:** [results.json](../../experiments/benchmarks/retrieval_kb/results.json)
- **Benchmark queries:** [benchmark_queries.json](../../evaluation/retrieval/benchmark_queries.json) (`24` queries across `3` balanced categories of `8` queries each: `exact_lookup`, `conceptual_symptom`, `multi_service_incident`)
- **Reproduction command:**
  ```powershell
  .venv\Scripts\python scripts/run_retrieval_benchmark.py
  ```
- **Corpus & Chunking:**
  - `37` synthetic Markdown documents (`15` runbooks, `10` incident postmortems, `6` architecture documents, `6` deployment reports) chunked via `MarkdownChunker(max_chars=1200, overlap_chars=150)` into `143` deterministic UUID5 chunks.
  - All `expected_doc_ids` are validated against the loaded knowledge base prior to evaluation.
- **Models & Infrastructure:**
  - **Dense embedder:** `sentence-transformers/all-MiniLM-L6-v2` (`384`-dim L2-normalized vectors, forced `device="cpu"`).
  - **Vector store:** Live Qdrant container (`localhost:6333`), isolated temporary collection `signalforge_knowledge_bench` (`Distance.COSINE`), deleted automatically in a `finally` block on completion.
  - **Lexical index:** In-memory `BM25Index` (`rank_bm25.BM25Okapi`) with compound-identifier + sub-token regex tokenization.
  - **Hybrid fusion:** Reciprocal Rank Fusion (`1 / (rrf_k + rank)`, `rrf_k=60`, `candidate_k = max(top_k * 4, 20)`), deterministic `chunk_id` tie-breaking.
  - **Indexing & warmup:** All `143` chunks indexed in `72.19 s` on CPU (including model weight load), followed by `1` untimed hybrid warmup query so Qdrant connection pools are warm before steady-state latency timing.
- **Evaluation Protocol:**
  - Every mode runs with `top_k=10` chunks per query. Retrieved chunks are deduplicated by `doc_id` in first-seen rank order (`deduplicate_doc_ids`) before computing document-level **Recall@5**, **MRR@10**, and binary-relevance **nDCG@10**, so multiple chunks from the same document never inflate scores.
  - **Citation Metadata Completeness** checks that every retrieved chunk retains non-empty `doc_id`, `doc_type`, `title`, `section_heading`, `source_path`, and boolean `is_synthetic`.

## 2. Overall Measured Results (`24` Queries, `top_k=10`)

| Mode | Recall@5 | MRR@10 | nDCG@10 | Citation Completeness | p50 Latency (ms) | p95 Latency (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25` | 0.9861 | 0.9514 | 0.9384 | 1.0000 | 3.15 | 7.57 |
| `dense` (`all-MiniLM-L6-v2`) | 0.9653 | 0.9792 | 0.9540 | 1.0000 | 52.63 | 123.31 |
| `hybrid` (RRF, `k=60`) | **0.9861** | **0.9792** | **0.9673** | **1.0000** | 53.66 | 70.02 |

## 3. Per-Category Breakdown (`8` Queries per Category)

| Query Category | Mode | Recall@5 | MRR@10 | nDCG@10 |
| --- | --- | ---: | ---: | ---: |
| `exact_lookup` | `bm25` | 1.0000 | 1.0000 | 1.0000 |
| `exact_lookup` | `dense` | 1.0000 | 1.0000 | 0.9959 |
| `exact_lookup` | `hybrid` | 1.0000 | 1.0000 | 1.0000 |
| `conceptual_symptom` | `bm25` | 1.0000 | 0.8542 | 0.8510 |
| `conceptual_symptom` | `dense` | 1.0000 | 0.9375 | 0.9438 |
| `conceptual_symptom` | `hybrid` | 1.0000 | 0.9375 | 0.9539 |
| `multi_service_incident` | `bm25` | 0.9583 | 1.0000 | 0.9640 |
| `multi_service_incident` | `dense` | 0.8958 | 1.0000 | 0.9223 |
| `multi_service_incident` | `hybrid` | 0.9583 | 1.0000 | 0.9479 |

## 4. Analysis & Honest Limitations

1. **Hybrid RRF matches or exceeds both standalone retrievers overall:**
   - On `exact_lookup` queries (SQLSTATE codes, error constants, release versions, Postgres views), `bm25` achieves `1.0000` across all three ranking metrics while `dense` drops slightly to `0.9959` nDCG@10 (`q-08` ranks `inc-2026-002` above `arch-004`). `hybrid` preserves the exact `1.0000` result.
   - On `conceptual_symptom` queries written in natural on-call phrasing without exact identifiers, `bm25` drops to `0.8542` MRR@10 and `0.8510` nDCG@10 (e.g., `q-11` ranks `rb-015` above `rb-005`; `q-16` ranks `inc-2026-009` at rank 3). `dense` raises `conceptual_symptom` nDCG@10 to `0.9438`, and `hybrid` improves it further to `0.9539`.
   - On `multi_service_incident` queries, `dense` misses `arch-002` inside top-5 on `q-24` (`trace_id`/`span_id` propagation), lowering `Recall@5` to `0.8958`; `hybrid` recovers `arch-002` into top-5 and restores `Recall@5` to `0.9583`.
   - Across all `24` queries, only `q-19` misses 1 of its 3 expected docs (`arch-005`, which mentions `inc-2026-008` in a single bullet) in all three modes, yielding `0.6667` Recall@5 on that query.
2. **Latency profile on local CPU:**
   - Pure in-memory `bm25` runs in `3.15 ms` p50 / `7.57 ms` p95.
   - `dense` (`52.63 ms` p50 / `123.31 ms` p95) and `hybrid` (`53.66 ms` p50 / `70.02 ms` p95) are dominated by CPU Sentence-Transformer query encoding and local Qdrant HTTP I/O; adding BM25 + RRF adds `~1 ms` median overhead over dense-only retrieval. (The `123.31 ms` `dense` p95 reflects a single OS/CPU scheduling tail spike over 24 samples.)
3. **Key limitations:**
   - **Small, clean synthetic corpus (`37` docs / `143` chunks):** Real-world enterprise wikis contain thousands of stale, conflicting, or poorly structured pages where precision/recall margins will be substantially harder and lower.
   - **Single-turn queries with binary relevance:** The `24` queries evaluate single-turn retrieval with binary relevant/irrelevant labels rather than graded relevance or multi-hop follow-up queries.
   - **No cross-encoder reranker yet:** At larger corpus scales, adding a lightweight CPU cross-encoder reranker over top-20 RRF candidates may further improve rank-1 precision on ambiguous queries.
