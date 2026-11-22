import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from apps.api.src.core.config import Settings
from services.investigation import (
    DEFAULT_ABSTENTION_REASON,
    SYSTEM_PROMPT,
    CitationRef,
    EvidenceBundle,
    EvidenceItem,
    FakeLLMClient,
    LLMClient,
    LLMClientError,
    LLMInvestigationDraft,
    OllamaClient,
    RootCauseHypothesis,
    build_and_validate_report,
    build_investigation_user_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
INVESTIGATION_PKG_DIR = REPO_ROOT / "services" / "investigation"
FORBIDDEN_IMPORTS = {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette", "pickle", "joblib"}


def _make_sample_bundle(*, actionable: bool = True) -> EvidenceBundle:
    t0 = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=30)

    items = [
        EvidenceItem(
            evidence_id="dep-edge-1",
            signal_type="dependency",
            service="payment-service",
            timestamp_utc=None,
            summary="checkout-service calls payment-service (hop=1, error_rate=0.50)",
        ),
    ]
    if actionable:
        items.extend(
            [
                EvidenceItem(
                    evidence_id="dep-1",
                    signal_type="deployment",
                    service="payment-service",
                    timestamp_utc=t0 - timedelta(minutes=20),
                    summary="Deployment v1.9.0 (succeeded) on payment-service",
                ),
                EvidenceItem(
                    evidence_id="anom-1",
                    signal_type="anomaly",
                    service="checkout-service",
                    timestamp_utc=t0 + timedelta(minutes=5),
                    summary="LSTM anomaly score 9.42 > threshold 6.75 on error_rate",
                ),
                EvidenceItem(
                    evidence_id="evt-1",
                    signal_type="event",
                    service="payment-service",
                    timestamp_utc=t0 + timedelta(minutes=6),
                    summary="[ERROR] Upstream card processor 504 timeout",
                ),
            ]
        )

    citations = [
        CitationRef(
            doc_id="rb-003",
            doc_type="runbook",
            title="Payment Service Outage and Provider Failover",
            section_heading="Failover Procedure",
            source_path="runbooks/rb-003-payment-service-outage.md",
            is_synthetic=True,
        ),
        CitationRef(
            doc_id="inc-2026-003",
            doc_type="incident",
            title="Incident Postmortem — Primary Card Processor 504 Outage",
            section_heading="Root Cause",
            source_path="incidents/inc-2026-003-payment-provider-outage.md",
            is_synthetic=True,
        ),
    ]
    snippets = {
        "rb-003": "Switch traffic from stripe-eu to adyen-eu when 504 error_rate exceeds 5%.",
        "inc-2026-003": "Primary card processor returned HTTP 504 timeouts for 18 minutes.",
    }
    return EvidenceBundle(
        question="Why did checkout-service 5xx errors spike at 14:05 UTC?",
        target_service="checkout-service",
        window_start=t0,
        window_end=t1,
        related_services=["api-gateway", "payment-service"],
        evidence_items=items,
        retrieved_citations=citations,
        retrieved_snippets=snippets,
    )


# ---------------------------------------------------------------------------
# 1. OllamaClient tests (httpx.MockTransport)
# ---------------------------------------------------------------------------


def test_ollama_client_structured_success_and_request_contract() -> None:
    captured_body: dict[str, object] = {}
    sample_draft = LLMInvestigationDraft(
        confirmed_facts=["payment-service logged upstream 504 timeouts at 14:06 UTC."],
        detected_anomalies=["checkout-service LSTM anomaly score 9.42 > 6.75."],
        observed_correlations=["payment-service v1.9.0 deployed 20m prior to spike."],
        probable_root_causes=[
            RootCauseHypothesis(
                hypothesis="Downstream payment provider 504 timeouts cascaded to checkout-service.",
                confidence="high",
                supporting_evidence_ids=["anom-1", "evt-1"],
            )
        ],
        cited_doc_ids=["rb-003"],
        recommended_next_steps=["Initiate payment provider failover per rb-003."],
        uncertainty_and_gaps=["External processor status feed not ingested."],
        abstained=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/chat"
        captured_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": sample_draft.model_dump_json()}},
        )

    client = OllamaClient(
        base_url="http://localhost:11434",
        model_name="qwen2.5:7b",
        timeout_seconds=30.0,
        num_ctx=8192,
        transport=httpx.MockTransport(handler),
    )
    assert isinstance(client, LLMClient)

    result = client.generate_structured(
        SYSTEM_PROMPT,
        "Investigate checkout-service",
        LLMInvestigationDraft,
    )
    assert result == sample_draft
    assert captured_body["model"] == "qwen2.5:7b"
    assert captured_body["stream"] is False
    assert captured_body["options"] == {"num_ctx": 8192, "temperature": 0.1}
    assert captured_body["format"] == LLMInvestigationDraft.model_json_schema()


def test_ollama_client_error_mapping() -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    timeout_client = OllamaClient(transport=httpx.MockTransport(raise_timeout))
    with pytest.raises(LLMClientError, match="timed out"):
        timeout_client.generate_structured("sys", "usr", LLMInvestigationDraft)

    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    connect_client = OllamaClient(transport=httpx.MockTransport(raise_connect))
    with pytest.raises(LLMClientError, match="Unable to connect"):
        connect_client.generate_structured("sys", "usr", LLMInvestigationDraft)

    http_500_client = OllamaClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="internal error"))
    )
    with pytest.raises(LLMClientError, match="HTTP 500"):
        http_500_client.generate_structured("sys", "usr", LLMInvestigationDraft)

    bad_json_client = OllamaClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"message": {"content": "{not valid json"}})
        )
    )
    with pytest.raises(LLMClientError, match="failed validation"):
        bad_json_client.generate_structured("sys", "usr", LLMInvestigationDraft)

    missing_content_client = OllamaClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"message": {}}))
    )
    with pytest.raises(LLMClientError, match="missing structured message content"):
        missing_content_client.generate_structured("sys", "usr", LLMInvestigationDraft)


# ---------------------------------------------------------------------------
# 2. FakeLLMClient tests
# ---------------------------------------------------------------------------


def test_fake_llm_client_returns_draft_and_raises_errors() -> None:
    draft = LLMInvestigationDraft(
        confirmed_facts=["Observed error event."],
        abstained=True,
        abstention_reason="Single isolated event.",
    )
    fake = FakeLLMClient(response=draft, model_name="fake-qwen")
    assert isinstance(fake, LLMClient)

    out = fake.generate_structured("sys", "usr", LLMInvestigationDraft)
    assert out == draft
    assert len(fake.calls) == 1
    assert fake.calls[0]["response_model"] == "LLMInvestigationDraft"

    callable_fake = FakeLLMClient(
        response=lambda s, u, cls: cls(confirmed_facts=[f"seen:{len(u)}"], abstained=False)
    )
    out2 = callable_fake.generate_structured("sys", "hello", LLMInvestigationDraft)
    assert out2.confirmed_facts == ["seen:5"]

    failing_fake = FakeLLMClient(error=LLMClientError("backend offline"))
    with pytest.raises(LLMClientError, match="backend offline"):
        failing_fake.generate_structured("sys", "usr", LLMInvestigationDraft)


# ---------------------------------------------------------------------------
# 3. Evidence validator & deterministic hydration tests
# ---------------------------------------------------------------------------


def test_validator_preserves_valid_citations_and_hydrates_exact_metadata() -> None:
    bundle = _make_sample_bundle(actionable=True)
    draft = LLMInvestigationDraft(
        confirmed_facts=["payment-service logged 504 timeout."],
        detected_anomalies=["checkout-service anomaly score 9.42."],
        observed_correlations=["payment-service deployed v1.9.0 20 minutes earlier."],
        probable_root_causes=[
            RootCauseHypothesis(
                hypothesis="Upstream card processor 504 timeouts propagated to checkout-service.",
                confidence="medium",
                supporting_evidence_ids=["anom-1", "evt-1", "evt-1"],
            )
        ],
        cited_doc_ids=["rb-003", "inc-2026-003", "rb-003"],
        recommended_next_steps=["Fail over card processor per rb-003."],
        uncertainty_and_gaps=["Missing external processor telemetry."],
        abstained=False,
    )

    report = build_and_validate_report(draft, bundle, model_name="qwen2.5:7b")

    assert report.abstained is False
    assert report.abstention_reason is None
    assert report.validation_warnings == []
    assert [c.doc_id for c in report.citations] == ["rb-003", "inc-2026-003"]
    assert report.citations[0].title == "Payment Service Outage and Provider Failover"
    assert report.citations[0].source_path == "runbooks/rb-003-payment-service-outage.md"
    assert report.citations[0].is_synthetic is True
    assert len(report.probable_root_causes) == 1
    assert report.probable_root_causes[0].supporting_evidence_ids == ["anom-1", "evt-1"]
    assert report.probable_root_causes[0].is_proven_causation is False


def test_validator_strips_fabricated_ids_and_drops_unsupported_hypotheses() -> None:
    bundle = _make_sample_bundle(actionable=True)
    draft = LLMInvestigationDraft(
        confirmed_facts=["Observed checkout anomaly."],
        probable_root_causes=[
            RootCauseHypothesis(
                hypothesis="Partially supported hypothesis with one fabricated ID.",
                confidence="medium",
                supporting_evidence_ids=["anom-1", "evt-hallucinated-999"],
            ),
            RootCauseHypothesis(
                hypothesis="Completely fabricated hypothesis with no real evidence.",
                confidence="high",
                supporting_evidence_ids=["fake-id-1", "fake-id-2"],
            ),
        ],
        cited_doc_ids=["rb-003", "rb-999-hallucinated"],
        abstained=False,
    )

    report = build_and_validate_report(draft, bundle, model_name="qwen2.5:7b")

    assert [c.doc_id for c in report.citations] == ["rb-003"]
    assert len(report.probable_root_causes) == 1
    assert report.probable_root_causes[0].supporting_evidence_ids == ["anom-1"]
    assert any("rb-999-hallucinated" in w for w in report.validation_warnings)
    assert any("evt-hallucinated-999" in w for w in report.validation_warnings)
    assert any("Dropped unsupported hypothesis" in w for w in report.validation_warnings)


def test_validator_forces_abstention_when_no_actionable_evidence_exists() -> None:
    bundle = _make_sample_bundle(actionable=False)
    assert bundle.has_actionable_evidence is False

    # LLM tries to emit a root cause despite having only topology edge (no actionable signals)
    draft = LLMInvestigationDraft(
        probable_root_causes=[
            RootCauseHypothesis(
                hypothesis="Payment service failed.",
                confidence="high",
                supporting_evidence_ids=["dep-edge-1"],
            )
        ],
        cited_doc_ids=["rb-003"],
        abstained=False,
    )

    report = build_and_validate_report(draft, bundle, model_name="qwen2.5:7b")

    assert report.abstained is True
    assert report.probable_root_causes == []
    assert report.abstention_reason == DEFAULT_ABSTENTION_REASON
    assert any("Forced abstention" in w for w in report.validation_warnings)


def test_root_cause_hypothesis_forbids_proven_causation_true() -> None:
    with pytest.raises(ValidationError):
        RootCauseHypothesis(
            hypothesis="Claiming proven causation must fail schema validation.",
            confidence="high",
            supporting_evidence_ids=["evt-1"],
            is_proven_causation=True,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 4. Prompt builder & Ollama settings tests
# ---------------------------------------------------------------------------


def test_build_investigation_user_prompt_contains_ids_bounds_and_truncates() -> None:
    bundle = _make_sample_bundle(actionable=True)
    prompt = build_investigation_user_prompt(bundle)

    assert bundle.question in prompt
    assert "checkout-service" in prompt
    assert bundle.window_start.isoformat() in prompt
    assert bundle.window_end.isoformat() in prompt
    assert "Actionable Evidence Available: yes" in prompt
    for ev_id in ("dep-edge-1", "dep-1", "anom-1", "evt-1"):
        assert f"[{ev_id}]" in prompt
    for doc_id in ("rb-003", "inc-2026-003"):
        assert f"[{doc_id}]" in prompt
    assert "adyen-eu" in prompt

    compact = build_investigation_user_prompt(bundle, max_total_chars=300)
    assert len(compact) <= 300
    assert "[prompt truncated]" in compact


def test_ollama_settings_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.OLLAMA_BASE_URL == "http://localhost:11434"
    assert settings.OLLAMA_MODEL == "qwen2.5:7b"
    assert settings.OLLAMA_TIMEOUT_SECONDS == 60.0
    assert settings.OLLAMA_NUM_CTX == 8192


# ---------------------------------------------------------------------------
# 5. AST import-boundary enforcement for services/investigation/
# ---------------------------------------------------------------------------


def test_investigation_package_has_no_framework_or_db_imports() -> None:
    py_files = sorted(INVESTIGATION_PKG_DIR.rglob("*.py"))
    assert len(py_files) >= 5

    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        overlap = imported_roots & FORBIDDEN_IMPORTS
        assert not overlap, f"{path.relative_to(REPO_ROOT)} imported forbidden: {overlap}"
