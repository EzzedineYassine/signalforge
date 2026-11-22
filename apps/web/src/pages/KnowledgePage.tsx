import { useState, type FormEvent } from "react";
import { ApiError, indexKnowledge, searchKnowledge } from "../services/api";
import type {
  DocType,
  KnowledgeIndexResponse,
  KnowledgeSearchResponse,
  RetrievalMode,
} from "../types/api";

const RETRIEVAL_MODES: { value: RetrievalMode; label: string }[] = [
  { value: "hybrid", label: "Hybrid (RRF: Qdrant + BM25)" },
  { value: "dense", label: "Dense (all-MiniLM-L6-v2)" },
  { value: "bm25", label: "BM25 Lexical" },
];

const DOC_TYPES: { value: "" | DocType; label: string }[] = [
  { value: "", label: "All doc types" },
  { value: "runbook", label: "Runbook" },
  { value: "incident", label: "Incident Postmortem" },
  { value: "architecture", label: "Architecture" },
  { value: "deployment", label: "Deployment Report" },
];

export function KnowledgePage() {
  const [query, setQuery] = useState<string>("");
  const [mode, setMode] = useState<RetrievalMode>("hybrid");
  const [serviceFilter, setServiceFilter] = useState<string>("");
  const [docTypeFilter, setDocTypeFilter] = useState<"" | DocType>("");
  const [topK, setTopK] = useState<number>(5);

  const [indexing, setIndexing] = useState<boolean>(false);
  const [searching, setSearching] = useState<boolean>(false);
  const [indexResult, setIndexResult] = useState<KnowledgeIndexResponse | null>(null);
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleIndexSyntheticKb = async () => {
    setIndexing(true);
    setError(null);
    try {
      const res = await indexKnowledge({
        source_dir: "data/synthetic/knowledge_base",
        max_chars: 1200,
        overlap_chars: 150,
      });
      setIndexResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to index knowledge base.");
    } finally {
      setIndexing(false);
    }
  };

  const handleSearch = async (e: FormEvent) => {
    e.preventDefault();
    const cleaned = query.trim();
    if (!cleaned) {
      setError("Enter a search query to retrieve operational documentation.");
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const res = await searchKnowledge({
        query: cleaned,
        top_k: topK,
        mode,
        service: serviceFilter.trim() || null,
        doc_type: docTypeFilter || null,
      });
      setSearchResult(res);
    } catch (err) {
      setSearchResult(null);
      setError(err instanceof ApiError ? err.detail : "Knowledge search failed.");
    } finally {
      setSearching(false);
    }
  };

  return (
    <>
      <section className="card">
        <div className="card-header" style={{ marginBottom: indexResult ? 12 : 0 }}>
          <div>
            <h2 className="card-title">Operations Knowledge Base Indexing</h2>
            <p className="card-subtitle">
              Idempotently chunk and index synthetic runbooks, postmortems, architecture docs, and
              deployment reports into Qdrant + BM25.
            </p>
          </div>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void handleIndexSyntheticKb()}
            disabled={indexing}
          >
            {indexing ? "Indexing KB..." : "Index Synthetic Knowledge Base"}
          </button>
        </div>

        {indexResult && (
          <div className="banner banner-ok" role="status">
            <span>
              Indexed <strong>{indexResult.documents_indexed}</strong> documents (
              <strong>{indexResult.chunks_indexed}</strong> chunks) from{" "}
              <span className="mono">{indexResult.source_dir}</span> into collection{" "}
              <span className="mono">{indexResult.collection}</span> using{" "}
              <span className="mono">{indexResult.embedding_model}</span>.
            </span>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <div>
            <h3 className="card-title">Hybrid Knowledge Search</h3>
            <p className="card-subtitle">
              Query Qdrant dense embeddings, BM25Okapi lexical index, or Reciprocal Rank Fusion
            </p>
          </div>
        </div>

        <form className="toolbar" onSubmit={handleSearch}>
          <div className="field" style={{ flex: "1 1 300px" }}>
            <label htmlFor="kb-query">Query</label>
            <input
              id="kb-query"
              type="text"
              placeholder="e.g. database connection pool exhausted postgres or SQLSTATE 40P01"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>

          <div className="field">
            <label htmlFor="kb-mode">Retrieval Mode</label>
            <select
              id="kb-mode"
              value={mode}
              onChange={(e) => setMode(e.target.value as RetrievalMode)}
            >
              {RETRIEVAL_MODES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="kb-service">Service Filter (optional)</label>
            <input
              id="kb-service"
              type="text"
              placeholder="All services"
              value={serviceFilter}
              onChange={(e) => setServiceFilter(e.target.value)}
            />
          </div>

          <div className="field">
            <label htmlFor="kb-doctype">Document Type</label>
            <select
              id="kb-doctype"
              value={docTypeFilter}
              onChange={(e) => setDocTypeFilter(e.target.value as "" | DocType)}
            >
              {DOC_TYPES.map((dt) => (
                <option key={dt.value || "all"} value={dt.value}>
                  {dt.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ minWidth: 95 }}>
            <label htmlFor="kb-topk">Top K</label>
            <input
              id="kb-topk"
              type="number"
              min={1}
              max={20}
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
            />
          </div>

          <button type="submit" className="btn btn-primary" disabled={searching}>
            {searching ? "Searching..." : "Search"}
          </button>
        </form>
      </section>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>{error}</span>
        </div>
      )}

      {searchResult && (
        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">
                Retrieved Chunks for <span className="mono">{searchResult.query}</span>
              </h3>
              <p className="card-subtitle">Mode: {searchResult.mode}</p>
            </div>
            <span className="pill pill-accent">{searchResult.total} results</span>
          </div>

          {searchResult.results.length === 0 ? (
            <div className="empty-state">
              <div>No matching knowledge chunks found.</div>
              <code>Click "Index Synthetic Knowledge Base" above if Qdrant has not been indexed yet</code>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {searchResult.results.map((chunk, idx) => (
                <article key={chunk.chunk_id} className="kb-card">
                  <div className="chip-row">
                    <span className="pill pill-neutral">#{idx + 1}</span>
                    <span className="pill pill-accent">{chunk.doc_id}</span>
                    <span className="pill pill-neutral">{chunk.doc_type}</span>
                    <span className="pill pill-neutral">{chunk.service}</span>
                    <span className="pill pill-ok">{chunk.retrieval_source}</span>
                    <span className="mono">score={chunk.score.toFixed(4)}</span>
                    {chunk.is_synthetic && <span className="pill pill-warn">SYNTHETIC</span>}
                  </div>
                  <h4 className="kb-title">{chunk.title}</h4>
                  <div className="api-meta">
                    Section: <strong>{chunk.section_heading}</strong> · Source:{" "}
                    <span>{chunk.source_path}</span> (chunk #{chunk.chunk_index})
                  </div>
                  <pre className="kb-excerpt">{chunk.text}</pre>
                </article>
              ))}
            </div>
          )}
        </section>
      )}
    </>
  );
}
