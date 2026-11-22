import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  formatUtcTimestamp,
  getDependencyGraph,
  getServiceImpact,
  listServices,
} from "../services/api";
import type {
  DependencyGraphResponse,
  ServiceImpactResponse,
  ServiceSummary,
} from "../types/api";

export function ServicesPage() {
  const [services, setServices] = useState<ServiceSummary[]>([]);
  const [graph, setGraph] = useState<DependencyGraphResponse | null>(null);
  const [selectedService, setSelectedService] = useState<string | null>(null);
  const [maxDepth, setMaxDepth] = useState<number>(3);
  const [impact, setImpact] = useState<ServiceImpactResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [impactLoading, setImpactLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [servicesRes, graphRes] = await Promise.all([
        listServices({ limit: 100 }),
        getDependencyGraph(),
      ]);
      setServices(servicesRes.items);
      setGraph(graphRes);

      setSelectedService((prev) => {
        if (prev && servicesRes.items.some((s) => s.name === prev)) {
          return prev;
        }
        return servicesRes.items.length > 0 ? servicesRes.items[0].name : null;
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to load services and dependencies.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (!selectedService) {
      setImpact(null);
      setImpactError(null);
      return;
    }
    let cancelled = false;
    setImpactLoading(true);
    setImpactError(null);

    void getServiceImpact(selectedService, { max_depth: maxDepth })
      .then((res) => {
        if (!cancelled) {
          setImpact(res);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setImpact(null);
          setImpactError(
            err instanceof ApiError ? err.detail : "Failed to load service impact analysis.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setImpactLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [selectedService, maxDepth]);

  return (
    <>
      <div className="card-header" style={{ marginBottom: 0 }}>
        <div>
          <h2 className="card-title">Registered Services &amp; Dependency Topology</h2>
          <p className="card-subtitle">
            Select any service to inspect multi-hop downstream dependencies and upstream callers.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void loadData()} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>{error}</span>
          <button type="button" className="btn" onClick={() => void loadData()}>
            Retry
          </button>
        </div>
      )}

      <section className="card">
        <div className="card-header">
          <div>
            <h3 className="card-title">Registered Services</h3>
            <p className="card-subtitle">Click a row to load its live impact graph</p>
          </div>
          <span className="pill pill-accent">{services.length} services</span>
        </div>

        {services.length === 0 ? (
          <div className="empty-state">
            <div>No services registered yet.</div>
            <code>.venv\Scripts\python scripts/ingest_otel_capture.py --input-dir data/samples/otel</code>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Environment</th>
                  <th>Discovery</th>
                  <th>Event Count</th>
                  <th>Last Seen (UTC)</th>
                  <th>Created (UTC)</th>
                </tr>
              </thead>
              <tbody>
                {services.map((svc) => (
                  <tr
                    key={svc.id}
                    className={`clickable${selectedService === svc.name ? " selected" : ""}`}
                    onClick={() => setSelectedService(svc.name)}
                  >
                    <td className="mono">{svc.name}</td>
                    <td className="mono">{svc.environment}</td>
                    <td>
                      <span
                        className={
                          svc.is_auto_discovered ? "pill pill-accent" : "pill pill-neutral"
                        }
                      >
                        {svc.is_auto_discovered ? "Auto-discovered" : "Registered"}
                      </span>
                    </td>
                    <td className="mono">{svc.event_count}</td>
                    <td className="mono">{formatUtcTimestamp(svc.last_seen_at)}</td>
                    <td className="mono">{formatUtcTimestamp(svc.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="two-col">
        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">Service Dependency Edges</h3>
              <p className="card-subtitle">
                Directed caller → callee links aggregated from parent/child trace spans
              </p>
            </div>
            <span className="pill pill-neutral">
              {graph ? `${graph.node_count} nodes · ${graph.edge_count} edges` : "—"}
            </span>
          </div>

          {!graph || graph.edges.length === 0 ? (
            <div className="empty-state">
              <div>No trace-derived service dependency edges observed yet.</div>
              <code>Ingest parent/child spans via POST /api/v1/traces/ingest</code>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Caller</th>
                    <th>Callee</th>
                    <th>Calls</th>
                    <th>Error Rate</th>
                    <th>Avg Latency</th>
                  </tr>
                </thead>
                <tbody>
                  {graph.edges.map((edge) => (
                    <tr key={`${edge.caller}->${edge.callee}`}>
                      <td>
                        <button
                          type="button"
                          className="btn mono"
                          onClick={() => setSelectedService(edge.caller)}
                        >
                          {edge.caller}
                        </button>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn mono"
                          onClick={() => setSelectedService(edge.callee)}
                        >
                          {edge.callee}
                        </button>
                      </td>
                      <td className="mono">{edge.call_count}</td>
                      <td>
                        <span
                          className={
                            edge.error_rate > 0 ? "pill pill-danger" : "pill pill-ok"
                          }
                        >
                          {(edge.error_rate * 100).toFixed(1)}% ({edge.error_count})
                        </span>
                      </td>
                      <td className="mono">{edge.avg_duration_ms.toFixed(1)} ms</td>
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
              <h3 className="card-title">
                Impact Explorer{selectedService ? `: ${selectedService}` : ""}
              </h3>
              <p className="card-subtitle">
                Live GET /api/v1/graph/services/&#123;service&#125;/impact traversal
              </p>
            </div>
            <div className="field" style={{ minWidth: 110 }}>
              <label htmlFor="impact-depth">Max Depth</label>
              <select
                id="impact-depth"
                value={maxDepth}
                onChange={(e) => setMaxDepth(Number(e.target.value))}
              >
                {[1, 2, 3, 4, 5].map((depth) => (
                  <option key={depth} value={depth}>
                    {depth} {depth === 1 ? "hop" : "hops"}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {!selectedService ? (
            <div className="empty-state">Select a service above to inspect its impact topology.</div>
          ) : impactLoading ? (
            <div className="empty-state">Computing multi-hop impact for {selectedService}...</div>
          ) : impactError ? (
            <div className="banner banner-error">{impactError}</div>
          ) : impact ? (
            <>
              <div className="mono" style={{ color: "var(--text-secondary)" }}>
                Related services within {impact.max_depth} hop(s):{" "}
                <strong>{impact.all_related_services.length}</strong>
              </div>

              <div className="impact-lists">
                <div className="impact-box">
                  <h4>Downstream Dependencies (Calls)</h4>
                  {impact.dependencies.length === 0 ? (
                    <div className="api-meta">No outgoing dependencies.</div>
                  ) : (
                    <ul className="hop-list">
                      {impact.dependencies.map((dep) => (
                        <li key={dep.service} className="hop-item">
                          <button
                            type="button"
                            className="btn mono"
                            onClick={() => setSelectedService(dep.service)}
                          >
                            {dep.service}
                          </button>
                          <span className="pill pill-accent">hop {dep.hop_distance}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                <div className="impact-box">
                  <h4>Affected Upstream Callers</h4>
                  {impact.affected_callers.length === 0 ? (
                    <div className="api-meta">No upstream callers.</div>
                  ) : (
                    <ul className="hop-list">
                      {impact.affected_callers.map((caller) => (
                        <li key={caller.service} className="hop-item">
                          <button
                            type="button"
                            className="btn mono"
                            onClick={() => setSelectedService(caller.service)}
                          >
                            {caller.service}
                          </button>
                          <span className="pill pill-warn">hop {caller.hop_distance}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </>
          ) : null}
        </section>
      </div>
    </>
  );
}
