import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ApiError,
  detectAnomalies,
  formatUtcTimestamp,
  listAnomalies,
  listMetrics,
  listServices,
} from "../services/api";
import type {
  AnomalyDetectResponse,
  AnomalyRead,
  DetectorName,
  MetricPointRead,
  ServiceSummary,
} from "../types/api";

type TimeRangePreset = "all" | "1h" | "6h" | "24h" | "7d" | "custom";

const CANONICAL_METRICS = [
  "cpu_utilization",
  "memory_utilization",
  "request_latency_ms",
  "request_throughput",
  "error_rate",
  "db_connections",
  "queue_depth",
];

const DETECTORS: { value: DetectorName; label: string }[] = [
  { value: "lstm_autoencoder", label: "LSTM Autoencoder" },
  { value: "isolation_forest", label: "Isolation Forest" },
  { value: "rolling_zscore", label: "Rolling Z-Score" },
];

function computeWindowBounds(
  preset: TimeRangePreset,
  customStart: string,
  customEnd: string,
): { startTime?: string; endTime?: string } {
  if (preset === "all") {
    return {};
  }
  if (preset === "custom") {
    return {
      startTime: customStart.trim() || undefined,
      endTime: customEnd.trim() || undefined,
    };
  }
  const now = new Date();
  const hoursMap: Record<Exclude<TimeRangePreset, "all" | "custom">, number> = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
    "7d": 24 * 7,
  };
  const start = new Date(now.getTime() - hoursMap[preset] * 3600 * 1000);
  return { startTime: start.toISOString(), endTime: now.toISOString() };
}

function shortTimeLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return d.toISOString().slice(11, 19);
}

interface MetricChartRow {
  timestamp: string;
  label: string;
  value: number;
  anomalyMarker: number | null;
}

interface ScoreChartRow {
  timestamp: string;
  label: string;
  score: number;
  threshold: number;
  isAnomaly: boolean;
}

export function TelemetryAnomaliesPage() {
  const [services, setServices] = useState<ServiceSummary[]>([]);
  const [selectedService, setSelectedService] = useState<string>("");
  const [selectedMetric, setSelectedMetric] = useState<string>("cpu_utilization");
  const [availableMetrics, setAvailableMetrics] = useState<string[]>(CANONICAL_METRICS);
  const [timePreset, setTimePreset] = useState<TimeRangePreset>("all");
  const [customStart, setCustomStart] = useState<string>("");
  const [customEnd, setCustomEnd] = useState<string>("");
  const [detector, setDetector] = useState<DetectorName>("lstm_autoencoder");

  const [metrics, setMetrics] = useState<MetricPointRead[]>([]);
  const [allServiceMetrics, setAllServiceMetrics] = useState<MetricPointRead[]>([]);
  const [anomalies, setAnomalies] = useState<AnomalyRead[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [detecting, setDetecting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [detectBanner, setDetectBanner] = useState<AnomalyDetectResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    void listServices({ limit: 100 })
      .then((res) => {
        if (cancelled) {
          return;
        }
        setServices(res.items);
        if (res.items.length > 0) {
          setSelectedService((prev) => prev || res.items[0].name);
        } else {
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.detail : "Failed to load services.");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadTelemetryAndAnomalies = useCallback(async () => {
    if (!selectedService) {
      setMetrics([]);
      setAllServiceMetrics([]);
      setAnomalies([]);
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    const { startTime, endTime } = computeWindowBounds(timePreset, customStart, customEnd);

    try {
      const [serviceMetricsRes, anomaliesRes] = await Promise.all([
        listMetrics({
          service: selectedService,
          start_time: startTime,
          end_time: endTime,
          limit: 500,
        }),
        listAnomalies({
          service: selectedService,
          detector,
          start_time: startTime,
          end_time: endTime,
          limit: 500,
        }),
      ]);

      const discoveredNames = Array.from(
        new Set(serviceMetricsRes.items.map((pt) => pt.metric_name)),
      ).sort();
      const mergedNames = Array.from(new Set([...discoveredNames, ...CANONICAL_METRICS]));
      setAvailableMetrics(mergedNames);
      setAllServiceMetrics(serviceMetricsRes.items);

      let activeMetric = selectedMetric;
      if (discoveredNames.length > 0 && !discoveredNames.includes(selectedMetric)) {
        activeMetric = discoveredNames[0];
        setSelectedMetric(activeMetric);
      }

      const metricSpecific = serviceMetricsRes.items.filter(
        (pt) => pt.metric_name === activeMetric,
      );
      setMetrics(metricSpecific);
      setAnomalies(anomaliesRes.items);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.detail
          : "Failed to load telemetry metrics or anomalies.",
      );
    } finally {
      setLoading(false);
    }
  }, [selectedService, selectedMetric, timePreset, customStart, customEnd, detector]);

  useEffect(() => {
    void loadTelemetryAndAnomalies();
  }, [loadTelemetryAndAnomalies]);

  const handleRunDetection = async () => {
    if (!selectedService) {
      return;
    }
    setDetecting(true);
    setError(null);
    setDetectBanner(null);

    const { startTime, endTime } = computeWindowBounds(timePreset, customStart, customEnd);
    let detectStart = startTime;
    let detectEnd = endTime;

    if (!detectStart || !detectEnd) {
      const sortedTs = allServiceMetrics
        .map((m) => m.timestamp_utc)
        .sort((a, b) => new Date(a).getTime() - new Date(b).getTime());
      if (sortedTs.length >= 2) {
        detectStart = detectStart ?? sortedTs[0];
        detectEnd = detectEnd ?? sortedTs[sortedTs.length - 1];
      } else {
        const now = new Date();
        detectEnd = detectEnd ?? now.toISOString();
        detectStart = detectStart ?? new Date(now.getTime() - 24 * 3600 * 1000).toISOString();
      }
    }

    try {
      const result = await detectAnomalies({
        service: selectedService,
        start_time: detectStart,
        end_time: detectEnd,
        detector,
        persist_all: false,
      });
      setDetectBanner(result);
      await loadTelemetryAndAnomalies();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Anomaly detection request failed.");
    } finally {
      setDetecting(false);
    }
  };

  const anomalyTimestamps = useMemo(() => {
    const flagged = new Set<string>();
    for (const item of anomalies) {
      if (item.is_anomaly) {
        flagged.add(new Date(item.timestamp_utc).toISOString());
      }
    }
    return flagged;
  }, [anomalies]);

  const metricChartData: MetricChartRow[] = useMemo(() => {
    return [...metrics]
      .sort((a, b) => new Date(a.timestamp_utc).getTime() - new Date(b.timestamp_utc).getTime())
      .map((pt) => {
        const iso = new Date(pt.timestamp_utc).toISOString();
        return {
          timestamp: iso,
          label: shortTimeLabel(iso),
          value: pt.value,
          anomalyMarker: anomalyTimestamps.has(iso) ? pt.value : null,
        };
      });
  }, [metrics, anomalyTimestamps]);

  const scoreChartData: ScoreChartRow[] = useMemo(() => {
    return [...anomalies]
      .sort((a, b) => new Date(a.timestamp_utc).getTime() - new Date(b.timestamp_utc).getTime())
      .map((item) => {
        const iso = new Date(item.timestamp_utc).toISOString();
        return {
          timestamp: iso,
          label: shortTimeLabel(iso),
          score: Number(item.score.toFixed(4)),
          threshold: Number(item.threshold.toFixed(4)),
          isAnomaly: item.is_anomaly,
        };
      });
  }, [anomalies]);

  const activeThreshold =
    scoreChartData.length > 0 ? scoreChartData[scoreChartData.length - 1].threshold : null;

  return (
    <>
      <section className="card">
        <div className="toolbar">
          <div className="field">
            <label htmlFor="telemetry-service">Service</label>
            <select
              id="telemetry-service"
              value={selectedService}
              onChange={(e) => {
                setSelectedService(e.target.value);
                setDetectBanner(null);
              }}
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

          <div className="field">
            <label htmlFor="telemetry-metric">Metric</label>
            <select
              id="telemetry-metric"
              value={selectedMetric}
              onChange={(e) => setSelectedMetric(e.target.value)}
            >
              {availableMetrics.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="telemetry-range">Time Range</label>
            <select
              id="telemetry-range"
              value={timePreset}
              onChange={(e) => setTimePreset(e.target.value as TimeRangePreset)}
            >
              <option value="all">All ingested points</option>
              <option value="1h">Last 1 hour</option>
              <option value="6h">Last 6 hours</option>
              <option value="24h">Last 24 hours</option>
              <option value="7d">Last 7 days</option>
              <option value="custom">Custom UTC window</option>
            </select>
          </div>

          {timePreset === "custom" && (
            <>
              <div className="field">
                <label htmlFor="custom-start">Start (UTC ISO)</label>
                <input
                  id="custom-start"
                  type="text"
                  placeholder="2026-11-20T12:00:00Z"
                  value={customStart}
                  onChange={(e) => setCustomStart(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="custom-end">End (UTC ISO)</label>
                <input
                  id="custom-end"
                  type="text"
                  placeholder="2026-11-20T13:00:00Z"
                  value={customEnd}
                  onChange={(e) => setCustomEnd(e.target.value)}
                />
              </div>
            </>
          )}

          <div className="field">
            <label htmlFor="telemetry-detector">Detector</label>
            <select
              id="telemetry-detector"
              value={detector}
              onChange={(e) => {
                setDetector(e.target.value as DetectorName);
                setDetectBanner(null);
              }}
            >
              {DETECTORS.map((d) => (
                <option key={d.value} value={d.value}>
                  {d.label}
                </option>
              ))}
            </select>
          </div>

          <button
            type="button"
            className="btn"
            onClick={() => void loadTelemetryAndAnomalies()}
            disabled={loading || !selectedService}
          >
            {loading ? "Loading..." : "Refresh"}
          </button>

          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void handleRunDetection()}
            disabled={detecting || !selectedService}
          >
            {detecting ? "Detecting..." : "Run Anomaly Detection"}
          </button>
        </div>
      </section>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>{error}</span>
        </div>
      )}

      {detectBanner && (
        <div className="banner banner-ok" role="status">
          <span>
            <strong>{detectBanner.detector}</strong> evaluated{" "}
            <span className="mono">{detectBanner.points_evaluated}</span> aligned points on{" "}
            <span className="mono">{detectBanner.service}</span>: detected{" "}
            <strong>{detectBanner.anomalies_detected}</strong> anomalies (persisted{" "}
            {detectBanner.persisted}, duplicates {detectBanner.duplicates}, max score{" "}
            <span className="mono">{detectBanner.max_score.toFixed(4)}</span>, threshold{" "}
            <span className="mono">{detectBanner.threshold.toFixed(4)}</span>).
          </span>
        </div>
      )}

      <div className="two-col">
        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">
                Metric Time Series: <span className="mono">{selectedMetric}</span>
              </h3>
              <p className="card-subtitle">
                Red markers highlight timestamps flagged as anomalies by {detector}
              </p>
            </div>
            <span className="pill pill-accent">{metricChartData.length} points</span>
          </div>

          {metricChartData.length === 0 ? (
            <div className="empty-state">
              <div>
                No metric points found for <span className="mono">{selectedMetric}</span>
                {selectedService ? ` on ${selectedService}` : ""}.
              </div>
              <code>Ingest metrics via POST /api/v1/metrics/ingest or /ingest/csv</code>
            </div>
          ) : (
            <div className="chart-box">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart
                  data={metricChartData}
                  margin={{ top: 10, right: 16, left: 4, bottom: 8 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="label" stroke="#94a3b8" fontSize={11} />
                  <YAxis stroke="#94a3b8" fontSize={11} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: "#0f172a",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                    labelFormatter={(_label, payload) =>
                      payload && payload[0]
                        ? formatUtcTimestamp(String(payload[0].payload.timestamp))
                        : ""
                    }
                  />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="value"
                    name={selectedMetric}
                    stroke="#38bdf8"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                  />
                  <Scatter
                    dataKey="anomalyMarker"
                    name="Anomaly Flag"
                    fill="#ef4444"
                    isAnimationActive={false}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>

        <section className="card">
          <div className="card-header">
            <div>
              <h3 className="card-title">
                Anomaly Score vs. Threshold (<span className="mono">{detector}</span>)
              </h3>
              <p className="card-subtitle">
                Persisted anomaly records from GET /api/v1/anomalies
              </p>
            </div>
            <span className="pill pill-danger">
              {anomalies.filter((a) => a.is_anomaly).length} flagged
            </span>
          </div>

          {scoreChartData.length === 0 ? (
            <div className="empty-state">
              <div>No persisted anomaly scores for this service and detector yet.</div>
              <code>Click "Run Anomaly Detection" once aligned 7-feature metrics are ingested</code>
            </div>
          ) : (
            <div className="chart-box">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart
                  data={scoreChartData}
                  margin={{ top: 10, right: 16, left: 4, bottom: 8 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="label" stroke="#94a3b8" fontSize={11} />
                  <YAxis stroke="#94a3b8" fontSize={11} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: "#0f172a",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                    labelFormatter={(_label, payload) =>
                      payload && payload[0]
                        ? formatUtcTimestamp(String(payload[0].payload.timestamp))
                        : ""
                    }
                  />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="score"
                    name="Anomaly Score"
                    stroke="#f59e0b"
                    strokeWidth={2}
                    dot={{ r: 3, fill: "#ef4444" }}
                    isAnimationActive={false}
                  />
                  {activeThreshold !== null && (
                    <ReferenceLine
                      y={activeThreshold}
                      stroke="#ef4444"
                      strokeDasharray="5 5"
                      label={{
                        value: `Threshold (${activeThreshold.toFixed(3)})`,
                        fill: "#fca5a5",
                        fontSize: 11,
                        position: "insideTopRight",
                      }}
                    />
                  )}
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>
      </div>

      <section className="card">
        <div className="card-header">
          <div>
            <h3 className="card-title">Anomalies Table</h3>
            <p className="card-subtitle">
              Score, calibrated threshold, and highest-magnitude standardized feature
            </p>
          </div>
          <span className="pill pill-neutral">{anomalies.length} records</span>
        </div>

        {anomalies.length === 0 ? (
          <div className="empty-state">No anomaly rows matching current filters.</div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Timestamp (UTC)</th>
                  <th>Service</th>
                  <th>Detector</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th>Threshold</th>
                  <th>Top Feature</th>
                </tr>
              </thead>
              <tbody>
                {anomalies.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{formatUtcTimestamp(row.timestamp_utc)}</td>
                    <td className="mono">{row.service_name}</td>
                    <td className="mono">{row.detector_name}</td>
                    <td>
                      <span className={row.is_anomaly ? "pill pill-danger" : "pill pill-ok"}>
                        {row.is_anomaly ? "Anomaly" : "Normal"}
                      </span>
                    </td>
                    <td className="mono">{row.score.toFixed(4)}</td>
                    <td className="mono">{row.threshold.toFixed(4)}</td>
                    <td className="mono">{row.details?.top_feature ?? "—"}</td>
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
