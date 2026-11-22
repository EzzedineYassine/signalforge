import argparse
import json
import math
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.src.core.database import SessionLocal
from apps.api.src.models import Deployment, Incident, Service
from apps.api.src.services.anomaly_service import detect_and_persist_anomalies
from apps.api.src.services.knowledge_service import (
    DEFAULT_KNOWLEDGE_DIR,
    QdrantUnavailableError,
    index_knowledge_base,
)
from services.anomaly_detection import FEATURE_COLUMNS
from services.ingestion.pipeline import ingest_event_batch
from services.ingestion.telemetry_pipeline import ingest_metric_batch, ingest_trace_batch

DEMO_SCENARIO_ID = "demo_checkout_payment_db_v1"
DEMO_INCIDENT_EXTERNAL_ID = "demo-inc-checkout-001"

METRICS_START = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)
METRICS_END = datetime(2026, 11, 20, 14, 44, 0, tzinfo=UTC)
INCIDENT_WINDOW_START = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
INCIDENT_WINDOW_END = datetime(2026, 11, 20, 14, 45, 0, tzinfo=UTC)
DEPLOYMENT_TIME = datetime(2026, 11, 20, 14, 5, 0, tzinfo=UTC)


def _build_demo_trace_spans() -> list[dict[str, Any]]:
    """Build deterministic multi-service spans: api-gateway -> checkout -> payment -> postgres."""
    traces_spec = [
        # Healthy baseline trace at 14:15 UTC
        (
            "demo-trace-001",
            datetime(2026, 11, 20, 14, 15, 0, tzinfo=UTC),
            "OK",
            "OK",
            "OK",
            "OK",
            (68.0, 52.0, 31.0, 9.0),
        ),
        # Degraded trace at 14:36 UTC (DB pool exhaustion -> payment 504 -> checkout 502)
        (
            "demo-trace-002",
            datetime(2026, 11, 20, 14, 36, 0, tzinfo=UTC),
            "ERROR",
            "ERROR",
            "ERROR",
            "ERROR",
            (940.0, 880.0, 760.0, 620.0),
        ),
        # Degraded trace at 14:39 UTC
        (
            "demo-trace-003",
            datetime(2026, 11, 20, 14, 39, 0, tzinfo=UTC),
            "ERROR",
            "ERROR",
            "ERROR",
            "ERROR",
            (1080.0, 1010.0, 890.0, 740.0),
        ),
    ]

    spans: list[dict[str, Any]] = []
    for trace_id, base_ts, gw_st, co_st, pay_st, db_st, durs in traces_spec:
        gw_dur, co_dur, pay_dur, db_dur = durs
        chain = [
            (
                "api-gateway",
                f"{trace_id}-gw",
                None,
                "POST /v1/checkout",
                "SERVER",
                gw_st,
                0,
                gw_dur,
            ),
            (
                "checkout-service",
                f"{trace_id}-co",
                f"{trace_id}-gw",
                "checkout.place_order",
                "SERVER",
                co_st,
                5,
                co_dur,
            ),
            (
                "payment-service",
                f"{trace_id}-pay",
                f"{trace_id}-co",
                "payment.authorize_card",
                "SERVER",
                pay_st,
                12,
                pay_dur,
            ),
            (
                "postgres-primary",
                f"{trace_id}-pg",
                f"{trace_id}-pay",
                "SELECT payment_attempts FOR UPDATE",
                "CLIENT",
                db_st,
                20,
                db_dur,
            ),
        ]
        for svc, span_id, parent_id, op, kind, st, offset_ms, dur_ms in chain:
            start = base_ts + timedelta(milliseconds=offset_ms)
            end = start + timedelta(milliseconds=dur_ms)
            spans.append(
                {
                    "service": svc,
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "parent_span_id": parent_id,
                    "operation_name": op,
                    "span_kind": kind,
                    "status_code": st,
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "duration_ms": dur_ms,
                    "attributes": {"synthetic": True, "scenario": DEMO_SCENARIO_ID},
                }
            )
    return spans


def _build_demo_metrics() -> list[dict[str, Any]]:
    """Generate 45 minutes of aligned 7-feature synthetic metrics for checkout & payment."""
    points: list[dict[str, Any]] = []
    units = {
        "cpu_utilization": "ratio",
        "memory_utilization": "ratio",
        "request_latency_ms": "ms",
        "request_throughput": "req/s",
        "error_rate": "ratio",
        "db_connections": "count",
        "queue_depth": "messages",
    }

    service_profiles: dict[str, tuple[dict[str, float], dict[str, float]]] = {
        "checkout-service": (
            {
                "cpu_utilization": 0.37,
                "memory_utilization": 0.45,
                "request_latency_ms": 58.0,
                "request_throughput": 245.0,
                "error_rate": 0.003,
                "db_connections": 32.0,
                "queue_depth": 18.0,
            },
            {
                "cpu_utilization": 0.89,
                "memory_utilization": 0.78,
                "request_latency_ms": 840.0,
                "request_throughput": 85.0,
                "error_rate": 0.41,
                "db_connections": 185.0,
                "queue_depth": 520.0,
            },
        ),
        "payment-service": (
            {
                "cpu_utilization": 0.34,
                "memory_utilization": 0.43,
                "request_latency_ms": 48.0,
                "request_throughput": 220.0,
                "error_rate": 0.002,
                "db_connections": 38.0,
                "queue_depth": 15.0,
            },
            {
                "cpu_utilization": 0.92,
                "memory_utilization": 0.82,
                "request_latency_ms": 910.0,
                "request_throughput": 70.0,
                "error_rate": 0.52,
                "db_connections": 198.0,
                "queue_depth": 610.0,
            },
        ),
    }

    for svc_name, (normal_vals, fault_vals) in service_profiles.items():
        for minute in range(45):
            ts = (METRICS_START + timedelta(minutes=minute)).isoformat()
            is_fault = minute >= 35
            wave = math.sin(minute / 6.0)
            for feature in FEATURE_COLUMNS:
                base = fault_vals[feature] if is_fault else normal_vals[feature]
                jitter = base * 0.02 * wave
                val = max(0.0, round(base + jitter, 5))
                points.append(
                    {
                        "service": svc_name,
                        "metric_name": feature,
                        "timestamp": ts,
                        "value": val,
                        "unit": units[feature],
                        "labels": {"synthetic": True, "scenario": DEMO_SCENARIO_ID},
                    }
                )
    return points


def _build_demo_events() -> list[dict[str, Any]]:
    """Build correlated WARNING/ERROR/CRITICAL synthetic log events during the fault window."""
    return [
        {
            "external_id": "demo-evt-001",
            "service": "postgres-primary",
            "timestamp": "2026-11-20T14:35:10Z",
            "event_type": "log",
            "severity": "WARNING",
            "message": (
                "PgBouncer pool saturation: active db_connections=196/200; "
                "client wait queue growing for payment-service"
            ),
            "trace_id": "demo-trace-002",
            "attributes": {"synthetic": True, "scenario": DEMO_SCENARIO_ID, "sqlstate": "53300"},
        },
        {
            "external_id": "demo-evt-002",
            "service": "postgres-primary",
            "timestamp": "2026-11-20T14:35:45Z",
            "event_type": "log",
            "severity": "ERROR",
            "message": (
                "FATAL: SQLSTATE 53300 too_many_connections — rejecting new connections "
                "from payment-service v2.4.1"
            ),
            "trace_id": "demo-trace-002",
            "attributes": {"synthetic": True, "scenario": DEMO_SCENARIO_ID, "sqlstate": "53300"},
        },
        {
            "external_id": "demo-evt-003",
            "service": "payment-service",
            "timestamp": "2026-11-20T14:36:05Z",
            "event_type": "log",
            "severity": "ERROR",
            "message": (
                "Database connection pool exhausted after v2.4.1 rollout; "
                "payment.authorize_card timing out (890 ms)"
            ),
            "trace_id": "demo-trace-002",
            "attributes": {"synthetic": True, "scenario": DEMO_SCENARIO_ID, "version": "v2.4.1"},
        },
        {
            "external_id": "demo-evt-004",
            "service": "checkout-service",
            "timestamp": "2026-11-20T14:36:20Z",
            "event_type": "log",
            "severity": "ERROR",
            "message": (
                "Downstream payment-service returned HTTP 503/504; "
                "circuit_breaker_state=OPEN on checkout.place_order"
            ),
            "trace_id": "demo-trace-002",
            "attributes": {"synthetic": True, "scenario": DEMO_SCENARIO_ID},
        },
        {
            "external_id": "demo-evt-005",
            "service": "api-gateway",
            "timestamp": "2026-11-20T14:37:00Z",
            "event_type": "log",
            "severity": "CRITICAL",
            "message": "POST /v1/checkout 5xx error_rate exceeded 40% SLO threshold",
            "trace_id": "demo-trace-003",
            "attributes": {"synthetic": True, "scenario": DEMO_SCENARIO_ID},
        },
    ]


def _seed_idempotent_deployment(db: Session) -> dict[str, int]:
    """Ensure the synthetic payment-service v2.4.1 deployment exists exactly once."""
    pay_svc = db.scalar(select(Service).where(Service.name == "payment-service"))
    if pay_svc is None:
        raise RuntimeError("payment-service must be registered before seeding deployment")

    existing = db.scalar(
        select(Deployment).where(
            Deployment.service_id == pay_svc.id,
            Deployment.version == "v2.4.1",
            Deployment.deployed_at == DEPLOYMENT_TIME,
        )
    )
    if existing is not None:
        return {"accepted": 0, "duplicates": 1}

    db.add(
        Deployment(
            service_id=pay_svc.id,
            version="v2.4.1",
            commit_sha="f4e9c21d",
            status="succeeded",
            deployed_by="demo-ci-bot",
            deployed_at=DEPLOYMENT_TIME,
            metadata_json={
                "synthetic": True,
                "scenario": DEMO_SCENARIO_ID,
                "environment": "production",
                "summary": "Increased payment worker concurrency without matching PgBouncer limit",
            },
        )
    )
    db.commit()
    return {"accepted": 1, "duplicates": 0}


def _seed_idempotent_incident(db: Session) -> dict[str, int]:
    """Ensure the synthetic open incident row exists exactly once by external_id."""
    checkout_svc = db.scalar(select(Service).where(Service.name == "checkout-service"))
    if checkout_svc is None:
        raise RuntimeError("checkout-service must be registered before seeding incident")

    existing = db.scalar(
        select(Incident).where(Incident.external_id == DEMO_INCIDENT_EXTERNAL_ID)
    )
    if existing is not None:
        return {"accepted": 0, "duplicates": 1}

    db.add(
        Incident(
            external_id=DEMO_INCIDENT_EXTERNAL_ID,
            title=(
                "[SYNTHETIC DEMO] Checkout 5xx latency spike following "
                "payment-service v2.4.1 deployment and Postgres pool exhaustion"
            ),
            description=(
                "Synthetic e-commerce incident scenario for SignalForge UI and investigation "
                "walkthroughs. Not real production data."
            ),
            service_id=checkout_svc.id,
            severity="CRITICAL",
            status="open",
            started_at=datetime(2026, 11, 20, 14, 36, 0, tzinfo=UTC),
        )
    )
    db.commit()
    return {"accepted": 1, "duplicates": 0}


def seed_demo_scenario(
    db: Session,
    *,
    index_knowledge: bool = True,
) -> dict[str, Any]:
    """Idempotently seed the complete synthetic e-commerce incident scenario."""
    trace_res = ingest_trace_batch(db, _build_demo_trace_spans())
    metric_res = ingest_metric_batch(db, _build_demo_metrics())
    event_res = ingest_event_batch(db, _build_demo_events())
    deployment_res = _seed_idempotent_deployment(db)
    incident_res = _seed_idempotent_incident(db)

    anomaly_summaries: dict[str, dict[str, Any]] = {}
    for svc_name in ("checkout-service", "payment-service"):
        det = detect_and_persist_anomalies(
            db,
            service_name=svc_name,
            start_time=METRICS_START,
            end_time=METRICS_END,
            detector_name="lstm_autoencoder",
            persist_all=False,
        )
        anomaly_summaries[svc_name] = {
            "detector": det.detector,
            "points_evaluated": det.points_evaluated,
            "anomalies_detected": det.anomalies_detected,
            "persisted": det.persisted,
            "duplicates": det.duplicates,
        }

    kb_summary: dict[str, Any]
    if not index_knowledge:
        kb_summary = {"status": "skipped_by_flag"}
    else:
        try:
            idx = index_knowledge_base(source_dir=DEFAULT_KNOWLEDGE_DIR)
            kb_summary = {
                "status": "indexed",
                "source_dir": idx.source_dir,
                "documents_indexed": idx.documents_indexed,
                "chunks_indexed": idx.chunks_indexed,
                "collection": idx.collection,
                "embedding_model": idx.embedding_model,
            }
        except QdrantUnavailableError as exc:
            kb_summary = {"status": "skipped_qdrant_unavailable", "reason": str(exc)}

    return {
        "synthetic": True,
        "scenario": DEMO_SCENARIO_ID,
        "traces": {"accepted": trace_res.accepted, "duplicates": trace_res.duplicates},
        "metrics": {"accepted": metric_res.accepted, "duplicates": metric_res.duplicates},
        "events": {"accepted": event_res.accepted, "duplicates": event_res.duplicates},
        "deployments": deployment_res,
        "incidents": incident_res,
        "anomalies": anomaly_summaries,
        "knowledge_base": kb_summary,
        "recommended_ui_query": {
            "service": "checkout-service",
            "start_time": INCIDENT_WINDOW_START.isoformat(),
            "end_time": INCIDENT_WINDOW_END.isoformat(),
            "lookback_minutes": 60,
            "max_depth": 3,
            "question": (
                "Why did checkout-service latency and 5xx errors spike after 14:35 UTC?"
            ),
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotently seed a synthetic e-commerce incident demo scenario."
    )
    parser.add_argument(
        "--skip-knowledge",
        action="store_true",
        help="Skip indexing data/synthetic/knowledge_base into Qdrant.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        with SessionLocal() as db:
            summary = seed_demo_scenario(db, index_knowledge=not args.skip_knowledge)
    except Exception as exc:
        print(f"Demo seeder failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
