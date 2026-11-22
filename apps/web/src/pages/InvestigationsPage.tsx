import { Fragment, useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  formatUtcTimestamp,
  getInvestigation,
  listIncidents,
  listInvestigations,
  listServices,
  runInvestigation,
} from "../services/api";
import type { InvestigationRead, ServiceSummary } from "../types/api";

function defaultWindow(): { start: string; end: string } {
  const now = new Date();
  const start = new Date(now.getTime() - 60 * 60 * 1000);
  return { start: start.toISOString(), end: now.toISOString() };
}

function confidencePillClass(confidence: "low" | "medium" | "high"): string {
  if (confidence === "high") {
    return "pill pill-ok";
  }
  if (confidence === "medium") {
    return "pill pill-warn";
  }
  return "pill pill-neutral";
}

export function InvestigationsPage() {
  const [services, setServices] = useState<ServiceSummary[]>([]);
  const [investigations, setInvestigations] = useState<InvestigationRead[]>([]);
  const [selected, setSelected] = useState<InvestigationRead | null>(null);

  const [question, setQuestion] = useState<string>(
    "Why did errors and latency spike on this service?",
  );
  const [service, setService] = useState<string>("");
  const [startTime, setStartTime] = useState<string>(() => defaultWindow().start);
  const [endTime, setEndTime] = useState<string>(() => defaultWindow().end);
  const [lookbackMinutes, setLookbackMinutes] = useState<number>(60);
  const [maxDepth, setMaxDepth] = useState<number>(3);
  const [topKDocs, setTopKDocs] = useState<number>(5);

  const [loadingList, setLoadingList] = useState<boolean>(true);
  const [loadingDetail, setLoadingDetail] = useState<boolean>(false);
  const [running, setRunning] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadWorkspace = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    try {
      const [servicesRes, invRes, incRes] = await Promise.all([
        listServices({ limit: 100 }),
        listInvestigations({ limit: 50 }),
        listIncidents({ limit: 5 }),
      ]);
      setServices(servicesRes.items);
      setInvestigations(invRes.items);

      if (invRes.items.length > 0) {
        setSelected((prev) => prev ?? invRes.items[0]);
      }

      const latestInc = incRes.items.find((inc) => Boolean(inc.service_name));
      if (latestInc && latestInc.service_name) {
        const startedMs = new Date(latestInc.started_at).getTime();
        if (!Number.isNaN(startedMs)) {
          setService((prev) => prev || latestInc.service_name || "");
          setStartTime(new Date(startedMs - 6 * 60 * 1000).toISOString());
          setEndTime(new Date(startedMs + 15 * 60 * 1000).toISOString());
        }
      } else if (servicesRes.items.length > 0) {
        setService((prev) => prev || servicesRes.items[0].name);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to load investigations.");
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  const handleSelectInvestigation = async (id: string) => {
    setLoadingDetail(true);
    setError(null);
    try {
      const detail = await getInvestigation(id);
      setSelected(detail);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to load investigation detail.");
    } finally {
      setLoadingDetail(false);
    }
  };

  const handleRunInvestigation = async (e: FormEvent) => {
    e.preventDefault();
    if (!service || !question.trim()) {
      setError("Select a service and enter an investigation question.");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const created = await runInvestigation({
        question: question.trim(),
        service,
        start_time: startTime,
        end_time: endTime,
        lookback_minutes: lookbackMinutes,
        max_depth: maxDepth,
        top_k_docs: topKDocs,
      });
      setSelected(created);
      const refreshed = await listInvestigations({ limit: 50 });
      setInvestigations(refreshed.items);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Investigation request failed.");
    } finally {
      setRunning(false);
    }
  };

  return (
    <>
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Run Bounded AI Investigation</h2>
            <p className="card-subtitle">
              Executes LangGraph evidence collection, hybrid RAG lookup, structured synthesis, and
              deterministic citation/evidence validation.
            </p>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => void loadWorkspace()}
            disabled={loadingList}
          >
            {loadingList ? "Refreshing..." : "Refresh History"}
          </button>
        </div>

        <form onSubmit={handleRunInvestigation} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="field">
            <label htmlFor="inv-question">Investigation Question</label>
            <textarea
              id="inv-question"
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Describe the incident question to investigate..."
              required
            />
          </div>

          <div className="toolbar">
            <div className="field">
              <label htmlFor="inv-service">Target Service</label>
              <select
                id="inv-service"
                value={service}
                onChange={(e) => setService(e.target.value)}
                disabled={services.length === 0}
              >
                {services.length === 0 ? (
                  <option value="">No services registered</option>
                ) : (
                  services.map((svc) => (
                    <option key={svc.id} value={svc.name}>
                      {svc.name}
                    </option>
                  ))
                )}
              </select>
            </div>

            <div className="field" style={{ minWidth: 215 }}>
              <label htmlFor="inv-start">Window Start (UTC ISO)</label>
              <input
                id="inv-start"
                type="text"
                value={startTime}
                onChange={(e) => setStartTime(e.target.value)}
                required
              />
            </div>

            <div className="field" style={{ minWidth: 215 }}>
              <label htmlFor="inv-end">Window End (UTC ISO)</label>
              <input
                id="inv-end"
                type="text"
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
                required
              />
            </div>

            <div className="field" style={{ minWidth: 115 }}>
              <label htmlFor="inv-lookback">Lookback (m)</label>
              <input
                id="inv-lookback"
                type="number"
                min={0}
                max={1440}
                value={lookbackMinutes}
                onChange={(e) => setLookbackMinutes(Number(e.target.value))}
              />
            </div>

            <div className="field" style={{ minWidth: 95 }}>
              <label htmlFor="inv-depth">Depth</label>
              <input
                id="inv-depth"
                type="number"
                min={1}
                max={10}
                value={maxDepth}
                onChange={(e) => setMaxDepth(Number(e.target.value))}
              />
            </div>

            <div className="field" style={{ minWidth: 95 }}>
              <label htmlFor="inv-topk">Top K Docs</label>
              <input
                id="inv-topk"
                type="number"
                min={1}
                max={20}
                value={topKDocs}
                onChange={(e) => setTopKDocs(Number(e.target.value))}
              />
            </div>

            <button
              type="submit"
              className="btn btn-primary"
              disabled={running || !service}
            >
              {running ? "Running LangGraph..." : "Run Investigation"}
            </button>
          </div>
        </form>
      </section>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>{error}</span>
        </div>
      )}

      <div className="inv-workspace">
        <section className="card">
          <div className="card-header">
            <h3 className="card-title">Saved Investigations</h3>
            <span className="pill pill-neutral">{investigations.length}</span>
          </div>

          {investigations.length === 0 ? (
            <div className="empty-state">
              No investigations persisted yet. Run one above to generate an audited report.
            </div>
          ) : (
            <div className="inv-list">
              {investigations.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`inv-list-btn${selected?.id === item.id ? " active" : ""}`}
                  onClick={() => void handleSelectInvestigation(item.id)}
                >
                  <div className="chip-row" style={{ justifyContent: "space-between" }}>
                    <span className="mono">{item.service_name}</span>
                    <span className={item.abstained ? "pill pill-warn" : "pill pill-ok"}>
                      {item.abstained ? "Abstained" : "Grounded"}
                    </span>
                  </div>
                  <div style={{ fontSize: 12.5 }}>{item.report.question}</div>
                  <div className="api-meta">
                    {formatUtcTimestamp(item.created_at)} · {item.model_name}
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="card">
          {loadingDetail ? (
            <div className="empty-state">Loading persisted investigation report...</div>
          ) : !selected ? (
            <div className="empty-state">
              Select a saved investigation on the left or run a new investigation above.
            </div>
          ) : (
            <>
              <div className="card-header">
                <div>
                  <h3 className="card-title">{selected.report.question}</h3>
                  <p className="card-subtitle">
                    Service: <span className="mono">{selected.report.target_service}</span> ·
                    Window:{" "}
                    <span className="mono">
                      {formatUtcTimestamp(selected.report.window_start)} →{" "}
                      {formatUtcTimestamp(selected.report.window_end)}
                    </span>
                  </p>
                </div>
                <div className="chip-row">
                  <span className={selected.abstained ? "pill pill-warn" : "pill pill-ok"}>
                    {selected.abstained ? "Abstained" : "Grounded"}
                  </span>
                  <span className="pill pill-neutral">{selected.model_name}</span>
                  <span className="pill pill-neutral">{selected.duration_ms.toFixed(1)} ms</span>
                </div>
              </div>

              <div style={{ marginBottom: 14 }}>
                <div className="kpi-label" style={{ marginBottom: 6 }}>
                  Executed LangGraph Nodes
                </div>
                <div className="node-trail">
                  {selected.executed_nodes.map((node, idx) => (
                    <Fragment key={`${node}-${idx}`}>
                      <span className="pill pill-accent">{node}</span>
                      {idx < selected.executed_nodes.length - 1 && (
                        <span className="node-arrow">→</span>
                      )}
                    </Fragment>
                  ))}
                </div>
              </div>

              {selected.report.abstained && (
                <div className="banner banner-warn" role="status" style={{ marginBottom: 12 }}>
                  <span>
                    <strong>Investigation Abstained:</strong>{" "}
                    {selected.report.abstention_reason ??
                      "Insufficient verified evidence to propose a root cause."}
                  </span>
                </div>
              )}

              {selected.report.validation_warnings.length > 0 && (
                <div className="banner banner-error" role="alert" style={{ marginBottom: 12 }}>
                  <div>
                    <strong>Validation Warnings ({selected.report.validation_warnings.length}):</strong>
                    <ul className="report-bullets" style={{ marginTop: 6 }}>
                      {selected.report.validation_warnings.map((warn, i) => (
                        <li key={i}>{warn}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}

              <div className="report-section">
                <h4>Confirmed Facts ({selected.report.confirmed_facts.length})</h4>
                {selected.report.confirmed_facts.length === 0 ? (
                  <div className="api-meta">None recorded.</div>
                ) : (
                  <ul className="report-bullets">
                    {selected.report.confirmed_facts.map((line, i) => (
                      <li key={i}>{line}</li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="report-section">
                <h4>Detected Anomalies ({selected.report.detected_anomalies.length})</h4>
                {selected.report.detected_anomalies.length === 0 ? (
                  <div className="api-meta">No anomaly detections listed.</div>
                ) : (
                  <ul className="report-bullets">
                    {selected.report.detected_anomalies.map((line, i) => (
                      <li key={i}>{line}</li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="report-section">
                <h4>Observed Correlations ({selected.report.observed_correlations.length})</h4>
                {selected.report.observed_correlations.length === 0 ? (
                  <div className="api-meta">No correlations listed.</div>
                ) : (
                  <ul className="report-bullets">
                    {selected.report.observed_correlations.map((line, i) => (
                      <li key={i}>{line}</li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="report-section">
                <h4>Probable Root Causes ({selected.report.probable_root_causes.length})</h4>
                {selected.report.probable_root_causes.length === 0 ? (
                  <div className="api-meta">
                    No probable root-cause hypotheses emitted (abstained or unsupported).
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {selected.report.probable_root_causes.map((hyp, i) => (
                      <div key={i} className="hypothesis-card">
                        <div className="chip-row">
                          <span className={confidencePillClass(hyp.confidence)}>
                            confidence: {hyp.confidence}
                          </span>
                          <span className="pill pill-warn">
                            Hypothesis — not proven causation
                          </span>
                        </div>
                        <div>{hyp.hypothesis}</div>
                        <div className="chip-row">
                          <span className="api-meta">Supporting evidence:</span>
                          {hyp.supporting_evidence_ids.map((evId) => (
                            <span key={evId} className="pill pill-accent">
                              {evId}
                            </span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="report-section">
                <h4>Cited Knowledge Sources ({selected.report.citations.length})</h4>
                {selected.report.citations.length === 0 ? (
                  <div className="api-meta">No knowledge documents cited.</div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {selected.report.citations.map((cit) => (
                      <div key={cit.doc_id} className="kb-card">
                        <div className="chip-row">
                          <span className="pill pill-accent">{cit.doc_id}</span>
                          <span className="pill pill-neutral">{cit.doc_type}</span>
                          {cit.is_synthetic && <span className="pill pill-warn">SYNTHETIC</span>}
                        </div>
                        <div style={{ fontWeight: 600 }}>{cit.title}</div>
                        <div className="api-meta">
                          Section: {cit.section_heading} · Source: {cit.source_path}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="report-section">
                <h4>Recommended Next Steps ({selected.report.recommended_next_steps.length})</h4>
                {selected.report.recommended_next_steps.length === 0 ? (
                  <div className="api-meta">None provided.</div>
                ) : (
                  <ul className="report-bullets">
                    {selected.report.recommended_next_steps.map((step, i) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="report-section">
                <h4>Uncertainty &amp; Gaps ({selected.report.uncertainty_and_gaps.length})</h4>
                {selected.report.uncertainty_and_gaps.length === 0 ? (
                  <div className="api-meta">No explicit gaps listed.</div>
                ) : (
                  <ul className="report-bullets">
                    {selected.report.uncertainty_and_gaps.map((gap, i) => (
                      <li key={i}>{gap}</li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}
