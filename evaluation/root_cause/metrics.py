from collections.abc import Sequence

from pydantic import BaseModel, Field

from evaluation.retrieval.metrics import percentile
from services.investigation import EvidenceBundle, InvestigationReport


class ScenarioEvalResult(BaseModel):
    scenario_id: str
    target_service: str
    expect_abstention: bool
    abstained: bool
    executed_nodes: list[str]
    top_1_hit: float | None = None
    top_3_hit: float | None = None
    affected_service_f1: float
    citation_correctness: float
    kb_expected_doc_recall: float | None = None
    unsupported_root_cause_rate: float
    evidence_coverage: float
    abstention_correctness: float
    validation_warning_count: int
    duration_ms: float


class AdversarialProbeResult(BaseModel):
    injected_fake_doc_ids: int
    stripped_fake_doc_ids: int
    injected_fake_evidence_ids: int
    stripped_fake_evidence_ids: int
    injected_unsupported_hypotheses: int
    dropped_unsupported_hypotheses: int
    post_validation_unsupported_rate: float
    post_validation_citation_correctness: float
    empty_evidence_short_circuited: bool
    empty_evidence_llm_calls: int
    empty_evidence_validator_forced_abstention: bool
    guardrail_interception_rate: float


class EndToEndEvalSummary(BaseModel):
    is_synthetic_evaluation: bool = True
    evaluation_mode: str
    model_name: str
    disclaimer: str
    num_scenarios: int
    num_fault_scenarios: int
    top_1_root_cause_accuracy: float
    top_3_root_cause_accuracy: float
    mean_affected_service_f1: float
    mean_citation_correctness: float
    mean_kb_expected_doc_recall: float
    mean_unsupported_root_cause_rate: float
    mean_evidence_coverage: float
    abstention_accuracy: float
    p50_duration_ms: float
    p95_duration_ms: float
    adversarial_probe: AdversarialProbeResult | None = None
    scenarios: list[ScenarioEvalResult] = Field(default_factory=list)


def top_k_root_cause_hit(
    report: InvestigationReport,
    expected_keywords: Sequence[str],
    k: int = 1,
) -> float:
    """Return 1.0 if any of the top-k hypotheses matches any expected root-cause keyword."""
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    cleaned_keywords = [kw.strip().lower() for kw in expected_keywords if kw.strip()]
    if not cleaned_keywords:
        raise ValueError("expected_keywords must contain at least one non-empty keyword")

    for hyp in report.probable_root_causes[:k]:
        text = hyp.hypothesis.lower()
        if any(kw in text for kw in cleaned_keywords):
            return 1.0
    return 0.0


def affected_service_f1(
    predicted_services: Sequence[str],
    expected_services: Sequence[str],
) -> float:
    """Compute set-based F1 between predicted and expected affected service names."""
    pred = {s.strip().lower() for s in predicted_services if s.strip()}
    exp = {s.strip().lower() for s in expected_services if s.strip()}
    if not pred and not exp:
        return 1.0
    if not pred or not exp:
        return 0.0
    tp = len(pred & exp)
    if tp == 0:
        return 0.0
    precision = tp / len(pred)
    recall = tp / len(exp)
    return (2.0 * precision * recall) / (precision + recall)


def citation_correctness(
    report: InvestigationReport,
    allowed_doc_ids: set[str],
) -> float:
    """Fraction of cited docs present in allowed retrieved doc_ids with complete metadata."""
    if not report.citations:
        return 1.0
    valid = 0
    for cit in report.citations:
        fields_ok = all(
            isinstance(val, str) and bool(val.strip())
            for val in (
                cit.doc_id,
                cit.doc_type,
                cit.title,
                cit.section_heading,
                cit.source_path,
            )
        )
        if cit.doc_id in allowed_doc_ids and fields_ok and isinstance(cit.is_synthetic, bool):
            valid += 1
    return valid / len(report.citations)


def unsupported_root_cause_rate(
    report: InvestigationReport,
    allowed_evidence_ids: set[str],
) -> float:
    """Fraction of hypotheses lacking valid supporting_evidence_ids or claiming causation."""
    if not report.probable_root_causes:
        return 0.0
    unsupported = 0
    for hyp in report.probable_root_causes:
        ids_valid = bool(hyp.supporting_evidence_ids) and all(
            ev_id in allowed_evidence_ids for ev_id in hyp.supporting_evidence_ids
        )
        if not ids_valid or hyp.is_proven_causation is not False:
            unsupported += 1
    return unsupported / len(report.probable_root_causes)


def evidence_coverage(
    report: InvestigationReport,
    bundle: EvidenceBundle,
) -> float:
    """Fraction of emitted hypotheses and confirmed facts grounded in bundle evidence."""
    total_claims = len(report.probable_root_causes) + len(report.confirmed_facts)
    if total_claims == 0:
        return 1.0 if report.abstained else 0.0

    allowed_ids = bundle.allowed_evidence_ids
    bundle_services = {item.service.lower() for item in bundle.evidence_items}

    covered = 0
    for hyp in report.probable_root_causes:
        if hyp.supporting_evidence_ids and all(
            ev_id in allowed_ids for ev_id in hyp.supporting_evidence_ids
        ):
            covered += 1

    for fact in report.confirmed_facts:
        lower_fact = fact.lower()
        has_id = any(ev_id.lower() in lower_fact for ev_id in allowed_ids)
        has_service = any(svc in lower_fact for svc in bundle_services)
        if has_id or has_service:
            covered += 1

    return covered / total_claims


def abstention_correctness(
    report: InvestigationReport,
    expect_abstention: bool,
) -> float:
    """Return 1.0 when abstention behavior matches ground-truth expectation."""
    if expect_abstention:
        return 1.0 if (report.abstained and len(report.probable_root_causes) == 0) else 0.0
    return 1.0 if (not report.abstained and len(report.probable_root_causes) >= 1) else 0.0


def aggregate_e2e_results(
    results: Sequence[ScenarioEvalResult],
    *,
    evaluation_mode: str,
    model_name: str,
    disclaimer: str,
    adversarial_probe: AdversarialProbeResult | None = None,
) -> EndToEndEvalSummary:
    """Aggregate per-scenario metrics into an EndToEndEvalSummary."""
    if not results:
        raise ValueError("results cannot be empty")

    fault_rows = [row for row in results if not row.expect_abstention]
    if not fault_rows:
        raise ValueError("results must contain at least one non-abstention fault scenario")

    top1_vals = [row.top_1_hit for row in fault_rows if row.top_1_hit is not None]
    top3_vals = [row.top_3_hit for row in fault_rows if row.top_3_hit is not None]
    kb_recalls = [
        row.kb_expected_doc_recall
        for row in fault_rows
        if row.kb_expected_doc_recall is not None
    ]
    durations = [row.duration_ms for row in results]
    n = len(results)

    return EndToEndEvalSummary(
        is_synthetic_evaluation=True,
        evaluation_mode=evaluation_mode,
        model_name=model_name,
        disclaimer=disclaimer,
        num_scenarios=n,
        num_fault_scenarios=len(fault_rows),
        top_1_root_cause_accuracy=round(sum(top1_vals) / len(top1_vals), 6) if top1_vals else 0.0,
        top_3_root_cause_accuracy=round(sum(top3_vals) / len(top3_vals), 6) if top3_vals else 0.0,
        mean_affected_service_f1=round(sum(r.affected_service_f1 for r in results) / n, 6),
        mean_citation_correctness=round(sum(r.citation_correctness for r in results) / n, 6),
        mean_kb_expected_doc_recall=(
            round(sum(kb_recalls) / len(kb_recalls), 6) if kb_recalls else 0.0
        ),
        mean_unsupported_root_cause_rate=round(
            sum(r.unsupported_root_cause_rate for r in results) / n, 6
        ),
        mean_evidence_coverage=round(sum(r.evidence_coverage for r in results) / n, 6),
        abstention_accuracy=round(sum(r.abstention_correctness for r in results) / n, 6),
        p50_duration_ms=round(percentile(durations, 50.0), 3),
        p95_duration_ms=round(percentile(durations, 95.0), 3),
        adversarial_probe=adversarial_probe,
        scenarios=list(results),
    )
