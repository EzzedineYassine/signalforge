from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc

EvidenceSignalType = Literal[
    "event",
    "metric",
    "anomaly",
    "deployment",
    "incident",
    "dependency",
]
ACTIONABLE_SIGNAL_TYPES: frozenset[str] = frozenset(
    {"event", "anomaly", "deployment", "incident"}
)
ConfidenceLevel = Literal["low", "medium", "high"]


class EvidenceItem(BaseModel):
    """Single grounded operational observation supplied to the investigation LLM."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    signal_type: EvidenceSignalType
    service: str
    timestamp_utc: datetime | None = None
    summary: str

    @field_validator("evidence_id", "summary")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field cannot be empty")
        return cleaned

    @field_validator("service", mode="before")
    @classmethod
    def _normalize_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("timestamp_utc", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        return normalize_timestamp_utc(value)


class CitationRef(BaseModel):
    """Verified knowledge-base citation hydrated deterministically from retrieval results."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    doc_type: str
    title: str
    section_heading: str
    source_path: str
    is_synthetic: bool


class EvidenceBundle(BaseModel):
    """Complete operational telemetry and knowledge context for a single investigation."""

    model_config = ConfigDict(frozen=True)

    question: str
    target_service: str
    window_start: datetime
    window_end: datetime
    related_services: list[str] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    retrieved_citations: list[CitationRef] = Field(default_factory=list)
    retrieved_snippets: dict[str, str] = Field(default_factory=dict)

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question cannot be empty")
        return cleaned

    @field_validator("target_service", mode="before")
    @classmethod
    def _normalize_target_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("related_services", mode="before")
    @classmethod
    def _normalize_related_services(cls, value: Any) -> list[str]:
        if value is None:
            return []
        return [normalize_service_name(item) for item in value]

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _normalize_window_bounds(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @model_validator(mode="after")
    def _validate_window_order(self) -> "EvidenceBundle":
        if self.window_start > self.window_end:
            raise ValueError("window_start must be less than or equal to window_end")
        return self

    @property
    def has_actionable_evidence(self) -> bool:
        return any(item.signal_type in ACTIONABLE_SIGNAL_TYPES for item in self.evidence_items)

    @property
    def allowed_doc_ids(self) -> set[str]:
        return {ref.doc_id for ref in self.retrieved_citations}

    @property
    def allowed_evidence_ids(self) -> set[str]:
        return {item.evidence_id for item in self.evidence_items}


class RootCauseHypothesis(BaseModel):
    """Evidence-backed hypothesis explicitly flagged as non-proven causation."""

    model_config = ConfigDict(frozen=True)

    hypothesis: str
    confidence: ConfidenceLevel
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    is_proven_causation: Literal[False] = False

    @field_validator("hypothesis")
    @classmethod
    def _validate_hypothesis(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("hypothesis cannot be empty")
        return cleaned


class LLMInvestigationDraft(BaseModel):
    """Structured JSON output contract requested directly from the LLM."""

    confirmed_facts: list[str] = Field(default_factory=list)
    detected_anomalies: list[str] = Field(default_factory=list)
    observed_correlations: list[str] = Field(default_factory=list)
    probable_root_causes: list[RootCauseHypothesis] = Field(default_factory=list)
    cited_doc_ids: list[str] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)
    uncertainty_and_gaps: list[str] = Field(default_factory=list)
    abstained: bool = False
    abstention_reason: str | None = None


class InvestigationReport(BaseModel):
    """Validated investigation output with deterministically hydrated citations."""

    question: str
    target_service: str
    window_start: datetime
    window_end: datetime
    model_name: str
    confirmed_facts: list[str] = Field(default_factory=list)
    detected_anomalies: list[str] = Field(default_factory=list)
    observed_correlations: list[str] = Field(default_factory=list)
    probable_root_causes: list[RootCauseHypothesis] = Field(default_factory=list)
    citations: list[CitationRef] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)
    uncertainty_and_gaps: list[str] = Field(default_factory=list)
    abstained: bool = False
    abstention_reason: str | None = None
    validation_warnings: list[str] = Field(default_factory=list)
