import argparse
import ctypes
import json
import os
import platform
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.src.core import database
from apps.api.src.core.config import get_settings
from apps.api.src.main import app
from apps.api.src.models import Base, Incident, Service
from apps.api.src.services.investigation_service import get_llm_client
from apps.api.src.services.knowledge_service import get_knowledge_retriever
from evaluation.retrieval.metrics import percentile
from services.anomaly_detection import FEATURE_COLUMNS
from services.ingestion.pipeline import ingest_event_batch
from services.ingestion.telemetry_pipeline import (
    ingest_deployment_batch,
    ingest_metric_batch,
    ingest_trace_batch,
)
from services.investigation import (
    FakeLLMClient,
    LLMInvestigationDraft,
    RootCauseHypothesis,
)
from services.retrieval.chunking.markdown_chunker import MarkdownChunker
from services.retrieval.embeddings.embedder import DeterministicHashEmbedder
from services.retrieval.ingestion.loader import load_knowledge_base
from services.retrieval.search.hybrid import HybridRetriever
from services.retrieval.search.qdrant_store import QdrantVectorStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "experiments" / "benchmarks" / "end_to_end" / "system_perf.json"
BENCHMARK_QDRANT_COLLECTION = "signalforge_perf_bench"


def get_process_rss_mb() -> float:
    """Measure current Python process resident set size (MB) using stdlib only."""
    if sys.platform == "win32":

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        get_mem_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_mem_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        get_mem_info.restype = ctypes.c_int
        handle = get_current_process()
        if get_mem_info(handle, ctypes.byref(counters), counters.cb):
            return round(counters.WorkingSetSize / (1024.0 * 1024.0), 2)
        return 0.0

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
        return round(usage / divisor, 2)
    except Exception:
        return 0.0


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
    return cfg


def _truncate_all(engine: Any) -> None:
    tables = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def _time_requests(
    fn: Callable[[], Any],
    *,
    iterations: int = 20,
    expected_status: int = 200,
) -> dict[str, Any]:
    # Single untimed warmup request
    warm = fn()
    if warm.status_code != expected_status:
        raise RuntimeError(f"Warmup request returned HTTP {warm.status_code}: {warm.text}")

    latencies_ms: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        res = fn()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if res.status_code != expected_status:
            raise RuntimeError(f"Timed request returned HTTP {res.status_code}: {res.text}")
        latencies_ms.append(elapsed_ms)

    return {
        "iterations": iterations,
        "p50_ms": round(percentile(latencies_ms, 50.0), 3),
        "p95_ms": round(percentile(latencies_ms, 95.0), 3),
        "min_ms": round(min(latencies_ms), 3),
        "max_ms": round(max(latencies_ms), 3),
    }


def run_system_benchmark(
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    iterations: int = 20,
) -> dict[str, Any]:
    """Measure local ingestion throughput, API latencies, workflow duration, and RSS memory."""
    settings = get_settings()
    test_url = settings.TEST_DATABASE_URL
    if "test" not in test_url.lower():
        raise ValueError(
            f"Safety guard: TEST_DATABASE_URL must target a test database, got {test_url!r}"
        )

    os.environ["ALEMBIC_DATABASE_URL"] = test_url
    test_engine = create_engine(test_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    command.upgrade(_alembic_config(), "head")

    orig_engine = database.engine
    orig_session_local = database.SessionLocal
    database.engine = test_engine
    database.SessionLocal = sessionmaker(bind=test_engine)

    store = QdrantVectorStore(
        url=settings.QDRANT_URL,
        collection_name=BENCHMARK_QDRANT_COLLECTION,
    )

    rss_start_mb = get_process_rss_mb()
    base_ts = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)

    _truncate_all(test_engine)
    store.delete_collection()

    try:
        # 1. Measure 1,000-event batch ingestion throughput
        event_batch = [
            {
                "external_id": f"perf-evt-{i:04d}",
                "service": "checkout-service" if i % 2 == 0 else "payment-service",
                "timestamp": (base_ts + timedelta(seconds=i)).isoformat(),
                "event_type": "log",
                "severity": "ERROR" if i % 10 == 0 else "INFO",
                "message": f"Synthetic benchmark event #{i}",
                "attributes": {"synthetic": True, "seq": i},
            }
            for i in range(1000)
        ]

        with Session(test_engine) as db:
            t_ev0 = time.perf_counter()
            ev_res = ingest_event_batch(db, event_batch)
            ev_elapsed = time.perf_counter() - t_ev0

        # 2. Measure 2,000-point metric batch ingestion throughput (285*7 = 1,995 aligned + 5)
        metric_batch: list[dict[str, Any]] = []
        normal_vals = {
            "cpu_utilization": 0.38,
            "memory_utilization": 0.45,
            "request_latency_ms": 60.0,
            "request_throughput": 240.0,
            "error_rate": 0.004,
            "db_connections": 32.0,
            "queue_depth": 18.0,
        }
        spike_vals = {
            "cpu_utilization": 0.96,
            "memory_utilization": 0.91,
            "request_latency_ms": 860.0,
            "request_throughput": 75.0,
            "error_rate": 0.44,
            "db_connections": 192.0,
            "queue_depth": 680.0,
        }
        for minute in range(285):
            ts = (base_ts + timedelta(minutes=minute)).isoformat()
            vals = spike_vals if 250 <= minute < 260 else normal_vals
            for feature in FEATURE_COLUMNS:
                metric_batch.append(
                    {
                        "service": "checkout-service",
                        "metric_name": feature,
                        "timestamp": ts,
                        "value": vals[feature],
                        "labels": {"synthetic": True},
                    }
                )
        for extra in range(5):
            metric_batch.append(
                {
                    "service": "payment-service",
                    "metric_name": "cpu_utilization",
                    "timestamp": (base_ts + timedelta(minutes=extra)).isoformat(),
                    "value": 0.35,
                    "labels": {"synthetic": True},
                }
            )
        assert len(metric_batch) == 2000

        with Session(test_engine) as db:
            t_m0 = time.perf_counter()
            m_res = ingest_metric_batch(db, metric_batch)
            m_elapsed = time.perf_counter() - t_m0

            # Seed topology traces, deployment, and open incident for API benchmarks
            spans = [
                {
                    "service": "api-gateway",
                    "trace_id": "perf-trace-1",
                    "span_id": "s-gw",
                    "parent_span_id": None,
                    "operation_name": "POST /v1/checkout",
                    "span_kind": "SERVER",
                    "status_code": "ERROR",
                    "start_time": (base_ts + timedelta(minutes=252)).isoformat(),
                    "end_time": (base_ts + timedelta(minutes=252, milliseconds=880)).isoformat(),
                    "duration_ms": 880.0,
                },
                {
                    "service": "checkout-service",
                    "trace_id": "perf-trace-1",
                    "span_id": "s-co",
                    "parent_span_id": "s-gw",
                    "operation_name": "checkout.process",
                    "span_kind": "SERVER",
                    "status_code": "ERROR",
                    "start_time": (base_ts + timedelta(minutes=252, milliseconds=10)).isoformat(),
                    "end_time": (base_ts + timedelta(minutes=252, milliseconds=850)).isoformat(),
                    "duration_ms": 840.0,
                },
                {
                    "service": "payment-service",
                    "trace_id": "perf-trace-1",
                    "span_id": "s-pay",
                    "parent_span_id": "s-co",
                    "operation_name": "payment.authorize",
                    "span_kind": "SERVER",
                    "status_code": "ERROR",
                    "start_time": (base_ts + timedelta(minutes=252, milliseconds=20)).isoformat(),
                    "end_time": (base_ts + timedelta(minutes=252, milliseconds=790)).isoformat(),
                    "duration_ms": 770.0,
                },
            ]
            ingest_trace_batch(db, spans)
            ingest_deployment_batch(
                db,
                [
                    {
                        "service": "payment-service",
                        "version": "v2.4.1",
                        "deployed_at": (base_ts + timedelta(minutes=230)).isoformat(),
                        "status": "succeeded",
                    }
                ],
            )
            checkout_svc = db.query(Service).filter(Service.name == "checkout-service").one()
            db.add(
                Incident(
                    external_id="perf-inc-1",
                    title="Synthetic performance benchmark incident",
                    service_id=checkout_svc.id,
                    severity="CRITICAL",
                    status="open",
                    started_at=base_ts + timedelta(minutes=252),
                )
            )
            db.commit()

        # Index synthetic KB into isolated Qdrant collection via fast DeterministicHashEmbedder
        with tempfile.TemporaryDirectory() as tmp_dir:
            retriever = HybridRetriever(
                embedder=DeterministicHashEmbedder(dimension=384),
                vector_store=store,
                bm25_path=Path(tmp_dir) / "bm25_perf.json",
            )
            docs = load_knowledge_base(DEFAULT_KB_DIR)
            chunks = MarkdownChunker(max_chars=1200, overlap_chars=150).chunk_documents(docs)
            retriever.index_chunks(chunks)

            fake_llm = FakeLLMClient(
                response=LLMInvestigationDraft(
                    confirmed_facts=["[ev-001] Observed anomaly and error events in window."],
                    probable_root_causes=[
                        RootCauseHypothesis(
                            hypothesis="Synthetic benchmark checkout latency spike.",
                            confidence="medium",
                            supporting_evidence_ids=["ev-001"],
                        )
                    ],
                    cited_doc_ids=[],
                    abstained=False,
                ),
                model_name="deterministic-perf-llm",
            )

            app.dependency_overrides[get_knowledge_retriever] = lambda: retriever
            app.dependency_overrides[get_llm_client] = lambda: fake_llm
            try:
                detect_start = (base_ts + timedelta(minutes=215)).isoformat()
                detect_end = (base_ts + timedelta(minutes=260)).isoformat()
                corr_start = (base_ts + timedelta(minutes=250)).isoformat()
                corr_end = (base_ts + timedelta(minutes=265)).isoformat()

                with TestClient(app) as client:
                    api_latencies = {
                        "GET /health": _time_requests(
                            lambda: client.get("/health"),
                            iterations=iterations,
                        ),
                        "GET /api/v1/events": _time_requests(
                            lambda: client.get(
                                "/api/v1/events",
                                params={"service": "checkout-service", "limit": 50},
                            ),
                            iterations=iterations,
                        ),
                        "GET /api/v1/metrics": _time_requests(
                            lambda: client.get(
                                "/api/v1/metrics",
                                params={"service": "checkout-service", "limit": 100},
                            ),
                            iterations=iterations,
                        ),
                        "GET /api/v1/graph/dependencies": _time_requests(
                            lambda: client.get("/api/v1/graph/dependencies"),
                            iterations=iterations,
                        ),
                        "POST /api/v1/anomalies/detect (lstm_autoencoder, 46 pts)": _time_requests(
                            lambda: client.post(
                                "/api/v1/anomalies/detect",
                                json={
                                    "service": "checkout-service",
                                    "start_time": detect_start,
                                    "end_time": detect_end,
                                    "detector": "lstm_autoencoder",
                                },
                            ),
                            iterations=iterations,
                        ),
                        "POST /api/v1/correlation/timeline": _time_requests(
                            lambda: client.post(
                                "/api/v1/correlation/timeline",
                                json={
                                    "service": "checkout-service",
                                    "start_time": corr_start,
                                    "end_time": corr_end,
                                    "lookback_minutes": 60,
                                    "max_depth": 3,
                                },
                            ),
                            iterations=iterations,
                        ),
                    }

                    inv_perf = _time_requests(
                        lambda: client.post(
                            "/api/v1/investigations/run",
                            json={
                                "question": "Why did checkout-service latency spike?",
                                "service": "checkout-service",
                                "start_time": corr_start,
                                "end_time": corr_end,
                                "lookback_minutes": 60,
                                "max_depth": 3,
                                "top_k_docs": 5,
                            },
                        ),
                        iterations=5,
                    )
            finally:
                app.dependency_overrides.pop(get_knowledge_retriever, None)
                app.dependency_overrides.pop(get_llm_client, None)

        rss_peak_after_mb = get_process_rss_mb()

        payload: dict[str, Any] = {
            "is_synthetic_benchmark": True,
            "database_target": "signalforge_test",
            "environment": {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "ingestion_throughput": {
                "events": {
                    "batch_size": 1000,
                    "accepted": ev_res.accepted,
                    "elapsed_seconds": round(ev_elapsed, 4),
                    "events_per_second": round(ev_res.accepted / ev_elapsed, 2),
                },
                "metrics": {
                    "batch_size": 2000,
                    "accepted": m_res.accepted,
                    "elapsed_seconds": round(m_elapsed, 4),
                    "metrics_per_second": round(m_res.accepted / m_elapsed, 2),
                },
            },
            "api_latencies": api_latencies,
            "investigation_workflow_deterministic": {
                "mode": "deterministic (FakeLLMClient + live PostgreSQL + live Qdrant hash)",
                **inv_perf,
            },
            "memory_rss_mb": {
                "start_mb": rss_start_mb,
                "end_mb": rss_peak_after_mb,
            },
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    finally:
        store.delete_collection()
        _truncate_all(test_engine)
        test_engine.dispose()
        database.engine = orig_engine
        database.SessionLocal = orig_session_local


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run local SignalForge ingestion, API latency, and memory benchmark."
    )
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--iterations", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        payload = run_system_benchmark(output_path=args.output_path, iterations=args.iterations)
    except Exception as exc:
        print(f"System benchmark failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    ev = payload["ingestion_throughput"]["events"]
    met = payload["ingestion_throughput"]["metrics"]
    print(
        f"Event throughput:  {ev['events_per_second']:.1f} events/s "
        f"({ev['accepted']} in {ev['elapsed_seconds']:.3f}s)"
    )
    print(
        f"Metric throughput: {met['metrics_per_second']:.1f} metrics/s "
        f"({met['accepted']} in {met['elapsed_seconds']:.3f}s)\n"
    )
    for ep, stats in payload["api_latencies"].items():
        print(f"{ep:<58} p50={stats['p50_ms']:7.2f} ms | p95={stats['p95_ms']:7.2f} ms")
    inv = payload["investigation_workflow_deterministic"]
    print(
        f"\nInvestigation workflow (deterministic): "
        f"p50={inv['p50_ms']:.2f} ms | p95={inv['p95_ms']:.2f} ms"
    )
    mem = payload["memory_rss_mb"]
    print(f"Python process RSS: start={mem['start_mb']} MB | end={mem['end_mb']} MB")
    print(f"Saved results to: {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
