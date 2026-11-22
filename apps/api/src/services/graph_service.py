from dataclasses import dataclass
from datetime import datetime, timedelta

import networkx as nx
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session, aliased

from apps.api.src.models import Anomaly, Deployment, Event, Incident, Service, TraceSpan
from apps.api.src.services.anomaly_service import ServiceNotFoundError
from services.graph import (
    AnomalyRecord,
    CorrelationSummary,
    DeploymentRecord,
    EventRecord,
    ImpactAnalysis,
    IncidentRecord,
    ServiceEdge,
    ServiceEdgeInput,
    analyze_service_impact,
    build_service_dependency_graph,
    correlate_timeline,
    extract_service_edges,
    extract_service_nodes,
)
from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc

CORRELATED_EVENT_SEVERITIES: tuple[str, ...] = ("WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class DependencyGraphResult:
    nodes: list[str]
    edges: list[ServiceEdge]


def _load_service_names(db: Session) -> list[str]:
    stmt = select(Service.name).order_by(Service.name.asc())
    return list(db.scalars(stmt).all())


def _load_dependency_graph(
    db: Session,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> nx.DiGraph:
    """Build a NetworkX DiGraph from registered services and parent->child trace spans."""
    start_utc = normalize_timestamp_utc(start_time) if start_time is not None else None
    end_utc = normalize_timestamp_utc(end_time) if end_time is not None else None
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise ValueError("start_time must be less than or equal to end_time")

    service_names = _load_service_names(db)

    parent_span = aliased(TraceSpan, name="parent_span")
    child_span = aliased(TraceSpan, name="child_span")
    parent_svc = aliased(Service, name="parent_svc")
    child_svc = aliased(Service, name="child_svc")

    error_indicator = case((child_span.status_code == "ERROR", 1), else_=0)

    stmt = (
        select(
            parent_svc.name.label("caller"),
            child_svc.name.label("callee"),
            func.count(child_span.id).label("call_count"),
            func.coalesce(func.sum(error_indicator), 0).label("error_count"),
            func.coalesce(func.avg(child_span.duration_ms), 0.0).label("avg_duration_ms"),
        )
        .select_from(child_span)
        .join(
            parent_span,
            and_(
                child_span.trace_id == parent_span.trace_id,
                child_span.parent_span_id == parent_span.span_id,
            ),
        )
        .join(parent_svc, parent_span.service_id == parent_svc.id)
        .join(child_svc, child_span.service_id == child_svc.id)
        .where(child_span.parent_span_id.is_not(None))
    )

    if start_utc is not None:
        stmt = stmt.where(child_span.start_time_utc >= start_utc)
    if end_utc is not None:
        stmt = stmt.where(child_span.start_time_utc <= end_utc)

    stmt = stmt.group_by(parent_svc.name, child_svc.name).order_by(
        parent_svc.name.asc(), child_svc.name.asc()
    )

    rows = db.execute(stmt).all()
    edge_inputs = [
        ServiceEdgeInput(
            caller=row.caller,
            callee=row.callee,
            call_count=int(row.call_count),
            error_count=int(row.error_count),
            avg_duration_ms=float(row.avg_duration_ms),
        )
        for row in rows
    ]

    return build_service_dependency_graph(edge_inputs, services=service_names)


def get_dependency_graph(
    db: Session,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> DependencyGraphResult:
    """Return sorted service nodes and aggregated parent->child dependency edges."""
    graph = _load_dependency_graph(db, start_time=start_time, end_time=end_time)
    return DependencyGraphResult(
        nodes=extract_service_nodes(graph),
        edges=extract_service_edges(graph),
    )


def get_service_impact(
    db: Session,
    *,
    service: str,
    max_depth: int = 3,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> ImpactAnalysis:
    """Compute multi-hop dependencies and affected callers for a registered service."""
    target = normalize_service_name(service)
    graph = _load_dependency_graph(db, start_time=start_time, end_time=end_time)
    if target not in graph:
        raise ServiceNotFoundError(f"Service {target!r} not found")
    return analyze_service_impact(graph, target, max_depth=max_depth)


def build_correlation_timeline(
    db: Session,
    *,
    target_service: str,
    window_start: datetime,
    window_end: datetime,
    lookback_minutes: int = 60,
    max_depth: int = 3,
) -> CorrelationSummary:
    """Correlate deployments, anomalies, error/warning events, and incidents around a service."""
    target = normalize_service_name(target_service)
    start_utc = normalize_timestamp_utc(window_start)
    end_utc = normalize_timestamp_utc(window_end)
    if start_utc > end_utc:
        raise ValueError("window_start must be less than or equal to window_end")
    if lookback_minutes < 0:
        raise ValueError(f"lookback_minutes must be non-negative, got {lookback_minutes}")
    if max_depth < 1:
        raise ValueError(f"max_depth must be at least 1, got {max_depth}")

    graph = _load_dependency_graph(db)
    if target not in graph:
        raise ServiceNotFoundError(f"Service {target!r} not found")

    impact = analyze_service_impact(graph, target, max_depth=max_depth)
    scope_services = sorted({impact.target_service} | set(impact.all_related_services))
    deployment_window_start = start_utc - timedelta(minutes=lookback_minutes)

    dep_stmt = (
        select(Deployment, Service.name.label("service_name"), Service.environment.label("svc_env"))
        .join(Service, Deployment.service_id == Service.id)
        .where(
            Service.name.in_(scope_services),
            Deployment.deployed_at >= deployment_window_start,
            Deployment.deployed_at <= end_utc,
        )
        .order_by(Deployment.deployed_at.asc(), Deployment.id.asc())
    )
    deployment_records = [
        DeploymentRecord(
            deployment_id=str(dep.id),
            service=service_name,
            timestamp_utc=dep.deployed_at,
            version=dep.version,
            status=dep.status,
            commit_sha=dep.commit_sha,
            environment=str(dep.metadata_json.get("environment", svc_env)),
        )
        for dep, service_name, svc_env in db.execute(dep_stmt).all()
    ]

    anom_stmt = (
        select(Anomaly, Service.name.label("service_name"))
        .join(Service, Anomaly.service_id == Service.id)
        .where(
            Service.name.in_(scope_services),
            Anomaly.is_anomaly.is_(True),
            Anomaly.timestamp_utc >= start_utc,
            Anomaly.timestamp_utc <= end_utc,
        )
        .order_by(Anomaly.timestamp_utc.asc(), Anomaly.id.asc())
    )
    anomaly_records = [
        AnomalyRecord(
            anomaly_id=str(anom.id),
            service=service_name,
            timestamp_utc=anom.timestamp_utc,
            detector_name=anom.detector_name,
            score=float(anom.score),
            threshold=float(anom.threshold),
            is_anomaly=bool(anom.is_anomaly),
            metric_names=list(anom.metric_names or []),
        )
        for anom, service_name in db.execute(anom_stmt).all()
    ]

    event_stmt = (
        select(Event, Service.name.label("service_name"))
        .join(Service, Event.service_id == Service.id)
        .where(
            Service.name.in_(scope_services),
            Event.severity.in_(CORRELATED_EVENT_SEVERITIES),
            Event.timestamp_utc >= start_utc,
            Event.timestamp_utc <= end_utc,
        )
        .order_by(Event.timestamp_utc.asc(), Event.id.asc())
    )
    event_records = [
        EventRecord(
            event_id=str(evt.id),
            service=service_name,
            timestamp_utc=evt.timestamp_utc,
            severity=evt.severity,
            message=evt.message,
            event_type=evt.event_type,
            trace_id=evt.trace_id,
        )
        for evt, service_name in db.execute(event_stmt).all()
    ]

    incident_stmt = (
        select(Incident, Service.name.label("service_name"))
        .join(Service, Incident.service_id == Service.id)
        .where(
            Service.name.in_(scope_services),
            Incident.started_at >= start_utc,
            Incident.started_at <= end_utc,
        )
        .order_by(Incident.started_at.asc(), Incident.id.asc())
    )
    incident_records = [
        IncidentRecord(
            incident_id=str(inc.id),
            service=service_name,
            timestamp_utc=inc.started_at,
            title=inc.title,
            severity=inc.severity,
            status=inc.status,
        )
        for inc, service_name in db.execute(incident_stmt).all()
    ]

    return correlate_timeline(
        target_service=target,
        window_start=start_utc,
        window_end=end_utc,
        graph=graph,
        deployments=deployment_records,
        anomalies=anomaly_records,
        events=event_records,
        incidents=incident_records,
        lookback_minutes=lookback_minutes,
        max_depth=max_depth,
    )
