import type {
  AnomalyDetectRequest,
  AnomalyDetectResponse,
  AnomalyListResponse,
  CorrelationTimelineRequest,
  CorrelationTimelineResponse,
  DependencyGraphResponse,
  DeploymentListResponse,
  EventListResponse,
  HealthResponse,
  IncidentListResponse,
  InvestigationListResponse,
  InvestigationRead,
  InvestigationRunRequest,
  KnowledgeIndexRequest,
  KnowledgeIndexResponse,
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  MetricListResponse,
  ServiceImpactResponse,
  ServiceListResponse,
  TraceListResponse,
} from "../types/api";

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      qs.set(key, String(value));
    }
  }
  const encoded = qs.toString();
  return encoded ? `?${encoded}` : "";
}

function extractErrorDetail(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") {
    return fallback;
  }
  const record = payload as Record<string, unknown>;
  if (typeof record.detail === "string" && record.detail.trim()) {
    return record.detail;
  }
  if (Array.isArray(record.detail)) {
    const messages = record.detail
      .map((entry) => {
        if (entry && typeof entry === "object") {
          const obj = entry as Record<string, unknown>;
          const loc = Array.isArray(obj.loc) ? obj.loc.join(".") : "";
          const msg = typeof obj.msg === "string" ? obj.msg : "";
          return loc && msg ? `${loc}: ${msg}` : msg || loc;
        }
        return String(entry);
      })
      .filter(Boolean);
    if (messages.length > 0) {
      return messages.join("; ");
    }
  }
  return fallback;
}

function isHealthResponse(payload: unknown): payload is HealthResponse {
  if (!payload || typeof payload !== "object") {
    return false;
  }
  const record = payload as Record<string, unknown>;
  return typeof record.status === "string" && typeof record.database === "string";
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE_URL}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Network request failed";
    throw new ApiError(0, `Cannot reach SignalForge API (${API_BASE_URL}): ${message}`);
  }

  let parsed: unknown = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) {
    const fallback = `HTTP ${response.status} ${response.statusText}`.trim();
    const detail = extractErrorDetail(parsed, fallback);
    const message =
      response.status === 404
        ? `HTTP 404 on ${path}: ${detail}. Verify the SignalForge FastAPI backend is running on ${API_BASE_URL}.`
        : detail;
    throw new ApiError(response.status, message);
  }

  if (parsed === null || typeof parsed !== "object") {
    throw new ApiError(
      response.status,
      `Unexpected non-JSON response from ${url} (HTTP ${response.status}). Verify the SignalForge FastAPI backend is running on ${API_BASE_URL}.`,
    );
  }

  return parsed as T;
}

export async function getHealth(): Promise<HealthResponse> {
  const url = `${API_BASE_URL}/health`;
  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" } });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Network error";
    throw new ApiError(0, `API unreachable (${API_BASE_URL}): ${message}`);
  }

  let parsed: unknown = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
  }

  // SignalForge /health returns 200 (ok/connected) or 503 (degraded/disconnected).
  if (isHealthResponse(parsed) && (response.ok || response.status === 503)) {
    return parsed;
  }

  const fallback = `HTTP ${response.status} ${response.statusText}`.trim();
  const detail = extractErrorDetail(parsed, fallback);
  throw new ApiError(
    response.status,
    `SignalForge health check failed at ${url} (HTTP ${response.status}: ${detail}). Ensure the SignalForge FastAPI backend is running on ${API_BASE_URL}.`,
  );
}

export function listServices(params?: {
  limit?: number;
  offset?: number;
}): Promise<ServiceListResponse> {
  return requestJson<ServiceListResponse>(`/api/v1/services${buildQuery(params ?? {})}`);
}

export function listEvents(params?: {
  service?: string;
  severity?: string;
  event_type?: string;
  trace_id?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}): Promise<EventListResponse> {
  return requestJson<EventListResponse>(`/api/v1/events${buildQuery(params ?? {})}`);
}

export function listIncidents(params?: {
  service?: string;
  status?: string;
  severity?: string;
  limit?: number;
  offset?: number;
}): Promise<IncidentListResponse> {
  return requestJson<IncidentListResponse>(`/api/v1/incidents${buildQuery(params ?? {})}`);
}

export function listMetrics(params?: {
  service?: string;
  metric_name?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}): Promise<MetricListResponse> {
  return requestJson<MetricListResponse>(`/api/v1/metrics${buildQuery(params ?? {})}`);
}

export function listTraces(params?: {
  service?: string;
  trace_id?: string;
  status_code?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}): Promise<TraceListResponse> {
  return requestJson<TraceListResponse>(`/api/v1/traces${buildQuery(params ?? {})}`);
}

export function listDeployments(params?: {
  service?: string;
  status?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}): Promise<DeploymentListResponse> {
  return requestJson<DeploymentListResponse>(`/api/v1/deployments${buildQuery(params ?? {})}`);
}

export function listAnomalies(params?: {
  service?: string;
  detector?: string;
  is_anomaly?: boolean;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}): Promise<AnomalyListResponse> {
  return requestJson<AnomalyListResponse>(`/api/v1/anomalies${buildQuery(params ?? {})}`);
}

export function detectAnomalies(payload: AnomalyDetectRequest): Promise<AnomalyDetectResponse> {
  return requestJson<AnomalyDetectResponse>("/api/v1/anomalies/detect", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getDependencyGraph(params?: {
  start_time?: string;
  end_time?: string;
}): Promise<DependencyGraphResponse> {
  return requestJson<DependencyGraphResponse>(
    `/api/v1/graph/dependencies${buildQuery(params ?? {})}`,
  );
}

export function getServiceImpact(
  service: string,
  params?: {
    max_depth?: number;
    start_time?: string;
    end_time?: string;
  },
): Promise<ServiceImpactResponse> {
  const encoded = encodeURIComponent(service);
  return requestJson<ServiceImpactResponse>(
    `/api/v1/graph/services/${encoded}/impact${buildQuery(params ?? {})}`,
  );
}

export function postCorrelationTimeline(
  payload: CorrelationTimelineRequest,
): Promise<CorrelationTimelineResponse> {
  return requestJson<CorrelationTimelineResponse>("/api/v1/correlation/timeline", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function indexKnowledge(payload?: KnowledgeIndexRequest): Promise<KnowledgeIndexResponse> {
  return requestJson<KnowledgeIndexResponse>("/api/v1/knowledge/index", {
    method: "POST",
    body: JSON.stringify(payload ?? {}),
  });
}

export function searchKnowledge(payload: KnowledgeSearchRequest): Promise<KnowledgeSearchResponse> {
  return requestJson<KnowledgeSearchResponse>("/api/v1/knowledge/search", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function runInvestigation(payload: InvestigationRunRequest): Promise<InvestigationRead> {
  return requestJson<InvestigationRead>("/api/v1/investigations/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listInvestigations(params?: {
  service?: string;
  abstained?: boolean;
  limit?: number;
  offset?: number;
}): Promise<InvestigationListResponse> {
  return requestJson<InvestigationListResponse>(
    `/api/v1/investigations${buildQuery(params ?? {})}`,
  );
}

export function getInvestigation(investigationId: string): Promise<InvestigationRead> {
  return requestJson<InvestigationRead>(
    `/api/v1/investigations/${encodeURIComponent(investigationId)}`,
  );
}

export function formatUtcTimestamp(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}
