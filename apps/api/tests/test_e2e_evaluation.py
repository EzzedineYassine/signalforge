import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evaluation.root_cause import (
    ScenarioEvalResult,
    abstention_correctness,
    affected_service_f1,
    aggregate_e2e_results,
    citation_correctness,
    evidence_coverage,
    get_e2e_scenarios,
    top_k_root_cause_hit,
    unsupported_root_cause_rate,
)
from services.investigation import (
    CitationRef,
    EvidenceBundle,
    EvidenceItem,
    FakeLLMClient,
    InvestigationReport,
    LLMInvestigationDraft,
    RootCauseHypothesis,
    run_investigation_workflow,
)
from services.retrieval.ingestion.loader import load_knowledge_base

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"


def _make_report(
    *,
    hypotheses: list[RootCauseHypothesis] | None = None,
    citations: list[CitationRef] | None = None,
    confirmed_facts: list[str] | None = None,
    abstained: bool = False,
) -> InvestigationReport:
    t0 = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    return InvestigationReport(
        question="Why did checkout fail?",
        target_service="checkout-service",
        window_start=t0,
        window_end=t0 + timedelta(minutes=30),
        model_name="test-llm",
        confirmed_facts=confirmed_facts if confirmed_facts is not None else [],
        probable_root_causes=hypotheses if hypotheses is not None else [],
        citations=citations if citations is not None else [],
        abstained=abstained,
        abstention_reason="No evidence" if abstained else None,
    )


# ---------------------------------------------------------------------------
# 1. Hand-calculated metric unit tests
# ---------------------------------------------------------------------------


def test_hand_calculated_root_cause_metrics() -> None:
    valid_cit = CitationRef(
        doc_id="rb-002",
        doc_type="runbook",
        title="Postgres Connection Pool Exhaustion",
        section_heading="Triage",
        source_path="runbooks/rb-002.md",
        is_synthetic=True,
    )
    fake_cit = CitationRef(
        doc_id="rb-999-fake",
        doc_type="runbook",
        title="Fabricated",
        section_heading="None",
        source_path="fake.md",
        is_synthetic=True,
    )

    h1 = RootCauseHypothesis(
        hypothesis="Unrelated DNS timeout.",
        confidence="low",
        supporting_evidence_ids=["ev-001"],
    )
    h2 = RootCauseHypothesis(
        hypothesis="PgBouncer connection pool exhaustion caused SQLSTATE 53300.",
        confidence="high",
        supporting_evidence_ids=["ev-001", "ev-002"],
    )
    h_bad = RootCauseHypothesis(
        hypothesis="Unsupported claim.",
        confidence="low",
        supporting_evidence_ids=["ev-nonexistent"],
    )

    report = _make_report(
        hypotheses=[h1, h2],
        citations=[valid_cit, fake_cit],
        confirmed_facts=["[ev-001] postgres-primary hit 198 connections", "Unverified rumor"],
        abstained=False,
    )

    # Top-1 misses ("DNS timeout"), Top-3 hits ("PgBouncer connection pool")
    assert top_k_root_cause_hit(report, ["pgbouncer", "53300"], k=1) == 0.0
    assert top_k_root_cause_hit(report, ["pgbouncer", "53300"], k=3) == 1.0

    with pytest.raises(ValueError, match="k must be at least 1"):
        top_k_root_cause_hit(report, ["pgbouncer"], k=0)
    with pytest.raises(ValueError, match="expected_keywords"):
        top_k_root_cause_hit(report, [], k=1)

    # Affected service F1: pred={a, b, c}, exp={b, c, d} -> tp=2, prec=2/3, rec=2/3, F1=2/3
    assert math.isclose(
        affected_service_f1(["a", "b", "c"], ["b", "c", "d"]),
        2.0 / 3.0,
        rel_tol=1e-12,
    )
    assert affected_service_f1([], []) == 1.0
    assert affected_service_f1(["a"], []) == 0.0

    # Citation correctness: 1 valid out of 2 -> 0.5
    assert math.isclose(citation_correctness(report, {"rb-002"}), 0.5, rel_tol=1e-12)
    assert citation_correctness(_make_report(citations=[]), {"rb-002"}) == 1.0

    # Unsupported root cause rate: 0/2 for [h1, h2], 1/2 for [h2, h_bad]
    assert unsupported_root_cause_rate(report, {"ev-001", "ev-002"}) == 0.0
    mixed_report = _make_report(hypotheses=[h2, h_bad])
    assert math.isclose(
        unsupported_root_cause_rate(mixed_report, {"ev-001", "ev-002"}),
        0.5,
        rel_tol=1e-12,
    )

    # Evidence coverage: 2 valid hypotheses + 1 grounded fact out of 4 total claims -> 3/4 = 0.75
    t0 = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    bundle = EvidenceBundle(
        question="test",
        target_service="postgres-primary",
        window_start=t0,
        window_end=t0 + timedelta(minutes=30),
        evidence_items=[
            EvidenceItem(
                evidence_id="ev-001",
                signal_type="event",
                service="postgres-primary",
                timestamp_utc=t0,
                summary="Pool full",
            ),
            EvidenceItem(
                evidence_id="ev-002",
                signal_type="anomaly",
                service="postgres-primary",
                timestamp_utc=t0,
                summary="High db_connections",
            ),
        ],
    )
    assert math.isclose(evidence_coverage(report, bundle), 0.75, rel_tol=1e-12)

    # Abstention correctness
    abstained_rep = _make_report(hypotheses=[], abstained=True)
    assert abstention_correctness(abstained_rep, expect_abstention=True) == 1.0
    assert abstention_correctness(abstained_rep, expect_abstention=False) == 0.0
    assert abstention_correctness(report, expect_abstention=False) == 1.0
    assert abstention_correctness(report, expect_abstention=True) == 0.0


def test_aggregate_e2e_results() -> None:
    rows = [
        ScenarioEvalResult(
            scenario_id="s1",
            target_service="checkout-service",
            expect_abstention=False,
            abstained=False,
            executed_nodes=[
                "plan_investigation",
                "collect_evidence",
                "synthesize_report",
                "validate_evidence",
            ],
            top_1_hit=1.0,
            top_3_hit=1.0,
            affected_service_f1=1.0,
            citation_correctness=1.0,
            kb_expected_doc_recall=1.0,
            unsupported_root_cause_rate=0.0,
            evidence_coverage=1.0,
            abstention_correctness=1.0,
            validation_warning_count=0,
            duration_ms=10.0,
        ),
        ScenarioEvalResult(
            scenario_id="s_abstain",
            target_service="checkout-service",
            expect_abstention=True,
            abstained=True,
            executed_nodes=["plan_investigation", "collect_evidence", "abstain_no_evidence"],
            top_1_hit=None,
            top_3_hit=None,
            affected_service_f1=1.0,
            citation_correctness=1.0,
            kb_expected_doc_recall=None,
            unsupported_root_cause_rate=0.0,
            evidence_coverage=1.0,
            abstention_correctness=1.0,
            validation_warning_count=0,
            duration_ms=2.0,
        ),
    ]
    summary = aggregate_e2e_results(
        rows,
        evaluation_mode="deterministic",
        model_name="fake-llm",
        disclaimer="synthetic",
    )
    assert summary.num_scenarios == 2
    assert summary.num_fault_scenarios == 1
    assert summary.top_1_root_cause_accuracy == 1.0
    assert summary.abstention_accuracy == 1.0


# ---------------------------------------------------------------------------
# 2. Ground-truth integrity across all 8 synthetic scenarios
# ---------------------------------------------------------------------------


def test_all_8_scenarios_have_valid_ground_truth_and_existing_kb_doc_ids() -> None:
    scenarios = get_e2e_scenarios()
    assert len(scenarios) == 8

    expected_ids = {
        "cpu_saturation",
        "db_connection_exhaustion",
        "payment_service_failure",
        "memory_leak",
        "network_latency",
        "faulty_deployment",
        "message_queue_backlog",
        "insufficient_evidence_abstention",
    }
    assert {s.scenario_id for s in scenarios} == expected_ids

    corpus_doc_ids = {doc.doc_id for doc in load_knowledge_base(SYNTHETIC_KB_DIR)}
    assert len(corpus_doc_ids) == 37

    for scenario in scenarios:
        assert scenario.is_synthetic is True
        assert scenario.question.strip()
        assert scenario.target_service.strip()
        assert scenario.window_start < scenario.window_end
        assert len(scenario.expected_affected_services) >= 1
        missing_docs = [d for d in scenario.expected_doc_ids if d not in corpus_doc_ids]
        assert missing_docs == [], f"{scenario.scenario_id} unknown doc_ids: {missing_docs}"
        if scenario.expect_abstention:
            assert scenario.evidence_items == []
        else:
            assert len(scenario.expected_root_cause_keywords) >= 1
            assert len(scenario.expected_doc_ids) >= 1
            assert len(scenario.evidence_items) >= 2


# ---------------------------------------------------------------------------
# 3. Adversarial hallucination interception through LangGraph workflow
# ---------------------------------------------------------------------------


def test_adversarial_hallucination_probe_intercepted_by_langgraph_validator() -> None:
    scenarios = get_e2e_scenarios()
    db_scenario = next(s for s in scenarios if s.scenario_id == "db_connection_exhaustion")
    empty_scenario = next(s for s in scenarios if s.expect_abstention)

    valid_ref = CitationRef(
        doc_id="rb-002",
        doc_type="runbook",
        title="PostgreSQL Connection Pool Exhaustion Runbook",
        section_heading="Triage",
        source_path="runbooks/rb-002-postgres-connection-pool.md",
        is_synthetic=True,
    )

    def retrieve_stub(_q: str, _s: str, _k: int) -> tuple[list[CitationRef], dict[str, str]]:
        return [valid_ref], {"rb-002": "Check PgBouncer pool saturation."}

    # Probe 1: Fabricated doc_ids, fabricated evidence_ids, and unsupported hypothesis
    adversarial_llm = FakeLLMClient(
        response=LLMInvestigationDraft(
            confirmed_facts=["[ev-002] SQLSTATE 53300 observed on postgres-primary."],
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="PgBouncer pool exhausted (mixed valid + fake evidence ID).",
                    confidence="high",
                    supporting_evidence_ids=["ev-001", "ev-fake-999"],
                ),
                RootCauseHypothesis(
                    hypothesis="Fabricated root cause with zero real evidence.",
                    confidence="high",
                    supporting_evidence_ids=["ev-does-not-exist"],
                ),
            ],
            cited_doc_ids=["rb-002", "rb-999-fabricated", "inc-9999-fake"],
            abstained=False,
        ),
        model_name="adversarial-fake-llm",
    )

    wf = run_investigation_workflow(
        question=db_scenario.question,
        target_service=db_scenario.target_service,
        window_start=db_scenario.window_start,
        window_end=db_scenario.window_end,
        collect_signals=lambda _s, _a, _b, _l, _d: (
            ["payment-service", "order-service", "checkout-service"],
            list(db_scenario.evidence_items),
        ),
        retrieve_knowledge=retrieve_stub,
        llm_client=adversarial_llm,
    )

    assert wf.executed_nodes == [
        "plan_investigation",
        "collect_evidence",
        "synthesize_report",
        "validate_evidence",
    ]
    assert unsupported_root_cause_rate(wf.report, wf.evidence_bundle.allowed_evidence_ids) == 0.0
    assert citation_correctness(wf.report, wf.evidence_bundle.allowed_doc_ids) == 1.0
    assert [c.doc_id for c in wf.report.citations] == ["rb-002"]
    assert len(wf.report.probable_root_causes) == 1
    assert wf.report.probable_root_causes[0].supporting_evidence_ids == ["ev-001"]
    assert len(wf.report.validation_warnings) >= 3

    # Probe 2: Empty-evidence scenario short-circuits before calling adversarial LLM
    empty_llm = FakeLLMClient(
        response=LLMInvestigationDraft(
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="Should never be emitted.",
                    confidence="high",
                    supporting_evidence_ids=["ev-fake"],
                )
            ],
            abstained=False,
        ),
        model_name="adversarial-empty-llm",
    )
    wf_empty = run_investigation_workflow(
        question=empty_scenario.question,
        target_service=empty_scenario.target_service,
        window_start=empty_scenario.window_start,
        window_end=empty_scenario.window_end,
        collect_signals=lambda _s, _a, _b, _l, _d: (["api-gateway"], []),
        retrieve_knowledge=retrieve_stub,
        llm_client=empty_llm,
    )
    assert len(empty_llm.calls) == 0
    assert wf_empty.executed_nodes == [
        "plan_investigation",
        "collect_evidence",
        "abstain_no_evidence",
    ]
    assert wf_empty.report.abstained is True
    assert wf_empty.report.probable_root_causes == []
    assert abstention_correctness(wf_empty.report, expect_abstention=True) == 1.0
