export interface HealthResponse {
  status: "ok" | "degraded";
  database: "connected" | "disconnected";
}

export interface ServiceSummary {
  id: string;
  name: string;
  environment: string;
  owner_team: string | null;
  is_auto_discovered: boolean;
  event_count: number;
  last_seen_at: string | null;
  created_at: string;
}

export interface ServiceListResponse {
  total: number;
  limit: number;
  offset: number;
  items: ServiceSummary[];
}

export interface EventRead {
  id: string;
  service_id: string;
  service_name: string;
  external_id: string | null;
  dedup_hash: string;
  timestamp_utc: string;
  event_type: string;
  severity: string;
  message: string;
  trace_id: string | null;
  span_id: string | null;
  attributes: Record<string, unknown>;
  ingested_at: string;
}

export interface EventListResponse {
  total: number;
  limit: number;
  offset: number;
  items: EventRead[];
}

export interface IncidentRead {
  id: string;
  external_id: string | null;
  title: string;
  description: string | null;
  service_id: string | null;
  service_name: string | null;
  severity: string;
  status: string;
  started_at: string;
  resolved_at: string | null;
  created_at: string;
}

export interface IncidentListResponse {
  total: number;
  limit: number;
  offset: number;
  items: IncidentRead[];
}

export interface MetricPointRead {
  id: string;
  service_id: string;
  service_name: string;
  metric_name: string;
  timestamp_utc: string;
  value: number;
  unit: string | null;
  labels: Record<string, unknown>;
  dedup_hash: string;
  ingested_at: string;
}

export interface MetricListResponse {
  total: number;
  limit: number;
  offset: number;
  items: MetricPointRead[];
}

export interface TraceSpanRead {
  id: string;
  service_id: string;
  service_name: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  operation_name: string;
  span_kind: string;
  status_code: string;
  start_time_utc: string;
  end_time_utc: string;
  duration_ms: number;
  attributes: Record<string, unknown>;
  ingested_at: string;
}

export interface TraceListResponse {
  total: number;
  limit: number;
  offset: number;
  items: TraceSpanRead[];
}

export interface DeploymentRead {
  id: string;
  service_id: string;
  service_name: string;
  version: string;
  commit_sha: string | null;
  status: string;
  deployed_by: string | null;
  deployed_at: string;
  metadata_json: Record<string, unknown>;
}

export interface DeploymentListResponse {
  total: number;
  limit: number;
  offset: number;
  items: DeploymentRead[];
}

export type DetectorName = "lstm_autoencoder" | "isolation_forest" | "rolling_zscore";

export interface AnomalyDetails {
  top_feature?: string;
  feature_values?: Record<string, number>;
  [key: string]: unknown;
}

export interface AnomalyRead {
  id: string;
  service_id: string;
  service_name: string;
  detector_name: DetectorName | string;
  timestamp_utc: string;
  score: number;
  threshold: number;
  is_anomaly: boolean;
  metric_names: string[];
  details: AnomalyDetails;
  dedup_hash: string;
  detected_at: string;
}

export interface AnomalyListResponse {
  total: number;
  limit: number;
  offset: number;
  items: AnomalyRead[];
}

export interface AnomalyDetectRequest {
  service: string;
  start_time: string;
  end_time: string;
  detector: DetectorName;
  persist_all?: boolean;
}

export interface AnomalyDetectResponse {
  service: string;
  detector: DetectorName;
  points_evaluated: number;
  anomalies_detected: number;
  persisted: number;
  duplicates: number;
  threshold: number;
  max_score: number;
  items: AnomalyRead[];
}

export interface ServiceEdge {
  caller: string;
  callee: string;
  call_count: number;
  error_count: number;
  error_rate: number;
  avg_duration_ms: number;
}

export interface DependencyGraphResponse {
  node_count: number;
  edge_count: number;
  nodes: string[];
  edges: ServiceEdge[];
}

export interface HopService {
  service: string;
  hop_distance: number;
}

export interface ServiceImpactResponse {
  target_service: string;
  max_depth: number;
  dependencies: HopService[];
  affected_callers: HopService[];
  all_related_services: string[];
}

export type SignalType = "deployment" | "anomaly" | "event" | "incident";
export type RelationType = "target" | "dependency" | "affected_caller" | "bidirectional";

export interface TimelineItem {
  timestamp_utc: string;
  signal_type: SignalType;
  service: string;
  relation: RelationType;
  hop_distance: number;
  summary: string;
  record_id: string | null;
  minutes_relative_to_window_start: number | null;
  details: Record<string, unknown>;
}

export interface CorrelationTimelineRequest {
  service: string;
  start_time: string;
  end_time: string;
  lookback_minutes?: number;
  max_depth?: number;
}

export interface CorrelationTimelineResponse {
  target_service: string;
  window_start: string;
  window_end: string;
  lookback_minutes: number;
  deployment_window_start: string;
  max_depth: number;
  related_services: string[];
  correlated_services: string[];
  impact: ServiceImpactResponse;
  deployment_count: number;
  anomaly_count: number;
  event_count: number;
  incident_count: number;
  timeline: TimelineItem[];
  correlation_note: string;
}

export type RetrievalMode = "hybrid" | "dense" | "bm25";
export type DocType = "runbook" | "incident" | "architecture" | "deployment";

export interface RetrievedChunk {
  chunk_id: string;
  doc_id: string;
  doc_type: DocType;
  title: string;
  service: string;
  section_heading: string;
  chunk_index: number;
  source_path: string;
  is_synthetic: boolean;
  text: string;
  score: number;
  retrieval_source: RetrievalMode;
}

export interface KnowledgeIndexRequest {
  source_dir?: string;
  max_chars?: number;
  overlap_chars?: number;
}

export interface KnowledgeIndexResponse {
  source_dir: string;
  documents_indexed: number;
  chunks_indexed: number;
  collection: string;
  embedding_model: string;
}

export interface KnowledgeSearchRequest {
  query: string;
  top_k?: number;
  mode?: RetrievalMode;
  service?: string | null;
  doc_type?: DocType | null;
}

export interface KnowledgeSearchResponse {
  query: string;
  mode: RetrievalMode;
  total: number;
  results: RetrievedChunk[];
}

export interface CitationRef {
  doc_id: string;
  doc_type: string;
  title: string;
  section_heading: string;
  source_path: string;
  is_synthetic: boolean;
}

export interface RootCauseHypothesis {
  hypothesis: string;
  confidence: "low" | "medium" | "high";
  supporting_evidence_ids: string[];
  is_proven_causation: false;
}

export interface InvestigationReport {
  question: string;
  target_service: string;
  window_start: string;
  window_end: string;
  model_name: string;
  confirmed_facts: string[];
  detected_anomalies: string[];
  observed_correlations: string[];
  probable_root_causes: RootCauseHypothesis[];
  citations: CitationRef[];
  recommended_next_steps: string[];
  uncertainty_and_gaps: string[];
  abstained: boolean;
  abstention_reason: string | null;
  validation_warnings: string[];
}

export interface InvestigationRunRequest {
  question: string;
  service: string;
  start_time: string;
  end_time: string;
  lookback_minutes?: number;
  max_depth?: number;
  top_k_docs?: number;
}

export interface InvestigationRead {
  id: string;
  service_id: string;
  service_name: string;
  created_at: string;
  executed_nodes: string[];
  duration_ms: number;
  abstained: boolean;
  model_name: string;
  report: InvestigationReport;
}

export interface InvestigationListResponse {
  total: number;
  limit: number;
  offset: number;
  items: InvestigationRead[];
}
