import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc
from services.investigation.llm_client import LLMClient, LLMClientError
from services.investigation.prompts import SYSTEM_PROMPT, build_investigation_user_prompt
from services.investigation.schemas import (
    CitationRef,
    EvidenceBundle,
    EvidenceItem,
    InvestigationReport,
    LLMInvestigationDraft,
)
from services.investigation.validator import DEFAULT_ABSTENTION_REASON, build_and_validate_report

CollectSignalsFn = Callable[
    [str, datetime, datetime, int, int],
    tuple[list[str], list[EvidenceItem]],
]
RetrieveKnowledgeResult = (
    tuple[list[CitationRef], dict[str, str]]
    | tuple[list[CitationRef], dict[str, str], list[str]]
)
RetrieveKnowledgeFn = Callable[[str, str, int], RetrieveKnowledgeResult]

DETERMINISTIC_ABSTENTION_MODEL = "deterministic-abstention"


class WorkflowResult(BaseModel):
    """Completed output of a bounded LangGraph investigation workflow run."""

    report: InvestigationReport
    evidence_bundle: EvidenceBundle
    executed_nodes: list[str] = Field(default_factory=list)
    duration_ms: float = Field(ge=0.0)


class InvestigationState(TypedDict, total=False):
    question: str
    target_service: str
    window_start: datetime
    window_end: datetime
    lookback_minutes: int
    max_depth: int
    top_k_docs: int
    evidence_bundle: EvidenceBundle
    retrieval_warnings: list[str]
    draft: LLMInvestigationDraft | None
    llm_error: str | None
    report: InvestigationReport
    executed_nodes: list[str]


def _build_retrieval_query(
    question: str,
    target_service: str,
    evidence_items: Sequence[EvidenceItem],
    *,
    max_chars: int = 450,
) -> str:
    """Build a focused hybrid-retrieval query from the question, service, and top signals."""
    parts: list[str] = [question.strip(), target_service]
    priority_order = {"incident": 0, "anomaly": 1, "event": 2, "deployment": 3, "dependency": 4}
    ranked = sorted(
        evidence_items,
        key=lambda item: (priority_order.get(item.signal_type, 5), item.evidence_id),
    )
    for item in ranked[:3]:
        parts.append(f"{item.service} {item.summary}")

    combined = " ".join(" ".join(p.split()) for p in parts if p.strip())
    if len(combined) > max_chars:
        return combined[:max_chars].rstrip()
    return combined


def _deduplicate_citations(citations: Sequence[CitationRef]) -> list[CitationRef]:
    seen: set[str] = set()
    unique: list[CitationRef] = []
    for ref in citations:
        if ref.doc_id not in seen:
            seen.add(ref.doc_id)
            unique.append(ref)
    return unique


def _build_deterministic_report(
    bundle: EvidenceBundle,
    *,
    model_name: str,
    abstention_reason: str,
    extra_warnings: Sequence[str] = (),
) -> InvestigationReport:
    """Build a deterministic, grounded fallback/abstention report directly from EvidenceBundle."""
    confirmed_facts: list[str] = []
    detected_anomalies: list[str] = []
    observed_correlations: list[str] = []

    for item in bundle.evidence_items:
        ts = item.timestamp_utc.isoformat() if item.timestamp_utc is not None else "n/a"
        line = f"[{item.evidence_id}] ({item.service} @ {ts}) {item.summary}"
        if item.signal_type in {"event", "incident"}:
            confirmed_facts.append(line)
        elif item.signal_type == "anomaly":
            detected_anomalies.append(line)
        elif item.signal_type in {"deployment", "dependency"}:
            observed_correlations.append(line)
        else:
            confirmed_facts.append(line)

    citations = _deduplicate_citations(bundle.retrieved_citations)

    uncertainty: list[str] = [abstention_reason]
    for warning in extra_warnings:
        if warning not in uncertainty:
            uncertainty.append(warning)
    if not bundle.has_actionable_evidence:
        uncertainty.append(
            f"No actionable events, anomalies, deployments, or incidents were recorded for "
            f"{bundle.target_service} or its related services in the requested time window."
        )
        if citations:
            doc_list = ", ".join(ref.doc_id for ref in citations)
            uncertainty.append(
                f"Retrieved potentially relevant reference documentation ({doc_list}) "
                "for manual review, but no live telemetry corroborated an active fault."
            )

    next_steps: list[str] = []
    if not bundle.has_actionable_evidence:
        next_steps.append(
            f"Verify telemetry ingestion and expand the time window for {bundle.target_service}."
        )
    else:
        next_steps.append(
            "Review the confirmed facts, anomalies, and correlated signals above manually "
            "or retry once local LLM synthesis is available."
        )
    if citations:
        top_docs = ", ".join(ref.doc_id for ref in citations[:3])
        next_steps.append(f"Consult cited operational documentation: {top_docs}.")

    return InvestigationReport(
        question=bundle.question,
        target_service=bundle.target_service,
        window_start=bundle.window_start,
        window_end=bundle.window_end,
        model_name=model_name,
        confirmed_facts=confirmed_facts,
        detected_anomalies=detected_anomalies,
        observed_correlations=observed_correlations,
        probable_root_causes=[],
        citations=citations,
        recommended_next_steps=next_steps,
        uncertainty_and_gaps=uncertainty,
        abstained=True,
        abstention_reason=abstention_reason,
        validation_warnings=list(extra_warnings),
    )


def build_investigation_graph(
    *,
    collect_signals: CollectSignalsFn,
    retrieve_knowledge: RetrieveKnowledgeFn,
    llm_client: LLMClient,
) -> Any:
    """Compile a strictly bounded acyclic LangGraph StateGraph for incident investigation."""

    def plan_investigation(state: InvestigationState) -> InvestigationState:
        question = state["question"].strip()
        if not question:
            raise ValueError("question cannot be empty")

        target_service = normalize_service_name(state["target_service"])
        window_start = normalize_timestamp_utc(state["window_start"])
        window_end = normalize_timestamp_utc(state["window_end"])
        if window_start > window_end:
            raise ValueError("window_start must be less than or equal to window_end")

        lookback_minutes = int(state.get("lookback_minutes", 60))
        max_depth = int(state.get("max_depth", 3))
        top_k_docs = int(state.get("top_k_docs", 5))
        if lookback_minutes < 0:
            raise ValueError("lookback_minutes must be non-negative")
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if top_k_docs < 1:
            raise ValueError("top_k_docs must be at least 1")

        executed = [*state.get("executed_nodes", []), "plan_investigation"]
        return {
            "question": question,
            "target_service": target_service,
            "window_start": window_start,
            "window_end": window_end,
            "lookback_minutes": lookback_minutes,
            "max_depth": max_depth,
            "top_k_docs": top_k_docs,
            "executed_nodes": executed,
        }

    def collect_evidence(state: InvestigationState) -> InvestigationState:
        target_service = state["target_service"]
        window_start = state["window_start"]
        window_end = state["window_end"]
        lookback_minutes = state["lookback_minutes"]
        max_depth = state["max_depth"]
        top_k_docs = state["top_k_docs"]

        related_services, evidence_items = collect_signals(
            target_service,
            window_start,
            window_end,
            lookback_minutes,
            max_depth,
        )

        retrieval_query = _build_retrieval_query(
            state["question"], target_service, evidence_items
        )
        kb_result = retrieve_knowledge(retrieval_query, target_service, top_k_docs)
        if len(kb_result) == 3:
            citations, snippets, retrieval_warnings = kb_result
        else:
            citations, snippets = kb_result
            retrieval_warnings = []

        bundle = EvidenceBundle(
            question=state["question"],
            target_service=target_service,
            window_start=window_start,
            window_end=window_end,
            related_services=list(related_services),
            evidence_items=list(evidence_items),
            retrieved_citations=list(citations),
            retrieved_snippets=dict(snippets),
        )
        executed = [*state.get("executed_nodes", []), "collect_evidence"]
        return {
            "evidence_bundle": bundle,
            "retrieval_warnings": list(retrieval_warnings),
            "executed_nodes": executed,
        }

    def route_after_evidence(state: InvestigationState) -> str:
        if not state["evidence_bundle"].has_actionable_evidence:
            return "abstain_no_evidence"
        return "synthesize_report"

    def abstain_no_evidence(state: InvestigationState) -> InvestigationState:
        bundle = state["evidence_bundle"]
        warnings = list(state.get("retrieval_warnings", []))
        report = _build_deterministic_report(
            bundle,
            model_name=DETERMINISTIC_ABSTENTION_MODEL,
            abstention_reason=DEFAULT_ABSTENTION_REASON,
            extra_warnings=warnings,
        )
        executed = [*state.get("executed_nodes", []), "abstain_no_evidence"]
        return {
            "report": report,
            "executed_nodes": executed,
        }

    def synthesize_report(state: InvestigationState) -> InvestigationState:
        bundle = state["evidence_bundle"]
        user_prompt = build_investigation_user_prompt(bundle)
        executed = [*state.get("executed_nodes", []), "synthesize_report"]
        try:
            draft = llm_client.generate_structured(
                SYSTEM_PROMPT,
                user_prompt,
                LLMInvestigationDraft,
            )
        except LLMClientError as exc:
            return {
                "draft": None,
                "llm_error": str(exc),
                "executed_nodes": executed,
            }

        return {
            "draft": draft,
            "llm_error": None,
            "executed_nodes": executed,
        }

    def validate_evidence(state: InvestigationState) -> InvestigationState:
        bundle = state["evidence_bundle"]
        retrieval_warnings = list(state.get("retrieval_warnings", []))
        llm_error = state.get("llm_error")
        draft = state.get("draft")
        executed = [*state.get("executed_nodes", []), "validate_evidence"]

        if llm_error is not None or draft is None:
            reason = f"LLM synthesis unavailable: {llm_error or 'no draft returned'}"
            warnings = [*retrieval_warnings, reason]
            report = _build_deterministic_report(
                bundle,
                model_name=llm_client.model_name,
                abstention_reason=reason,
                extra_warnings=warnings,
            )
            return {"report": report, "executed_nodes": executed}

        report = build_and_validate_report(draft, bundle, llm_client.model_name)
        if retrieval_warnings:
            combined_warnings = [*report.validation_warnings, *retrieval_warnings]
            combined_gaps = [
                *report.uncertainty_and_gaps,
                *(w for w in retrieval_warnings if w not in report.uncertainty_and_gaps),
            ]
            report = report.model_copy(
                update={
                    "validation_warnings": combined_warnings,
                    "uncertainty_and_gaps": combined_gaps,
                }
            )
        return {"report": report, "executed_nodes": executed}

    graph = StateGraph(InvestigationState)
    graph.add_node("plan_investigation", plan_investigation)
    graph.add_node("collect_evidence", collect_evidence)
    graph.add_node("abstain_no_evidence", abstain_no_evidence)
    graph.add_node("synthesize_report", synthesize_report)
    graph.add_node("validate_evidence", validate_evidence)

    graph.add_edge(START, "plan_investigation")
    graph.add_edge("plan_investigation", "collect_evidence")
    graph.add_conditional_edges(
        "collect_evidence",
        route_after_evidence,
        {
            "abstain_no_evidence": "abstain_no_evidence",
            "synthesize_report": "synthesize_report",
        },
    )
    graph.add_edge("abstain_no_evidence", END)
    graph.add_edge("synthesize_report", "validate_evidence")
    graph.add_edge("validate_evidence", END)

    return graph.compile()


def run_investigation_workflow(
    *,
    question: str,
    target_service: str,
    window_start: datetime,
    window_end: datetime,
    collect_signals: CollectSignalsFn,
    retrieve_knowledge: RetrieveKnowledgeFn,
    llm_client: LLMClient,
    lookback_minutes: int = 60,
    max_depth: int = 3,
    top_k_docs: int = 5,
) -> WorkflowResult:
    """Execute the bounded LangGraph investigation workflow and return full audit output."""
    compiled = build_investigation_graph(
        collect_signals=collect_signals,
        retrieve_knowledge=retrieve_knowledge,
        llm_client=llm_client,
    )
    initial_state: InvestigationState = {
        "question": question,
        "target_service": target_service,
        "window_start": window_start,
        "window_end": window_end,
        "lookback_minutes": lookback_minutes,
        "max_depth": max_depth,
        "top_k_docs": top_k_docs,
        "executed_nodes": [],
    }
    t0 = time.perf_counter()
    final_state: InvestigationState = compiled.invoke(initial_state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return WorkflowResult(
        report=final_state["report"],
        evidence_bundle=final_state["evidence_bundle"],
        executed_nodes=list(final_state["executed_nodes"]),
        duration_ms=round(elapsed_ms, 3),
    )
