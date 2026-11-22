from collections.abc import Sequence
from datetime import datetime, timedelta

import networkx as nx

from services.graph.relationships import (
    DEFAULT_CORRELATION_NOTE,
    AnomalyRecord,
    CorrelationSummary,
    DeploymentRecord,
    EventRecord,
    HopService,
    ImpactAnalysis,
    IncidentRecord,
    RelationType,
    SignalType,
    TimelineItem,
)
from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc


def analyze_service_impact(
    graph: nx.DiGraph,
    service: str,
    max_depth: int = 3,
) -> ImpactAnalysis:
    """Compute cycle-safe multi-hop dependencies and affected callers around a service.

    In a `caller -> callee` graph:
    - `dependencies` are outgoing services reachable from `service` (what `service` calls).
    - `affected_callers` are incoming services that can reach `service` (what calls `service`).
    """
    if max_depth < 1:
        raise ValueError(f"max_depth must be at least 1, got {max_depth}")

    target = normalize_service_name(service)
    if target not in graph:
        raise ValueError(f"Service {target!r} is not present in the dependency graph")

    dep_lengths = nx.single_source_shortest_path_length(graph, target, cutoff=max_depth)
    dependencies = sorted(
        (
            HopService(service=str(node), hop_distance=int(dist))
            for node, dist in dep_lengths.items()
            if node != target
        ),
        key=lambda item: (item.hop_distance, item.service),
    )

    reversed_graph = graph.reverse(copy=False)
    caller_lengths = nx.single_source_shortest_path_length(
        reversed_graph, target, cutoff=max_depth
    )
    affected_callers = sorted(
        (
            HopService(service=str(node), hop_distance=int(dist))
            for node, dist in caller_lengths.items()
            if node != target
        ),
        key=lambda item: (item.hop_distance, item.service),
    )

    all_related_services = sorted(
        {item.service for item in dependencies} | {item.service for item in affected_callers}
    )

    return ImpactAnalysis(
        target_service=target,
        max_depth=max_depth,
        dependencies=dependencies,
        affected_callers=affected_callers,
        all_related_services=all_related_services,
    )


def _resolve_relation_and_hop(
    service: str,
    target_service: str,
    dep_hops: dict[str, int],
    caller_hops: dict[str, int],
) -> tuple[RelationType, int]:
    if service == target_service:
        return "target", 0
    in_deps = service in dep_hops
    in_callers = service in caller_hops
    if in_deps and in_callers:
        return "bidirectional", min(dep_hops[service], caller_hops[service])
    if in_deps:
        return "dependency", dep_hops[service]
    return "affected_caller", caller_hops[service]


def correlate_timeline(
    *,
    target_service: str,
    window_start: datetime,
    window_end: datetime,
    graph: nx.DiGraph,
    deployments: Sequence[DeploymentRecord] = (),
    anomalies: Sequence[AnomalyRecord] = (),
    events: Sequence[EventRecord] = (),
    incidents: Sequence[IncidentRecord] = (),
    lookback_minutes: int = 60,
    max_depth: int = 3,
    include_non_anomalies: bool = False,
) -> CorrelationSummary:
    """Correlate deployments, anomalies, events, and incidents over related services.

    - Deployments are included in `[window_start - lookback_minutes, window_end]`.
    - Anomalies, events, and incidents are included in `[window_start, window_end]`.
    - Only records belonging to `{target_service} | related_services` (up to `max_depth`)
      are retained, sorted deterministically by `(timestamp_utc, signal_type, service, summary)`.
    """
    start_utc = normalize_timestamp_utc(window_start)
    end_utc = normalize_timestamp_utc(window_end)
    if start_utc > end_utc:
        raise ValueError("window_start must be less than or equal to window_end")
    if lookback_minutes < 0:
        raise ValueError(f"lookback_minutes must be non-negative, got {lookback_minutes}")

    impact = analyze_service_impact(graph, target_service, max_depth=max_depth)
    deployment_window_start = start_utc - timedelta(minutes=lookback_minutes)

    dep_hops = {item.service: item.hop_distance for item in impact.dependencies}
    caller_hops = {item.service: item.hop_distance for item in impact.affected_callers}
    scope_services = {impact.target_service} | set(impact.all_related_services)

    timeline: list[TimelineItem] = []

    for dep in deployments:
        if dep.service not in scope_services:
            continue
        if not (deployment_window_start <= dep.timestamp_utc <= end_utc):
            continue
        relation, hop = _resolve_relation_and_hop(
            dep.service, impact.target_service, dep_hops, caller_hops
        )
        minutes_rel = round((dep.timestamp_utc - start_utc).total_seconds() / 60.0, 4)
        timeline.append(
            TimelineItem(
                timestamp_utc=dep.timestamp_utc,
                signal_type="deployment",
                service=dep.service,
                relation=relation,
                hop_distance=hop,
                summary=f"Deployment {dep.version} ({dep.status}) on {dep.service}",
                record_id=dep.deployment_id,
                minutes_relative_to_window_start=minutes_rel,
                details={
                    "version": dep.version,
                    "status": dep.status,
                    "environment": dep.environment,
                    "commit_sha": dep.commit_sha,
                },
            )
        )

    for anomaly in anomalies:
        if anomaly.service not in scope_services:
            continue
        if not anomaly.is_anomaly and not include_non_anomalies:
            continue
        if not (start_utc <= anomaly.timestamp_utc <= end_utc):
            continue
        relation, hop = _resolve_relation_and_hop(
            anomaly.service, impact.target_service, dep_hops, caller_hops
        )
        timeline.append(
            TimelineItem(
                timestamp_utc=anomaly.timestamp_utc,
                signal_type="anomaly",
                service=anomaly.service,
                relation=relation,
                hop_distance=hop,
                summary=(
                    f"Anomaly detected by {anomaly.detector_name} "
                    f"(score={anomaly.score:.4f}, threshold={anomaly.threshold:.4f})"
                ),
                record_id=anomaly.anomaly_id,
                minutes_relative_to_window_start=None,
                details={
                    "detector_name": anomaly.detector_name,
                    "score": anomaly.score,
                    "threshold": anomaly.threshold,
                    "is_anomaly": anomaly.is_anomaly,
                    "metric_names": list(anomaly.metric_names),
                },
            )
        )

    for event in events:
        if event.service not in scope_services:
            continue
        if not (start_utc <= event.timestamp_utc <= end_utc):
            continue
        relation, hop = _resolve_relation_and_hop(
            event.service, impact.target_service, dep_hops, caller_hops
        )
        timeline.append(
            TimelineItem(
                timestamp_utc=event.timestamp_utc,
                signal_type="event",
                service=event.service,
                relation=relation,
                hop_distance=hop,
                summary=f"[{event.severity}] {event.message}",
                record_id=event.event_id,
                minutes_relative_to_window_start=None,
                details={
                    "severity": event.severity,
                    "event_type": event.event_type,
                    "trace_id": event.trace_id,
                },
            )
        )

    for incident in incidents:
        if incident.service not in scope_services:
            continue
        if not (start_utc <= incident.timestamp_utc <= end_utc):
            continue
        relation, hop = _resolve_relation_and_hop(
            incident.service, impact.target_service, dep_hops, caller_hops
        )
        timeline.append(
            TimelineItem(
                timestamp_utc=incident.timestamp_utc,
                signal_type="incident",
                service=incident.service,
                relation=relation,
                hop_distance=hop,
                summary=f"Incident ({incident.severity}/{incident.status}): {incident.title}",
                record_id=incident.incident_id,
                minutes_relative_to_window_start=None,
                details={
                    "title": incident.title,
                    "severity": incident.severity,
                    "status": incident.status,
                },
            )
        )

    timeline.sort(
        key=lambda item: (item.timestamp_utc, item.signal_type, item.service, item.summary)
    )

    counts_by_signal: dict[SignalType, int] = {
        "deployment": 0,
        "anomaly": 0,
        "event": 0,
        "incident": 0,
    }
    for item in timeline:
        counts_by_signal[item.signal_type] += 1

    return CorrelationSummary(
        target_service=impact.target_service,
        window_start=start_utc,
        window_end=end_utc,
        lookback_minutes=lookback_minutes,
        deployment_window_start=deployment_window_start,
        max_depth=max_depth,
        impact=impact,
        correlated_services=sorted(scope_services),
        counts_by_signal=counts_by_signal,
        timeline=timeline,
        correlation_note=DEFAULT_CORRELATION_NOTE,
    )
