import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.api.src.core.config import get_settings
from evaluation.root_cause import (
    AdversarialProbeResult,
    RootCauseScenario,
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
from services.graph import analyze_service_impact, build_service_dependency_graph
from services.investigation import (
    CitationRef,
    EvidenceBundle,
    FakeLLMClient,
    LLMClient,
    LLMInvestigationDraft,
    OllamaClient,
    RootCauseHypothesis,
    build_and_validate_report,
    run_investigation_workflow,
)
from services.retrieval.chunking.markdown_chunker import MarkdownChunker
from services.retrieval.embeddings.embedder import SentenceTransformerEmbedder
from services.retrieval.ingestion.loader import load_knowledge_base
from services.retrieval.search.bm25_index import BM25Index
from services.retrieval.search.hybrid import HybridRetriever
from services.retrieval.search.qdrant_store import QdrantVectorStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "experiments" / "benchmarks" / "end_to_end" / "e2e_results.json"
DEFAULT_COLLECTION = "signalforge_e2e_eval"

_EVIDENCE_LINE_RE = re.compile(r"^- \[([^\]]+)\] \(([^|]+)\|([^|]+)\|([^)]+)\) (.+)$")
_CITATION_LINE_RE = re.compile(r"^- \[([^\]]+)\] \(([^)]+)\) (.+)$")


def _parse_prompt_sections(user_prompt: str) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Extract only evidence items and citation doc_ids present in the rendered prompt."""
    evidence_rows: list[tuple[str, str, str, str]] = []
    doc_ids: list[str] = []
    section = ""
    for line in user_prompt.splitlines():
        stripped = line.strip()
        if stripped == "=== EVIDENCE ITEMS ===":
            section = "evidence"
            continue
        if stripped == "=== KNOWLEDGE CITATIONS ===":
            section = "citations"
            continue
        if stripped.startswith("=== "):
            section = ""
            continue

        if section == "evidence":
            match = _EVIDENCE_LINE_RE.match(stripped)
            if match:
                ev_id, sig_type, svc, _ts, summary = match.groups()
                evidence_rows.append(
                    (ev_id.strip(), sig_type.strip(), svc.strip(), summary.strip())
                )
        elif section == "citations":
            match = _CITATION_LINE_RE.match(stripped)
            if match:
                doc_ids.append(match.group(1).strip())
    return evidence_rows, doc_ids


def _make_deterministic_prompt_grounded_llm() -> FakeLLMClient:
    """Build a deterministic LLM double that reasons strictly from rendered prompt text."""

    def _respond(_sys: str, user_prompt: str, _cls: type[Any]) -> LLMInvestigationDraft:
        evidence_rows, doc_ids = _parse_prompt_sections(user_prompt)
        actionable_rows = [
            row
            for row in evidence_rows
            if row[1] in {"event", "anomaly", "deployment", "incident"}
        ]
        if not actionable_rows:
            return LLMInvestigationDraft(
                uncertainty_and_gaps=["No actionable operational signals present in prompt."],
                abstained=True,
                abstention_reason="No actionable operational telemetry found in window.",
            )

        facts = [
            f"[{ev_id}] ({svc}) {summary}"
            for ev_id, sig, svc, summary in actionable_rows
            if sig in {"event", "incident"}
        ]
        anomalies = [
            f"[{ev_id}] ({svc}) {summary}"
            for ev_id, sig, svc, summary in actionable_rows
            if sig == "anomaly"
        ]
        correlations = [
            f"[{ev_id}] ({svc}) {summary}"
            for ev_id, sig, svc, summary in evidence_rows
            if sig in {"deployment", "dependency"}
        ]

        # Synthesize primary candidate hypothesis strictly from observed prompt evidence.
        key_summaries = "; ".join(summary for _, _, _, summary in actionable_rows[:2])
        supporting_ids = [ev_id for ev_id, _, _, _ in actionable_rows[:3]]
        primary = RootCauseHypothesis(
            hypothesis=f"Observed fault signature supported by telemetry: {key_summaries}",
            confidence="medium",
            supporting_evidence_ids=supporting_ids,
            is_proven_causation=False,
        )

        cited = doc_ids[:3]
        next_steps = [
            f"Follow remediation checks in cited documentation ({', '.join(cited)})."
            if cited
            else "Collect targeted diagnostic logs and metrics for the affected service."
        ]

        fallback_facts = [
            f"[{ev_id}] ({svc}) {summary}" for ev_id, _, svc, summary in actionable_rows[:1]
        ]
        return LLMInvestigationDraft(
            confirmed_facts=facts or fallback_facts,
            detected_anomalies=anomalies,
            observed_correlations=correlations,
            probable_root_causes=[primary],
            cited_doc_ids=cited,
            recommended_next_steps=next_steps,
            uncertainty_and_gaps=[
                "Synthesized via deterministic prompt-grounded double; "
                "live LLM semantic synthesis requires --mode ollama."
            ],
            abstained=False,
        )

    return FakeLLMClient(response=_respond, model_name="deterministic-grounded-double")


def _make_scenario_collect_signals(scenario: RootCauseScenario):
    def _collect_signals(
        service: str,
        _window_start: Any,
        _window_end: Any,
        _lookback_minutes: int,
        max_depth: int,
    ) -> tuple[list[str], list[Any]]:
        graph = build_service_dependency_graph(scenario.edges, services=[scenario.target_service])
        impact = analyze_service_impact(graph, service, max_depth=max_depth)
        return list(impact.all_related_services), list(scenario.evidence_items)

    return _collect_signals


def _make_retrieve_knowledge(retriever: HybridRetriever):
    def _retrieve_knowledge(
        query: str,
        _service: str,
        top_k: int,
    ) -> tuple[list[CitationRef], dict[str, str]]:
        hits = retriever.search(query, top_k=top_k, mode="hybrid")
        citations: list[CitationRef] = []
        snippets: dict[str, str] = {}
        seen: set[str] = set()
        for chunk in hits:
            if chunk.doc_id in seen:
                continue
            seen.add(chunk.doc_id)
            citations.append(
                CitationRef(
                    doc_id=chunk.doc_id,
                    doc_type=chunk.doc_type,
                    title=chunk.title,
                    section_heading=chunk.section_heading,
                    source_path=chunk.source_path,
                    is_synthetic=chunk.is_synthetic,
                )
            )
            snippets[chunk.doc_id] = chunk.text
        return citations, snippets

    return _retrieve_knowledge


def run_adversarial_hallucination_probe(
    scenarios: Sequence[RootCauseScenario],
    retriever: HybridRetriever,
) -> AdversarialProbeResult:
    """Inject fabricated doc_ids, evidence_ids, and unsupported hypotheses to verify guardrails."""
    db_scenario = next(s for s in scenarios if s.scenario_id == "db_connection_exhaustion")
    empty_scenario = next(s for s in scenarios if s.expect_abstention)

    def _adversarial_responder(
        _sys: str, user_prompt: str, _cls: type[Any]
    ) -> LLMInvestigationDraft:
        evidence_rows, doc_ids = _parse_prompt_sections(user_prompt)
        valid_ev = evidence_rows[0][0] if evidence_rows else "ev-001"
        valid_doc = doc_ids[0] if doc_ids else "rb-002"
        return LLMInvestigationDraft(
            confirmed_facts=[f"[{valid_ev}] PgBouncer connection exhaustion observed."],
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="PgBouncer connection pool exhaustion with one fabricated ID.",
                    confidence="high",
                    supporting_evidence_ids=[valid_ev, "ev-hallucinated-999"],
                ),
                RootCauseHypothesis(
                    hypothesis="Completely hallucinated DNS hijack with zero real evidence.",
                    confidence="high",
                    supporting_evidence_ids=["ev-fake-1", "ev-fake-2"],
                ),
            ],
            cited_doc_ids=[valid_doc, "rb-999-fabricated", "inc-9999-ghost"],
            abstained=False,
        )

    probe_llm = FakeLLMClient(response=_adversarial_responder, model_name="adversarial-probe-llm")
    wf_result = run_investigation_workflow(
        question=db_scenario.question,
        target_service=db_scenario.target_service,
        window_start=db_scenario.window_start,
        window_end=db_scenario.window_end,
        collect_signals=_make_scenario_collect_signals(db_scenario),
        retrieve_knowledge=_make_retrieve_knowledge(retriever),
        llm_client=probe_llm,
        top_k_docs=5,
    )

    report = wf_result.report
    bundle = wf_result.evidence_bundle
    cited_ids = {c.doc_id for c in report.citations}
    stripped_fake_docs = sum(
        1 for fake in ("rb-999-fabricated", "inc-9999-ghost") if fake not in cited_ids
    )
    remaining_ev_ids = {
        ev_id for hyp in report.probable_root_causes for ev_id in hyp.supporting_evidence_ids
    }
    stripped_fake_ev = sum(
        1
        for fake in ("ev-hallucinated-999", "ev-fake-1", "ev-fake-2")
        if fake not in remaining_ev_ids
    )
    dropped_unsupported = 1 if len(report.probable_root_causes) == 1 else 0

    # Probe B: empty-evidence scenario must short-circuit LLM and validator forces abstention
    empty_llm = FakeLLMClient(
        response=LLMInvestigationDraft(
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="Fabricated root cause on empty window.",
                    confidence="high",
                    supporting_evidence_ids=["ev-fake-empty"],
                )
            ],
            abstained=False,
        ),
        model_name="adversarial-empty-llm",
    )
    empty_wf = run_investigation_workflow(
        question=empty_scenario.question,
        target_service=empty_scenario.target_service,
        window_start=empty_scenario.window_start,
        window_end=empty_scenario.window_end,
        collect_signals=_make_scenario_collect_signals(empty_scenario),
        retrieve_knowledge=_make_retrieve_knowledge(retriever),
        llm_client=empty_llm,
        top_k_docs=5,
    )

    # Also test build_and_validate_report directly on empty EvidenceBundle
    direct_empty_bundle = EvidenceBundle(
        question=empty_scenario.question,
        target_service=empty_scenario.target_service,
        window_start=empty_scenario.window_start,
        window_end=empty_scenario.window_end,
        related_services=["api-gateway"],
        evidence_items=[],
    )
    direct_empty_report = build_and_validate_report(
        LLMInvestigationDraft(
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="Hallucinated root cause without evidence.",
                    confidence="high",
                    supporting_evidence_ids=["ev-fake-empty"],
                )
            ],
            abstained=False,
        ),
        direct_empty_bundle,
        model_name="adversarial-empty-llm",
    )

    # 2 fake docs + 3 fake evidence_ids + 1 unsupported hypothesis + 1 empty-evidence claim
    total_injected_faults = 2 + 3 + 1 + 1
    total_intercepted = (
        stripped_fake_docs
        + stripped_fake_ev
        + dropped_unsupported
        + (1 if (empty_wf.report.abstained and direct_empty_report.abstained) else 0)
    )

    return AdversarialProbeResult(
        injected_fake_doc_ids=2,
        stripped_fake_doc_ids=stripped_fake_docs,
        injected_fake_evidence_ids=3,
        stripped_fake_evidence_ids=stripped_fake_ev,
        injected_unsupported_hypotheses=1,
        dropped_unsupported_hypotheses=dropped_unsupported,
        post_validation_unsupported_rate=unsupported_root_cause_rate(
            report, bundle.allowed_evidence_ids
        ),
        post_validation_citation_correctness=citation_correctness(
            report, bundle.allowed_doc_ids
        ),
        empty_evidence_short_circuited=(
            empty_wf.report.abstained and empty_wf.executed_nodes[-1] == "abstain_no_evidence"
        ),
        empty_evidence_llm_calls=len(empty_llm.calls),
        empty_evidence_validator_forced_abstention=(
            direct_empty_report.abstained and len(direct_empty_report.probable_root_causes) == 0
        ),
        guardrail_interception_rate=round(total_intercepted / total_injected_faults, 6),
    )


def _check_ollama_available(base_url: str, model_name: str) -> tuple[bool, str]:
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=3.0) as client:
            res = client.get("/api/tags")
        if res.status_code != 200:
            return False, f"Ollama returned HTTP {res.status_code} at {base_url}"
        payload = res.json()
        models = [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]
        if models and not any(
            m == model_name or m.startswith(f"{model_name}:") for m in models
        ):
            return (
                False,
                f"Ollama is running at {base_url}, but model {model_name!r} is not pulled "
                f"(available: {models}). Run: ollama pull {model_name}",
            )
        return True, "ok"
    except httpx.HTTPError:
        return (
            False,
            f"Ollama is not reachable at {base_url}. Start Ollama (`ollama serve`) and "
            f"pull the configured model (`ollama pull {model_name}`), then retry `--mode ollama`.",
        )


def run_e2e_evaluation(
    *,
    mode: str = "deterministic",
    kb_dir: Path = DEFAULT_KB_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    collection_name: str = DEFAULT_COLLECTION,
) -> dict[str, Any]:
    """Run all 8 controlled synthetic scenarios + adversarial hallucination probe."""
    settings = get_settings()

    llm_client: LLMClient
    if mode == "ollama":
        ok, msg = _check_ollama_available(settings.OLLAMA_BASE_URL, settings.OLLAMA_MODEL)
        if not ok:
            raise RuntimeError(msg)
        llm_client = OllamaClient(
            base_url=settings.OLLAMA_BASE_URL,
            model_name=settings.OLLAMA_MODEL,
            timeout_seconds=settings.OLLAMA_TIMEOUT_SECONDS,
            num_ctx=settings.OLLAMA_NUM_CTX,
        )
        disclaimer = (
            "Measured using live local Ollama LLM synthesis over synthetic incident scenarios "
            "and SignalForge's 37-document synthetic knowledge base."
        )
    elif mode == "deterministic":
        llm_client = _make_deterministic_prompt_grounded_llm()
        disclaimer = (
            "Measured in deterministic pipeline mode (live NetworkX topology, live Qdrant + "
            "all-MiniLM-L6-v2 Hybrid RAG, real LangGraph workflow, and deterministic "
            "evidence-validator guardrails using a prompt-grounded LLM double). This measures "
            "end-to-end orchestration, retrieval, topology, and guardrail integrity — NOT live "
            "Ollama LLM reasoning quality (use --mode ollama to evaluate a live local LLM)."
        )
    else:
        raise ValueError(f"Unsupported evaluation mode: {mode!r}")

    docs = load_knowledge_base(kb_dir)
    chunks = MarkdownChunker(max_chars=1200, overlap_chars=150).chunk_documents(docs)

    embedder = SentenceTransformerEmbedder(
        model_name=settings.EMBEDDING_MODEL_NAME,
        device="cpu",
    )
    store = QdrantVectorStore(url=settings.QDRANT_URL, collection_name=collection_name)
    retriever = HybridRetriever(embedder=embedder, vector_store=store, bm25_index=BM25Index())

    scenarios = get_e2e_scenarios()
    scenario_results: list[ScenarioEvalResult] = []

    store.delete_collection()
    try:
        retriever.index_chunks(chunks)

        for scenario in scenarios:
            wf = run_investigation_workflow(
                question=scenario.question,
                target_service=scenario.target_service,
                window_start=scenario.window_start,
                window_end=scenario.window_end,
                collect_signals=_make_scenario_collect_signals(scenario),
                retrieve_knowledge=_make_retrieve_knowledge(retriever),
                llm_client=llm_client,
                lookback_minutes=60,
                max_depth=3,
                top_k_docs=5,
            )

            report = wf.report
            bundle = wf.evidence_bundle
            predicted_services = [bundle.target_service, *bundle.related_services]

            top1: float | None = None
            top3: float | None = None
            kb_recall: float | None = None
            if not scenario.expect_abstention:
                top1 = top_k_root_cause_hit(report, scenario.expected_root_cause_keywords, k=1)
                top3 = top_k_root_cause_hit(report, scenario.expected_root_cause_keywords, k=3)
                if scenario.expected_doc_ids:
                    retrieved_docs = bundle.allowed_doc_ids
                    expected_set = set(scenario.expected_doc_ids)
                    kb_recall = len(retrieved_docs & expected_set) / len(expected_set)

            scenario_results.append(
                ScenarioEvalResult(
                    scenario_id=scenario.scenario_id,
                    target_service=scenario.target_service,
                    expect_abstention=scenario.expect_abstention,
                    abstained=report.abstained,
                    executed_nodes=list(wf.executed_nodes),
                    top_1_hit=top1,
                    top_3_hit=top3,
                    affected_service_f1=round(
                        affected_service_f1(
                            predicted_services, scenario.expected_affected_services
                        ),
                        6,
                    ),
                    citation_correctness=round(
                        citation_correctness(report, bundle.allowed_doc_ids), 6
                    ),
                    kb_expected_doc_recall=(
                        round(kb_recall, 6) if kb_recall is not None else None
                    ),
                    unsupported_root_cause_rate=round(
                        unsupported_root_cause_rate(report, bundle.allowed_evidence_ids), 6
                    ),
                    evidence_coverage=round(evidence_coverage(report, bundle), 6),
                    abstention_correctness=round(
                        abstention_correctness(report, scenario.expect_abstention), 6
                    ),
                    validation_warning_count=len(report.validation_warnings),
                    duration_ms=wf.duration_ms,
                )
            )

        adversarial_probe = run_adversarial_hallucination_probe(scenarios, retriever)
    finally:
        store.delete_collection()

    summary = aggregate_e2e_results(
        scenario_results,
        evaluation_mode=mode,
        model_name=llm_client.model_name,
        disclaimer=disclaimer,
        adversarial_probe=adversarial_probe,
    )
    payload = summary.model_dump()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run end-to-end root-cause & RAG guardrail evaluation across 8 scenarios."
    )
    parser.add_argument(
        "--mode",
        choices=("deterministic", "ollama"),
        default="deterministic",
        help="Evaluation mode: deterministic pipeline/guardrail mode or live Ollama LLM.",
    )
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--collection", type=str, default=DEFAULT_COLLECTION)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        payload = run_e2e_evaluation(
            mode=args.mode,
            kb_dir=args.kb_dir,
            output_path=args.output_path,
            collection_name=args.collection,
        )
    except Exception as exc:
        print(f"E2E evaluation stopped: {exc}", file=sys.stderr)
        return 1

    print(
        f"Mode: {payload['evaluation_mode']} ({payload['model_name']})\n"
        f"Top-1 Hit (7 faults):       {payload['top_1_root_cause_accuracy']:.4f}\n"
        f"Top-3 Hit (7 faults):       {payload['top_3_root_cause_accuracy']:.4f}\n"
        f"Affected Service F1:        {payload['mean_affected_service_f1']:.4f}\n"
        f"KB Expected Doc Recall:     {payload['mean_kb_expected_doc_recall']:.4f}\n"
        f"Citation Correctness:       {payload['mean_citation_correctness']:.4f}\n"
        f"Unsupported Root-Cause:     {payload['mean_unsupported_root_cause_rate']:.4f}\n"
        f"Evidence Coverage:          {payload['mean_evidence_coverage']:.4f}\n"
        f"Abstention Accuracy (8/8):  {payload['abstention_accuracy']:.4f}\n"
        f"Workflow Latency p50 / p95: {payload['p50_duration_ms']:.2f} ms / "
        f"{payload['p95_duration_ms']:.2f} ms\n"
        f"Adversarial Guardrail Rate: "
        f"{payload['adversarial_probe']['guardrail_interception_rate']:.4f}\n"
        f"Saved results to: {args.output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
