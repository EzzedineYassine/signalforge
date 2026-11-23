from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from services.graph import ServiceEdgeInput
from services.investigation import EvidenceItem

_BASE = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)


class RootCauseScenario(BaseModel):
    """Controlled synthetic incident scenario with verified ground truth."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    is_synthetic: bool = True
    title: str
    question: str
    target_service: str
    window_start: datetime
    window_end: datetime
    expected_root_cause_keywords: list[str] = Field(default_factory=list)
    expected_affected_services: list[str] = Field(default_factory=list)
    expected_doc_ids: list[str] = Field(default_factory=list)
    expect_abstention: bool = False
    edges: list[ServiceEdgeInput] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)


def get_e2e_scenarios() -> list[RootCauseScenario]:
    """Return the 8 controlled synthetic end-to-end evaluation scenarios."""
    w_start = _BASE
    w_end = _BASE + timedelta(minutes=30)

    return [
        # 1. CPU Saturation (checkout-service)
        RootCauseScenario(
            scenario_id="cpu_saturation",
            title="Checkout Service CPU Saturation During Promo Surge",
            question="Why did checkout-service latency and CPU utilization spike during the promo?",
            target_service="checkout-service",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=["cpu", "saturation", "regex", "promo"],
            expected_affected_services=["api-gateway", "checkout-service", "order-service"],
            expected_doc_ids=["rb-001", "inc-2026-001"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="api-gateway",
                    callee="checkout-service",
                    call_count=420,
                    error_count=45,
                    avg_duration_ms=780.0,
                ),
                ServiceEdgeInput(
                    caller="checkout-service",
                    callee="order-service",
                    call_count=375,
                    error_count=4,
                    avg_duration_ms=52.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="anomaly",
                    service="checkout-service",
                    timestamp_utc=w_start + timedelta(minutes=4),
                    summary=(
                        "LSTM autoencoder anomaly score=11.42 > threshold=6.75; "
                        "top_feature=cpu_utilization (0.97) with request_latency_ms=820ms"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="event",
                    service="checkout-service",
                    timestamp_utc=w_start + timedelta(minutes=5),
                    summary=(
                        "[ERROR] CPU saturation during promo traffic surge; "
                        "cart promo regex evaluation pegged worker threads at 97% CPU"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-003",
                    signal_type="incident",
                    service="checkout-service",
                    timestamp_utc=w_start + timedelta(minutes=6),
                    summary="Incident (CRITICAL/open): Checkout Service CPU Saturation",
                ),
            ],
        ),
        # 2. DB Connection Exhaustion (payment-service / postgres-primary)
        RootCauseScenario(
            scenario_id="db_connection_exhaustion",
            title="PostgreSQL Primary Connection Pool Exhaustion",
            question=(
                "Why are database connections exhausted on postgres-primary and failing "
                "payment-service queries?"
            ),
            target_service="postgres-primary",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=[
                "connection",
                "pool",
                "pgbouncer",
                "53300",
                "db_connections",
            ],
            expected_affected_services=[
                "checkout-service",
                "order-service",
                "payment-service",
                "postgres-primary",
            ],
            expected_doc_ids=["rb-002", "inc-2026-002", "arch-004"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="checkout-service",
                    callee="payment-service",
                    call_count=300,
                    error_count=92,
                    avg_duration_ms=640.0,
                ),
                ServiceEdgeInput(
                    caller="checkout-service",
                    callee="order-service",
                    call_count=300,
                    error_count=60,
                    avg_duration_ms=510.0,
                ),
                ServiceEdgeInput(
                    caller="payment-service",
                    callee="postgres-primary",
                    call_count=300,
                    error_count=110,
                    avg_duration_ms=590.0,
                ),
                ServiceEdgeInput(
                    caller="order-service",
                    callee="postgres-primary",
                    call_count=300,
                    error_count=75,
                    avg_duration_ms=480.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="anomaly",
                    service="postgres-primary",
                    timestamp_utc=w_start + timedelta(minutes=3),
                    summary=(
                        "LSTM autoencoder anomaly score=13.10 > threshold=6.75; "
                        "top_feature=db_connections (198/200 active PgBouncer pool slots)"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="event",
                    service="postgres-primary",
                    timestamp_utc=w_start + timedelta(minutes=4),
                    summary=(
                        "[ERROR] SQLSTATE 53300 too_many_connections: PgBouncer connection "
                        "pool exhausted by idle-in-transaction sessions from payment-service"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-003",
                    signal_type="incident",
                    service="postgres-primary",
                    timestamp_utc=w_start + timedelta(minutes=5),
                    summary="Incident (CRITICAL/open): Postgres Connection Pool Exhaustion",
                ),
            ],
        ),
        # 3. Payment Service Failure (payment-service impacting checkout-service)
        RootCauseScenario(
            scenario_id="payment_service_failure",
            title="Primary Card Processor 504 Outage",
            question=(
                "Why is payment-service returning 504 errors and tripping checkout-service "
                "circuit breakers?"
            ),
            target_service="payment-service",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=[
                "payment",
                "504",
                "stripe-eu",
                "provider",
                "failover",
                "timeout",
            ],
            expected_affected_services=["api-gateway", "checkout-service", "payment-service"],
            expected_doc_ids=["rb-003", "inc-2026-003", "rb-010"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="api-gateway",
                    callee="checkout-service",
                    call_count=280,
                    error_count=125,
                    avg_duration_ms=410.0,
                ),
                ServiceEdgeInput(
                    caller="checkout-service",
                    callee="payment-service",
                    call_count=280,
                    error_count=130,
                    avg_duration_ms=390.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="anomaly",
                    service="payment-service",
                    timestamp_utc=w_start + timedelta(minutes=2),
                    summary=(
                        "LSTM autoencoder anomaly score=12.60 > threshold=6.75; "
                        "top_feature=error_rate (0.48) on payment.authorize"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="event",
                    service="payment-service",
                    timestamp_utc=w_start + timedelta(minutes=3),
                    summary=(
                        "[ERROR] Primary card processor stripe-eu returned HTTP 504 timeout; "
                        "provider failover to adyen-eu required"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-003",
                    signal_type="event",
                    service="checkout-service",
                    timestamp_utc=w_start + timedelta(minutes=4),
                    summary=(
                        "[ERROR] circuit_breaker_state=OPEN for downstream payment-service "
                        "(fast-fail HTTP 503)"
                    ),
                ),
            ],
        ),
        # 4. Memory Leak (order-service)
        RootCauseScenario(
            scenario_id="memory_leak",
            title="Order Service Unbounded Cache Memory Leak",
            question=(
                "Why did order-service memory utilization ramp steadily until pods restarted "
                "with OOMKilled?"
            ),
            target_service="order-service",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=["memory", "leak", "cache", "oom", "oomkilled", "heap"],
            expected_affected_services=["checkout-service", "order-service", "postgres-primary"],
            expected_doc_ids=["rb-004", "inc-2026-004", "dep-2026-005"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="checkout-service",
                    callee="order-service",
                    call_count=240,
                    error_count=38,
                    avg_duration_ms=310.0,
                ),
                ServiceEdgeInput(
                    caller="order-service",
                    callee="postgres-primary",
                    call_count=210,
                    error_count=2,
                    avg_duration_ms=28.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="anomaly",
                    service="order-service",
                    timestamp_utc=w_start + timedelta(minutes=8),
                    summary=(
                        "IsolationForest anomaly score=0.29 > threshold=0.09; "
                        "top_feature=memory_utilization (0.96) following gradual heap leak"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="event",
                    service="order-service",
                    timestamp_utc=w_start + timedelta(minutes=10),
                    summary=(
                        "[CRITICAL] Container OOMKilled after unbounded in-memory cache growth "
                        "exhausted heap and caused GC thrashing"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-003",
                    signal_type="incident",
                    service="order-service",
                    timestamp_utc=w_start + timedelta(minutes=11),
                    summary="Incident (CRITICAL/open): Order Service Memory Leak & OOM Restarts",
                ),
            ],
        ),
        # 5. Network Latency (api-gateway -> checkout-service)
        RootCauseScenario(
            scenario_id="network_latency",
            title="API Gateway East-West Network Latency Spike",
            question=(
                "Why did east-west request latency spike at api-gateway while checkout-service "
                "CPU stayed healthy?"
            ),
            target_service="api-gateway",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=[
                "network",
                "latency",
                "keepalive",
                "east-west",
                "jitter",
            ],
            expected_affected_services=["api-gateway", "checkout-service"],
            expected_doc_ids=["rb-005", "inc-2026-005", "dep-2026-001"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="api-gateway",
                    callee="checkout-service",
                    call_count=350,
                    error_count=14,
                    avg_duration_ms=890.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="anomaly",
                    service="api-gateway",
                    timestamp_utc=w_start + timedelta(minutes=5),
                    summary=(
                        "LSTM autoencoder anomaly score=10.15 > threshold=6.75; "
                        "top_feature=request_latency_ms (890ms) while backend CPU remained 0.36"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="event",
                    service="api-gateway",
                    timestamp_utc=w_start + timedelta(minutes=6),
                    summary=(
                        "[ERROR] East-west network latency and packet jitter spike on upstream "
                        "keepalive_requests pool to checkout-service"
                    ),
                ),
            ],
        ),
        # 6. Faulty Deployment (payment-service post-deploy regression)
        RootCauseScenario(
            scenario_id="faulty_deployment",
            title="Payment Service Post-Deploy Regression",
            question=(
                "Why did payment-service error rate surge shortly after the recent deployment "
                "and should we roll back?"
            ),
            target_service="payment-service",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=[
                "deployment",
                "regression",
                "rollback",
                "rollout",
                "v2.4.1",
            ],
            expected_affected_services=["api-gateway", "checkout-service", "payment-service"],
            expected_doc_ids=["rb-006", "dep-2026-004", "inc-2026-006"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="api-gateway",
                    callee="checkout-service",
                    call_count=260,
                    error_count=88,
                    avg_duration_ms=340.0,
                ),
                ServiceEdgeInput(
                    caller="checkout-service",
                    callee="payment-service",
                    call_count=260,
                    error_count=94,
                    avg_duration_ms=310.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="deployment",
                    service="payment-service",
                    timestamp_utc=w_start - timedelta(minutes=18),
                    summary=(
                        "Deployment v2.4.1 (succeeded) on payment-service "
                        "(t=-18.0m vs window_start)"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="anomaly",
                    service="payment-service",
                    timestamp_utc=w_start + timedelta(minutes=2),
                    summary=(
                        "LSTM autoencoder anomaly score=11.88 > threshold=6.75; "
                        "top_feature=error_rate (0.36) immediately after v2.4.1 rollout"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-003",
                    signal_type="event",
                    service="payment-service",
                    timestamp_utc=w_start + timedelta(minutes=3),
                    summary=(
                        "[ERROR] Schema regression introduced in payment-service v2.4.1 "
                        "deployment; immediate rollback recommended"
                    ),
                ),
            ],
        ),
        # 7. Message Queue Backlog (kafka-broker -> order-service)
        RootCauseScenario(
            scenario_id="message_queue_backlog",
            title="Kafka Order Events Consumer Backlog",
            question=(
                "Why is kafka-broker queue_depth surging on order-events and delaying "
                "order confirmations?"
            ),
            target_service="kafka-broker",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=[
                "kafka",
                "backlog",
                "queue_depth",
                "consumer",
                "lag",
                "order-events",
            ],
            expected_affected_services=["kafka-broker", "order-service"],
            expected_doc_ids=["rb-007", "inc-2026-007", "arch-005"],
            expect_abstention=False,
            edges=[
                ServiceEdgeInput(
                    caller="order-service",
                    callee="kafka-broker",
                    call_count=510,
                    error_count=19,
                    avg_duration_ms=42.0,
                ),
            ],
            evidence_items=[
                EvidenceItem(
                    evidence_id="ev-001",
                    signal_type="anomaly",
                    service="kafka-broker",
                    timestamp_utc=w_start + timedelta(minutes=4),
                    summary=(
                        "LSTM autoencoder anomaly score=10.74 > threshold=6.75; "
                        "top_feature=queue_depth (690 pending messages on order-events)"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-002",
                    signal_type="event",
                    service="kafka-broker",
                    timestamp_utc=w_start + timedelta(minutes=5),
                    summary=(
                        "[ERROR] Consumer lag rising on topic order-events partition 4 "
                        "due to poison-pill UnicodeDecodeError blocking consumer group"
                    ),
                ),
                EvidenceItem(
                    evidence_id="ev-003",
                    signal_type="incident",
                    service="kafka-broker",
                    timestamp_utc=w_start + timedelta(minutes=7),
                    summary="Incident (HIGH/open): Kafka Order Events Consumer Backlog",
                ),
            ],
        ),
        # 8. Zero-Evidence Abstention Scenario (checkout-service, empty actionable signals)
        RootCauseScenario(
            scenario_id="insufficient_evidence_abstention",
            title="Zero-Evidence Abstention Verification",
            question="Why did checkout-service fail during this quiet maintenance window?",
            target_service="checkout-service",
            window_start=w_start,
            window_end=w_end,
            expected_root_cause_keywords=[],
            expected_affected_services=["api-gateway", "checkout-service"],
            expected_doc_ids=[],
            expect_abstention=True,
            edges=[
                ServiceEdgeInput(
                    caller="api-gateway",
                    callee="checkout-service",
                    call_count=10,
                    error_count=0,
                    avg_duration_ms=18.0,
                ),
            ],
            evidence_items=[],
        ),
    ]
