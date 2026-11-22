from services.investigation.schemas import (
    CitationRef,
    EvidenceBundle,
    InvestigationReport,
    LLMInvestigationDraft,
    RootCauseHypothesis,
)

DEFAULT_ABSTENTION_REASON = (
    "Insufficient actionable operational evidence (events, anomalies, deployments, or incidents) "
    "in the requested time window to propose a grounded root cause."
)


def _clean_lines(values: list[str]) -> list[str]:
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def build_and_validate_report(
    draft: LLMInvestigationDraft,
    bundle: EvidenceBundle,
    model_name: str,
) -> InvestigationReport:
    """Deterministically validate an LLM draft against the grounded EvidenceBundle.

    - Hydrates `CitationRef`s strictly from `bundle.retrieved_citations` by verified `doc_id`.
    - Strips fabricated `doc_id`s and `evidence_id`s, recording explicit warnings.
    - Drops any root-cause hypothesis left with zero valid `supporting_evidence_ids`.
    - Forces `abstained=True` and clears `probable_root_causes` whenever
      `bundle.has_actionable_evidence` is False.
    """
    warnings: list[str] = []

    # 1. Hydrate citations strictly from retrieved bundle metadata (never trusting LLM text).
    citations_by_doc_id: dict[str, CitationRef] = {}
    for ref in bundle.retrieved_citations:
        citations_by_doc_id.setdefault(ref.doc_id, ref)

    validated_citations: list[CitationRef] = []
    seen_doc_ids: set[str] = set()
    for raw_doc_id in draft.cited_doc_ids:
        doc_id = raw_doc_id.strip()
        if not doc_id or doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)
        if doc_id in citations_by_doc_id:
            validated_citations.append(citations_by_doc_id[doc_id])
        else:
            warnings.append(f"Stripped fabricated citation doc_id: {doc_id!r}")

    # 2. Validate hypothesis evidence IDs and drop unsupported hypotheses.
    allowed_evidence = bundle.allowed_evidence_ids
    validated_hypotheses: list[RootCauseHypothesis] = []
    for hyp in draft.probable_root_causes:
        valid_ids: list[str] = []
        invalid_ids: list[str] = []
        seen_ev: set[str] = set()
        for raw_ev_id in hyp.supporting_evidence_ids:
            ev_id = raw_ev_id.strip()
            if not ev_id or ev_id in seen_ev:
                continue
            seen_ev.add(ev_id)
            if ev_id in allowed_evidence:
                valid_ids.append(ev_id)
            else:
                invalid_ids.append(ev_id)

        if invalid_ids:
            warnings.append(
                f"Stripped fabricated evidence_id(s) {invalid_ids} from hypothesis "
                f"{hyp.hypothesis!r}"
            )

        if not valid_ids:
            warnings.append(
                f"Dropped unsupported hypothesis with zero valid supporting evidence_ids: "
                f"{hyp.hypothesis!r}"
            )
            continue

        validated_hypotheses.append(
            RootCauseHypothesis(
                hypothesis=hyp.hypothesis,
                confidence=hyp.confidence,
                supporting_evidence_ids=valid_ids,
                is_proven_causation=False,
            )
        )

    # 3. Enforce mandatory abstention when actionable evidence is absent.
    abstained = bool(draft.abstained)
    abstention_reason = (
        draft.abstention_reason.strip()
        if isinstance(draft.abstention_reason, str) and draft.abstention_reason.strip()
        else None
    )

    if not bundle.has_actionable_evidence:
        if not abstained or validated_hypotheses:
            warnings.append(
                "Forced abstention and cleared root-cause hypotheses because the evidence "
                "bundle contains no actionable operational signals."
            )
        abstained = True
        validated_hypotheses = []
        if abstention_reason is None:
            abstention_reason = DEFAULT_ABSTENTION_REASON
    elif abstained:
        if validated_hypotheses:
            warnings.append(
                "Cleared probable_root_causes because draft marked abstained=True."
            )
            validated_hypotheses = []
        if abstention_reason is None:
            abstention_reason = (
                "Investigation abstained due to insufficient or ambiguous evidence."
            )

    return InvestigationReport(
        question=bundle.question,
        target_service=bundle.target_service,
        window_start=bundle.window_start,
        window_end=bundle.window_end,
        model_name=model_name,
        confirmed_facts=_clean_lines(draft.confirmed_facts),
        detected_anomalies=_clean_lines(draft.detected_anomalies),
        observed_correlations=_clean_lines(draft.observed_correlations),
        probable_root_causes=validated_hypotheses,
        citations=validated_citations,
        recommended_next_steps=_clean_lines(draft.recommended_next_steps),
        uncertainty_and_gaps=_clean_lines(draft.uncertainty_and_gaps),
        abstained=abstained,
        abstention_reason=abstention_reason,
        validation_warnings=warnings,
    )


validate_investigation_report = build_and_validate_report
