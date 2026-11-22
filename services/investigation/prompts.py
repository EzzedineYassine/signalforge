from services.investigation.schemas import EvidenceBundle

SYSTEM_PROMPT = """You are SignalForge's local AIOps incident investigation analyst.
Produce a strict JSON response conforming to the provided schema using ONLY the evidence below.

Non-negotiable grounding rules:
1. Cite ONLY exact `evidence_id` values listed under EVIDENCE ITEMS in `supporting_evidence_ids`.
2. Cite ONLY exact `doc_id` values listed under KNOWLEDGE CITATIONS in `cited_doc_ids`.
   Never invent document IDs, evidence IDs, metrics, error codes, or timestamps.
3. Explicitly separate:
   - `confirmed_facts`: directly observed records from EVIDENCE ITEMS.
   - `detected_anomalies`: anomaly detections with detector scores exceeding thresholds.
   - `observed_correlations`: temporal or topological co-occurrences across related services.
   - `probable_root_causes`: candidate explanations backed by >= 1 valid `supporting_evidence_ids`.
     Always set `is_proven_causation` to false. Never claim correlation proves causation.
4. Abstention rule:
   If ACTIONABLE EVIDENCE AVAILABLE is `no`, or if the evidence is insufficient to support a
   grounded hypothesis, you MUST set `abstained` to true, `probable_root_causes` to [], provide
   a clear `abstention_reason`, and list missing telemetry in `uncertainty_and_gaps`.
5. Keep `recommended_next_steps` concise, safe, and grounded in cited runbooks or missing checks.
"""


def _truncate(text: str, max_chars: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 3)].rstrip() + "..."


def build_investigation_user_prompt(
    bundle: EvidenceBundle,
    *,
    max_evidence_items: int = 40,
    max_snippet_chars: int = 600,
    max_total_chars: int = 12000,
) -> str:
    """Serialize an EvidenceBundle into a compact, bounded prompt safe for 8K-context models."""
    lines: list[str] = [
        "=== INVESTIGATION REQUEST ===",
        f"Question: {bundle.question}",
        f"Target Service: {bundle.target_service}",
        f"Time Window (UTC): {bundle.window_start.isoformat()} to {bundle.window_end.isoformat()}",
        (
            "Related Services: "
            + (", ".join(bundle.related_services) if bundle.related_services else "none")
        ),
        f"Actionable Evidence Available: {'yes' if bundle.has_actionable_evidence else 'no'}",
        "",
        "=== EVIDENCE ITEMS ===",
    ]

    items = bundle.evidence_items[:max_evidence_items]
    if not items:
        lines.append("(none)")
    else:
        for item in items:
            ts = item.timestamp_utc.isoformat() if item.timestamp_utc is not None else "n/a"
            lines.append(
                f"- [{item.evidence_id}] ({item.signal_type} | {item.service} | {ts}) "
                f"{_truncate(item.summary, 300)}"
            )
        if len(bundle.evidence_items) > max_evidence_items:
            omitted = len(bundle.evidence_items) - max_evidence_items
            lines.append(f"... ({omitted} additional evidence items truncated for context budget)")

    lines.append("")
    lines.append("=== KNOWLEDGE CITATIONS ===")
    if not bundle.retrieved_citations:
        lines.append("(none)")
    else:
        seen_docs: set[str] = set()
        for ref in bundle.retrieved_citations:
            if ref.doc_id in seen_docs:
                continue
            seen_docs.add(ref.doc_id)
            synth_tag = "synthetic" if ref.is_synthetic else "prod"
            lines.append(
                f"- [{ref.doc_id}] ({ref.doc_type} | {synth_tag} | {ref.section_heading} | "
                f"{ref.source_path}) {ref.title}"
            )
            snippet = bundle.retrieved_snippets.get(ref.doc_id)
            if snippet:
                lines.append(f"  Excerpt: {_truncate(snippet, max_snippet_chars)}")

    rendered = "\n".join(lines)
    if len(rendered) > max_total_chars:
        rendered = rendered[: max(0, max_total_chars - 40)].rstrip() + "\n... [prompt truncated]"
    return rendered
