import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  formatUtcTimestamp,
  listAnomalies,
  listEvents,
  listIncidents,
  listInvestigations,
  listServices,
} from "../services/api";
import type {
  AnomalyRead,
  EventRead,
  HealthResponse,
  IncidentRead,
  InvestigationRead,
} from "../types/api";

interface OverviewData {
  servicesTotal: number;
  eventsTotal: number;
  anomaliesTotal: number;
  openIncidentsTotal: number;
  investigationsTotal: number;
  recentAnomalies: AnomalyRead[];
  recentEvents: EventRead[];
  openIncidents: IncidentRead[];
  recentInvestigations: InvestigationRead[];
}

interface OverviewPageProps {
  health: HealthResponse | null;
  healthError: string | null;
}

function severityPillClass(severity: string | null | undefined): string {
  const upper = (severity ?? "").toUpperCase();
  if (upper === "CRITICAL" || upper === "ERROR") {
    return "pill pill-danger";
  }
  if (upper === "WARNING") {
    return "pill pill-warn";
  }
  return "pill pill-neutral";
}

export function OverviewPage({ health, healthError }: OverviewPageProps) {
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadOverview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [servicesRes, eventsRes, anomaliesRes, incidentsRes, investigationsRes] =
        await Promise.all([
          listServices({ limit: 50 }),
          listEvents({ limit: 15 }),
          listAnomalies({ is_anomaly: true, limit: 15 }),
          listIncidents({ status: "open", limit: 15 }),
          listInvestigations({ limit: 10 }),
        ]);

      setData({
        servicesTotal: servicesRes.total,
        eventsTotal: eventsRes.total,
        anomaliesTotal: anomaliesRes.total,
        openIncidentsTotal: incidentsRes.total,
        investigationsTotal: investigationsRes.total,
        recentAnomalies: anomaliesRes.items,
        recentEvents: eventsRes.items,
        openIncidents: incidentsRes.items,
        recentInvestigations: investigationsRes.items,
      });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.detail);
      } else {
        setError("Failed to load operations overview from API.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  return (
    <>
      <div className="card-header" style={{ marginBottom: 0 }}>
        <div>
          <h2 className="card-title">Live Operations Snapshot</h2>
          <p className="card-subtitle">
            All metrics and tables reflect live PostgreSQL state from the SignalForge API.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void loadOverview()} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh Overview"}
        </button>
      </div>

      {(error || healthError) && (
        <div className="banner banner-error" role="alert">
          <span>{healthError ?? error}</span>
          <button type="button" className="btn" onClick={() => void loadOverview()}>
            Retry
          </button>
        </div>
      )}

      <section className="kpi-grid" aria-label="Operational KPIs">
        <div className="card">
          <div className="kpi-label">Registered Services</div>
          <div className="kpi-value">{data ? data.servicesTotal : "—"}</div>
          <div className="kpi-foot">GET /api/v1/services</div>
        </div>

        <div className="card">
          <div className="kpi-label">Ingested Events</div>
          <div className="kpi-value">{data ? data.eventsTotal : "—"}</div>
          <div className="kpi-foot">GET /api/v1/events</div>
        </div>

        <div className="card">
          <div className="kpi-label">Detected Anomalies</div>
          <div className="kpi-value">{data ? data.anomaliesTotal : "—"}</div>
          <div className="kpi-foot">is_anomaly = true</div>
        </div>

        <div className="card">
          <div className="kpi-label">Open Incidents</div>
          <div className="kpi-value">{data ? data.openIncidentsTotal : "—"}</div>
          <div className="kpi-foot">status = open</div>
        </div>

        <div className="card">
          <div className="kpi-label">Investigations</div>
          <div className="kpi-value">{data ? data.investigationsTotal : "—"}</div>
          <div className="kpi-foot">GET /api/v1/investigations</div>
        </div>

        <div className="card">
          <div className="kpi-label">System Health</div>
          <div className="kpi-value" style={{ fontSize: 18, marginTop: 12 }}>
            {health && typeof health.status === "string"
              ? `${health.status.toUpperCase()} / ${health.database ?? "unknown"}`
              : "UNAVAILABLE"}
          </div>
          <div className="kpi-foot">GET /health</div>
        </div>
      </section>

      <div className="two-col">
        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">Recent Detected Anomalies</h3>
              <p className="card-subtitle">Latest points exceeding calibrated detector threshold</p>
            </div>
            <span className="pill pill-accent">{data?.recentAnomalies.length ?? 0} shown</span>
          </div>

          {!data || data.recentAnomalies.length === 0 ? (
            <div className="empty-state">
              <div>No detected anomalies recorded yet.</div>
              <code>Open Telemetry &amp; Anomalies to run detection over ingested metrics</code>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Timestamp (UTC)</th>
                    <th>Service</th>
                    <th>Detector</th>
                    <th>Score / Thresh</th>
                    <th>Top Feature</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recentAnomalies.map((item) => (
                    <tr key={item.id}>
                      <td className="mono">{formatUtcTimestamp(item.timestamp_utc)}</td>
                      <td className="mono">{item.service_name}</td>
                      <td className="mono">{item.detector_name}</td>
                      <td className="mono">
                        <span className="pill pill-danger">
                          {item.score.toFixed(3)} / {item.threshold.toFixed(3)}
                        </span>
                      </td>
                      <td className="mono">{item.details?.top_feature ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">Recent Events</h3>
              <p className="card-subtitle">Latest structured operational log events</p>
            </div>
            <span className="pill pill-neutral">{data?.recentEvents.length ?? 0} shown</span>
          </div>

          {!data || data.recentEvents.length === 0 ? (
            <div className="empty-state">
              <div>No events ingested yet.</div>
              <code>.venv\Scripts\python scripts/ingest_otel_capture.py --input-dir data/samples/otel</code>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Timestamp (UTC)</th>
                    <th>Service</th>
                    <th>Severity</th>
                    <th>Message</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recentEvents.map((ev) => (
                    <tr key={ev.id}>
                      <td className="mono">{formatUtcTimestamp(ev.timestamp_utc)}</td>
                      <td className="mono">{ev.service_name}</td>
                      <td>
                        <span className={severityPillClass(ev.severity)}>{ev.severity}</span>
                      </td>
                      <td>{ev.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {data && (data.openIncidents.length > 0 || data.recentInvestigations.length > 0) && (
        <div className="two-col">
          <section className="card">
            <div className="card-header">
              <h3 className="card-title">Open Incidents</h3>
              <span className="pill pill-warn">{data.openIncidents.length} open</span>
            </div>
            {data.openIncidents.length === 0 ? (
              <div className="empty-state">No open incidents.</div>
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Started (UTC)</th>
                      <th>Service</th>
                      <th>Severity</th>
                      <th>Title</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.openIncidents.map((inc) => (
                      <tr key={inc.id}>
                        <td className="mono">{formatUtcTimestamp(inc.started_at)}</td>
                        <td className="mono">{inc.service_name ?? "—"}</td>
                        <td>
                          <span className={severityPillClass(inc.severity)}>{inc.severity}</span>
                        </td>
                        <td>{inc.title}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card">
            <div className="card-header">
              <h3 className="card-title">Recent Investigations</h3>
              <span className="pill pill-accent">{data.recentInvestigations.length} stored</span>
            </div>
            {data.recentInvestigations.length === 0 ? (
              <div className="empty-state">No investigations persisted yet.</div>
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Created (UTC)</th>
                      <th>Service</th>
                      <th>Status</th>
                      <th>Model</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recentInvestigations.map((inv) => (
                      <tr key={inv.id}>
                        <td className="mono">{formatUtcTimestamp(inv.created_at)}</td>
                        <td className="mono">{inv.service_name}</td>
                        <td>
                          <span
                            className={inv.abstained ? "pill pill-warn" : "pill pill-ok"}
                          >
                            {inv.abstained ? "Abstained" : "Grounded"}
                          </span>
                        </td>
                        <td className="mono">{inv.model_name}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}
