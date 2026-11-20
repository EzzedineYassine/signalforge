import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.config import get_settings
from apps.api.src.core.logging import JsonFormatter
from services.ingestion.parsers.otel import (
    load_jsonl_file,
    parse_otlp_logs,
    parse_otlp_metrics,
    parse_otlp_traces,
)
from services.ingestion.pipeline import IngestionBatchResult, ingest_event_batch
from services.ingestion.telemetry_pipeline import ingest_metric_batch, ingest_trace_batch

SIGNAL_SPECS: tuple[
    tuple[
        str,
        str,
        Callable[[dict[str, Any]], list[dict[str, Any]]],
        Callable[[Session, Sequence[Any]], IngestionBatchResult],
    ],
    ...,
] = (
    ("traces", "traces.jsonl", parse_otlp_traces, ingest_trace_batch),
    ("metrics", "metrics.jsonl", parse_otlp_metrics, ingest_metric_batch),
    ("logs", "logs.jsonl", parse_otlp_logs, ingest_event_batch),
)


def _empty_counts() -> dict[str, int]:
    return {"received": 0, "accepted": 0, "duplicates": 0, "rejected": 0}


def _iter_chunks(items: Sequence[Any], batch_size: int) -> list[Sequence[Any]]:
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def _ingest_signal_file(
    db: Session,
    path: Path,
    parser: Callable[[dict[str, Any]], list[dict[str, Any]]],
    ingester: Callable[[Session, Sequence[Any]], IngestionBatchResult],
    *,
    batch_size: int,
) -> dict[str, int]:
    envelopes = load_jsonl_file(path)
    records: list[dict[str, Any]] = []
    for envelope in envelopes:
        records.extend(parser(envelope))

    totals = _empty_counts()
    for chunk in _iter_chunks(records, batch_size):
        result = ingester(db, chunk)
        totals["received"] += result.received
        totals["accepted"] += result.accepted
        totals["duplicates"] += result.duplicates
        totals["rejected"] += result.rejected
    return totals


def run_capture_ingestion(
    input_dir: Path,
    *,
    batch_size: int = 500,
    database_url: str | None = None,
    db: Session | None = None,
) -> dict[str, dict[str, int]]:
    """Load OTLP JSONL capture files from input_dir and ingest them idempotently into PostgreSQL."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")

    present_specs = [
        (signal_name, input_dir / filename, parser, ingester)
        for signal_name, filename, parser, ingester in SIGNAL_SPECS
        if (input_dir / filename).is_file()
    ]
    if not present_specs:
        expected = ", ".join(filename for _, filename, _, _ in SIGNAL_SPECS)
        raise FileNotFoundError(f"No OTLP JSONL files ({expected}) found in {input_dir}")

    summary: dict[str, dict[str, int]] = {name: _empty_counts() for name, _, _, _ in SIGNAL_SPECS}

    if db is not None:
        for signal_name, path, parser, ingester in present_specs:
            summary[signal_name] = _ingest_signal_file(
                db, path, parser, ingester, batch_size=batch_size
            )
        return summary

    url = database_url or get_settings().DATABASE_URL
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with Session(engine) as session:
            for signal_name, path, parser, ingester in present_specs:
                summary[signal_name] = _ingest_signal_file(
                    session, path, parser, ingester, batch_size=batch_size
                )
    finally:
        engine.dispose()

    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest captured OTLP JSONL files (traces, metrics, logs) into SignalForge."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/samples/otel"),
        help="Directory containing traces.jsonl, metrics.jsonl, and/or logs.jsonl.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Maximum records per ingestion batch (default: 500).",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy database URL override (defaults to DATABASE_URL from settings).",
    )
    return parser


def _configure_cli_logging(level: str) -> None:
    """Send structured JSON logs to stderr so stdout remains clean machine-readable JSON."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def main(argv: Sequence[str] | None = None) -> int:
    _configure_cli_logging(get_settings().LOG_LEVEL)
    args = build_arg_parser().parse_args(argv)
    try:
        summary = run_capture_ingestion(
            args.input_dir,
            batch_size=args.batch_size,
            database_url=args.database_url,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(
            f"Database error during OTLP capture ingestion: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
