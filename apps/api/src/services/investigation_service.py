import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.src.core.config import get_settings
from apps.api.src.models import Investigation, Service
from apps.api.src.services.anomaly_service import ServiceNotFoundError
from apps.api.src.services.graph_service import (
    build_correlation_timeline,
    get_dependency_graph,
)
from apps.api.src.services.knowledge_service import (
    QdrantUnavailableError,
    get_knowledge_retriever,
    search_knowledge,
)
from services.ingestion.normalizer import normalize_service_name
from services.investigation import (
    CitationRef,
    EvidenceItem,
    LLMClient,
    OllamaClient,
    run_investigation_workflow,
)
from services.retrieval.search.hybrid import HybridRetriever

logger = logging.getLogger(__name__)


class InvestigationNotFoundError(LookupError):
    """Raised when a requested investigation UUID does not exist."""


def get_llm_client() -> LLMClient:
    """FastAPI dependency returning an OllamaClient configured from Settings."""
    settings = get_settings()
    return OllamaClient(
        base_url=settings.OLLAMA_BASE_URL,
        model_name=settings.OLLAMA_MODEL,
        timeout_seconds=settings.OLLAMA_TIMEOUT_SECONDS,
        num_ctx=settings.OLLAMA_NUM_CTX,
    )


def _make_collect_signals_adapter(db: Session):
    def _collect_signals(
        service: str,
        window_start: datetime,
        window_end: datetime,
        lookback_minutes: int,
        max_depth: int,
    ) -> tuple[list[str], list[EvidenceItem]]:
        summary = build_correlation_timeline(
            db,
            target_service=service,
            window_start=window_start,
            window_end=window_end,
            lookback_minutes=lookback_minutes,
            max_depth=max_depth,
        )
        dep_graph = get_dependency_graph(db)
        scope_set = set(summary.correlated_services)

        evidence_items: list[EvidenceItem] = []
        counter = 1

        for item in summary.timeline:
            extra = ""
            if item.minutes_relative_to_window_start is not None:
                extra = f" (t={item.minutes_relative_to_window_start:+.1f}m vs window_start)"
            evidence_items.append(
                EvidenceItem(
                    evidence_id=f"ev-{counter:03d}",
                    signal_type=item.signal_type,
                    service=item.service,
                    timestamp_utc=item.timestamp_utc,
                    summary=(
                        f"{item.summary}{extra} "
                        f"[relation={item.relation}, hop={item.hop_distance}]"
                    ),
                )
            )
            counter += 1

        for edge in dep_graph.edges:
            if edge.caller in scope_set and edge.callee in scope_set:
                evidence_items.append(
                    EvidenceItem(
                        evidence_id=f"ev-{counter:03d}",
                        signal_type="dependency",
                        service=edge.callee,
                        timestamp_utc=None,
                        summary=(
                            f"Dependency edge {edge.caller} -> {edge.callee} "
                            f"(calls={edge.call_count}, errors={edge.error_count}, "
                            f"error_rate={edge.error_rate:.2f}, "
                            f"avg_duration_ms={edge.avg_duration_ms:.1f})"
                        ),
                    )
                )
                counter += 1

        return list(summary.impact.all_related_services), evidence_items

    return _collect_signals


def _make_retrieve_knowledge_adapter(retriever: HybridRetriever):
    def _retrieve_knowledge(
        query: str,
        service: str,
        top_k: int,
    ) -> tuple[list[CitationRef], dict[str, str], list[str]]:
        try:
            hits = search_knowledge(
                query=query,
                top_k=top_k,
                mode="hybrid",
                service=None,
                retriever=retriever,
            )
        except QdrantUnavailableError:
            logger.warning(
                "Knowledge base unavailable during investigation retrieval (service=%s)",
                service,
            )
            return [], {}, ["Knowledge base unavailable during retrieval"]

        citations: list[CitationRef] = []
        snippets: dict[str, str] = {}
        seen_doc_ids: set[str] = set()
        for chunk in hits:
            if chunk.doc_id in seen_doc_ids:
                continue
            seen_doc_ids.add(chunk.doc_id)
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

        return citations, snippets, []

    return _retrieve_knowledge


def run_and_persist_investigation(
    db: Session,
    *,
    question: str,
    service: str,
    start_time: datetime,
    end_time: datetime,
    lookback_minutes: int = 60,
    max_depth: int = 3,
    top_k_docs: int = 5,
    llm_client: LLMClient | None = None,
    retriever: HybridRetriever | None = None,
) -> tuple[Investigation, str]:
    """Run the bounded LangGraph investigation workflow and persist the report in PostgreSQL."""
    normalized_service = normalize_service_name(service)
    service_row = db.scalar(select(Service).where(Service.name == normalized_service))
    if service_row is None:
        raise ServiceNotFoundError(f"Service {normalized_service!r} not found")

    active_llm = llm_client if llm_client is not None else get_llm_client()
    active_retriever = retriever if retriever is not None else get_knowledge_retriever()

    workflow_result = run_investigation_workflow(
        question=question,
        target_service=normalized_service,
        window_start=start_time,
        window_end=end_time,
        collect_signals=_make_collect_signals_adapter(db),
        retrieve_knowledge=_make_retrieve_knowledge_adapter(active_retriever),
        llm_client=active_llm,
        lookback_minutes=lookback_minutes,
        max_depth=max_depth,
        top_k_docs=top_k_docs,
    )

    report = workflow_result.report
    row = Investigation(
        service_id=service_row.id,
        question=report.question,
        window_start=report.window_start,
        window_end=report.window_end,
        model_name=report.model_name,
        abstained=report.abstained,
        report_json=report.model_dump(mode="json"),
        executed_nodes=list(workflow_result.executed_nodes),
        duration_ms=float(workflow_result.duration_ms),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, service_row.name


def list_investigations(
    db: Session,
    *,
    service: str | None = None,
    abstained: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[tuple[Investigation, str]]]:
    """List persisted investigations ordered by created_at DESC, id DESC."""
    filters = []
    if service is not None:
        filters.append(Service.name == normalize_service_name(service))
    if abstained is not None:
        filters.append(Investigation.abstained.is_(abstained))

    count_stmt = (
        select(func.count(Investigation.id))
        .select_from(Investigation)
        .join(Service, Investigation.service_id == Service.id)
        .where(*filters)
    )
    total = int(db.scalar(count_stmt) or 0)

    items_stmt = (
        select(Investigation, Service.name.label("service_name"))
        .join(Service, Investigation.service_id == Service.id)
        .where(*filters)
        .order_by(Investigation.created_at.desc(), Investigation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [(row, str(svc_name)) for row, svc_name in db.execute(items_stmt).all()]
    return total, rows


def get_investigation_by_id(
    db: Session,
    investigation_id: UUID,
) -> tuple[Investigation, str]:
    """Retrieve a single persisted investigation by UUID or raise InvestigationNotFoundError."""
    stmt = (
        select(Investigation, Service.name.label("service_name"))
        .join(Service, Investigation.service_id == Service.id)
        .where(Investigation.id == investigation_id)
    )
    result = db.execute(stmt).first()
    if result is None:
        raise InvestigationNotFoundError(f"Investigation {investigation_id} not found")
    row, service_name = result
    return row, str(service_name)
