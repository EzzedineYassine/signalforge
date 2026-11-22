import importlib.util
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.src.models import (
    Anomaly,
    Deployment,
    Event,
    Incident,
    MetricPoint,
    Service,
    TraceSpan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SEEDER_PATH = REPO_ROOT / "scripts" / "seed_demo_scenario.py"


def _load_seeder_module():
    spec = importlib.util.spec_from_file_location("seed_demo_scenario", SEEDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_counts(db_session: Session) -> dict[str, int]:
    return {
        "services": int(db_session.scalar(select(func.count(Service.id))) or 0),
        "traces": int(db_session.scalar(select(func.count(TraceSpan.id))) or 0),
        "metrics": int(db_session.scalar(select(func.count(MetricPoint.id))) or 0),
        "events": int(db_session.scalar(select(func.count(Event.id))) or 0),
        "deployments": int(db_session.scalar(select(func.count(Deployment.id))) or 0),
        "incidents": int(db_session.scalar(select(func.count(Incident.id))) or 0),
        "anomalies": int(db_session.scalar(select(func.count(Anomaly.id))) or 0),
    }


def test_seed_demo_scenario_is_strictly_idempotent(db_session: Session) -> None:
    seeder = _load_seeder_module()

    run1 = seeder.seed_demo_scenario(db_session, index_knowledge=False)
    counts_after_run1 = _table_counts(db_session)

    assert run1["synthetic"] is True
    assert run1["knowledge_base"]["status"] == "skipped_by_flag"
    assert run1["traces"]["accepted"] == 12
    assert run1["traces"]["duplicates"] == 0
    assert run1["metrics"]["accepted"] == 630
    assert run1["metrics"]["duplicates"] == 0
    assert run1["events"]["accepted"] == 5
    assert run1["events"]["duplicates"] == 0
    assert run1["deployments"] == {"accepted": 1, "duplicates": 0}
    assert run1["incidents"] == {"accepted": 1, "duplicates": 0}
    assert run1["anomalies"]["checkout-service"]["anomalies_detected"] >= 1
    assert run1["anomalies"]["checkout-service"]["persisted"] >= 1
    assert run1["anomalies"]["payment-service"]["anomalies_detected"] >= 1
    assert run1["anomalies"]["payment-service"]["persisted"] >= 1

    assert counts_after_run1["services"] == 4
    assert counts_after_run1["traces"] == 12
    assert counts_after_run1["metrics"] == 630
    assert counts_after_run1["events"] == 5
    assert counts_after_run1["deployments"] == 1
    assert counts_after_run1["incidents"] == 1
    assert counts_after_run1["anomalies"] >= 2

    # Second run must insert zero new rows across every table
    run2 = seeder.seed_demo_scenario(db_session, index_knowledge=False)
    counts_after_run2 = _table_counts(db_session)

    assert counts_after_run2 == counts_after_run1
    assert run2["traces"] == {"accepted": 0, "duplicates": 12}
    assert run2["metrics"] == {"accepted": 0, "duplicates": 630}
    assert run2["events"] == {"accepted": 0, "duplicates": 5}
    assert run2["deployments"] == {"accepted": 0, "duplicates": 1}
    assert run2["incidents"] == {"accepted": 0, "duplicates": 1}
    assert run2["anomalies"]["checkout-service"]["persisted"] == 0
    assert (
        run2["anomalies"]["checkout-service"]["duplicates"]
        == run2["anomalies"]["checkout-service"]["anomalies_detected"]
    )
    assert run2["anomalies"]["payment-service"]["persisted"] == 0
    assert (
        run2["anomalies"]["payment-service"]["duplicates"]
        == run2["anomalies"]["payment-service"]["anomalies_detected"]
    )
