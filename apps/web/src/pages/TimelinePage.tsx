import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  formatUtcTimestamp,
  listIncidents,
  listServices,
  postCorrelationTimeline,
} from "../services/api";
import type {
  CorrelationTimelineResponse,
  IncidentRead,
  ServiceSummary,
  SignalType,
} from "../types/api";

function signalPillClass(signalType: SignalType): string {
  switch (signalType) {
    case "deployment":
      return "pill pill-accent";
    case "anomaly":
      return "pill pill-danger";
    case "event":
      return "pill pill-warn";
    case "incident":
      return "pill pill-danger";
  }
}

function defaultWindow(): { start: string; end: string } {
  const now = new Date();
  const start = new Date(now.getTime() - 60 * 60 * 1000);
  return { start: start.toISOString(), end: now.toISOString() };
}

export function TimelinePage() {
  const [services, setServices] = useState<ServiceSummary[]>([]);
  const [incidents, setIncidents] = useState<IncidentRead[]>([]);
  const [selectedService, setSelectedService] = useState<string>("");
  const [startTime, setStartTime] = useState<string>(() => defaultWindow().start);
  const [endTime, setEndTime] = useState<string>(() => defaultWindow().end);
  const [lookbackMinutes, setLookbackMinutes] = useState<number>(60);
  const [maxDepth, setMaxDepth] = useState<number>(3);

  const [result, setResult] = useState<CorrelationTimelineResponse | null>(null);
  const [loadingInit, setLoadingInit] = useState<boolean>(true);
  const [correlating, setCorrelating] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const runCorrelation = useCallback(
    async (
      svc: string,
      start: string,
      end: string,
      lookback: number,
      depth: number,
    ) => {
      if (!svc) {
        return;
      }
      setCorrelating(true);
      setError(null);
      try {
        const res = await postCorrelationTimeline({
          service: svc,
          start_time: start,
          end_time: end,
          lookback_minutes: lookback,
          max_depth: depth,
        });
        setResult(res);
      } catch (err) {
        setResult(null);
        setError(
          err instanceof ApiError ? err.detail : "Failed to build correlation timeline.",
        );
      } finally {
        setCorrelating(false);
      }
    },
    [],
  );

  const loadInitial = useCallback(async () => {
    setLoadingInit(true);
    setError(null);
    try {
      const [servicesRes, incidentsRes] = await Promise.all([
        listServices({ limit: 100 }),
        listIncidents({ limit: 25 }),
      ]);
      setServices(servicesRes.items);
      setIncidents(incidentsRes.items);

      const latestWithService = incidentsRes.items.find((inc) => Boolean(inc.service_name));
      if (latestWithService && latestWithService.service_name) {
        const startedMs = new Date(latestWithService.started_at).getTime();
        if (!Number.isNaN(startedMs)) {
          const wStart = new Date(startedMs - 6 * 60 * 1000).toISOString();
          const wEnd = new Date(startedMs + 15 * 60 * 1000).toISOString();
          setSelectedService(latestWithService.service_name);
          setStartTime(wStart);
          setEndTime(wEnd);
          await runCorrelation(latestWithService.service_name, wStart, wEnd, 60, 3);
        }
      } else if (servicesRes.items.length > 0) {
        setSelectedService((prev) => prev || servicesRes.items[0].name);
      }
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail : "Failed to load services or incidents.",
      );
    } finally {
      setLoadingInit(false);
    }
  }, [runCorrelation]);

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    void runCorrelation(selectedService, startTime, endTime, lookbackMinutes, maxDepth);
  };

  const handleUseIncident = (inc: IncidentRead) => {
    const svc = inc.service_name ?? selectedService;
    const startedMs = new Date(inc.started_at).getTime();
    if (!svc || Number.isNaN(startedMs)) {
      return;
    }
    const wStart = new Date(startedMs - 6 * 60 * 1000).toISOString();
    const wEnd = new Date(startedMs + 15 * 60 * 1000).toISOString();
    setSelectedService(svc);
    setStartTime(wStart);
    setEndTime(wEnd);
    void runCorrelation(svc, wStart, wEnd, lookbackMinutes, maxDepth);
  };

  return (
    <>
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Multi-Signal Correlation Timeline</h2>
            <p className="card-subtitle">
              Correlate deployments (with lookback), anomalies, error/warning events, and incidents
              across dependent services.
            </p>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => void loadInitial()}
            disabled={loadingInit}
          >
            {loadingInit ? "Loading..." : "Reload Signals"}
          </button>
        </div>

        <form className="toolbar" onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="tl-service">Target Service</label>
            <select
              id="tl-service"
              value={selectedService}
              onChange={(e) => setSelectedService(e.target.value)}
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
            <label htmlFor="tl-start">Window Start (UTC ISO)</label>
            <input
              id="tl-start"
              type="text"
              value={startTime}
              onChange={(e) => setStartTime(e.target.value)}
              required
            />
          </div>

          <div className="field" style={{ minWidth: 215 }}>
            <label htmlFor="tl-end">Window End (UTC ISO)</label>
            <input
              id="tl-end"
              type="text"
              value={endTime}
              onChange={(e) => setEndTime(e.target.value)}
              required
            />
          </div>

          <div className="field" style={{ minWidth: 135 }}>
            <label htmlFor="tl-lookback">Lookback (min)</label>
            <input
              id="tl-lookback"
              type="number"
              min={0}
              max={1440}
              value={lookbackMinutes}
              onChange={(e) => setLookbackMinutes(Number(e.target.value))}
            />
          </div>

          <div className="field" style={{ minWidth: 110 }}>
            <label htmlFor="tl-depth">Max Depth</label>
            <input
              id="tl-depth"
              type="number"
              min={1}
              max={10}
              value={maxDepth}
              onChange={(e) => setMaxDepth(Number(e.target.value))}
            />
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            disabled={correlating || !selectedService}
          >
            {correlating ? "Correlating..." : "Correlate Timeline"}
          </button>
        </form>
      </section>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>{error}</span>
        </div>
      )}

      {result && (
        <>
          <div className="banner banner-warn" role="note">
            <span>
              <strong>Correlation Disclaimer:</strong> {result.correlation_note}
            </span>
          </div>

          <section className="card">
            <div className="card-header">
              <div>
                <h3 className="card-title">
                  Correlated Scope for <span className="mono">{result.target_service}</span>
                </h3>
                <p className="card-subtitle">
                  Window {formatUtcTimestamp(result.window_start)} →{" "}
                  {formatUtcTimestamp(result.window_end)} (deployments lookback from{" "}
                  {formatUtcTimestamp(result.deployment_window_start)})
                </p>
              </div>
              <div className="chip-row">
                <span className="pill pill-accent">{result.deployment_count} deployments</span>
                <span className="pill pill-danger">{result.anomaly_count} anomalies</span>
                <span className="pill pill-warn">{result.event_count} events</span>
                <span className="pill pill-danger">{result.incident_count} incidents</span>
              </div>
            </div>

            <div className="chip-row" style={{ marginBottom: 16 }}>
              <span className="kpi-label">Related services:</span>
              {result.related_services.length === 0 ? (
                <span className="pill pill-neutral">none (isolated service)</span>
              ) : (
                result.related_services.map((svc) => (
                  <span key={svc} className="pill pill-neutral">
                    {svc}
                  </span>
                ))
              )}
            </div>

            {result.timeline.length === 0 ? (
              <div className="empty-state">
                No correlated deployments, anomalies, warning/error events, or incidents found in
                this window.
              </div>
            ) : (
              <div className="timeline-list">
                {result.timeline.map((item, idx) => (
                  <article
                    key={`${item.signal_type}-${item.record_id ?? idx}-${item.timestamp_utc}`}
                    className={`timeline-entry sig-${item.signal_type}`}
                  >
                    <div className="timeline-meta">
                      <span className={signalPillClass(item.signal_type)}>
                        {(item.signal_type ?? "signal").toUpperCase()}
                      </span>
                      <span className="mono">{formatUtcTimestamp(item.timestamp_utc)}</span>
                      <span className="pill pill-neutral">{item.service}</span>
                      <span className="api-meta">
                        {item.relation} (hop {item.hop_distance})
                      </span>
                      {item.minutes_relative_to_window_start !== null && (
                        <span className="pill pill-accent">
                          {item.minutes_relative_to_window_start >= 0 ? "+" : ""}
                          {item.minutes_relative_to_window_start.toFixed(1)}m vs window start
                        </span>
                      )}
                    </div>
                    <div>{item.summary}</div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </>
      )}

      <section className="card">
        <div className="card-header">
          <div>
            <h3 className="card-title">Recorded Incidents</h3>
            <p className="card-subtitle">
              Live GET /api/v1/incidents — click Correlate to inspect an incident window
            </p>
          </div>
          <span className="pill pill-neutral">{incidents.length} incidents</span>
        </div>

        {incidents.length === 0 ? (
          <div className="empty-state">
            <div>No incidents recorded in PostgreSQL yet.</div>
            <code>.venv\Scripts\python scripts/seed_demo_scenario.py</code>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Started (UTC)</th>
                  <th>Service</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Title</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {incidents.map((inc) => (
                  <tr key={inc.id}>
                    <td className="mono">{formatUtcTimestamp(inc.started_at)}</td>
                    <td className="mono">{inc.service_name ?? "—"}</td>
                    <td>
                      <span
                        className={
                          (inc.severity ?? "").toUpperCase() === "CRITICAL" ||
                          (inc.severity ?? "").toUpperCase() === "ERROR"
                            ? "pill pill-danger"
                            : "pill pill-warn"
                        }
                      >
                        {inc.severity}
                      </span>
                    </td>
                    <td>
                      <span
                        className={
                          inc.status === "open" ? "pill pill-warn" : "pill pill-ok"
                        }
                      >
                        {inc.status}
                      </span>
                    </td>
                    <td>{inc.title}</td>
                    <td>
                      <button
                        type="button"
                        className="btn"
                        disabled={!inc.service_name}
                        onClick={() => handleUseIncident(inc)}
                      >
                        Correlate
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
