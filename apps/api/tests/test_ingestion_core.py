import ast
import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from services.ingestion import (
    NormalizedEvent,
    compute_dedup_hash,
    normalize_event_type,
    normalize_service_name,
    normalize_severity,
    normalize_timestamp_utc,
    validate_and_normalize_batch,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestNormalizeServiceName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Payment_Service ", "payment-service"),
            ("auth.api", "auth-api"),
            ("  My__Core..API   v2  ", "my-core-api-v2"),
            ("---edge-case---", "edge-case"),
            ("a", "a"),
            ("0", "0"),
            ("a" * 128, "a" * 128),
        ],
    )
    def test_valid_service_names(self, raw: str, expected: str) -> None:
        assert normalize_service_name(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "---",
            "___",
            "$$$",
            "auth/api",
            "auth@api",
            "svc\nname",
            "a" * 129,
            None,
            123,
        ],
    )
    def test_invalid_service_names_rejected(self, raw: Any) -> None:
        with pytest.raises(ValueError):
            normalize_service_name(raw)


class TestNormalizeTimestampUtc:
    def test_iso_z_and_offset_converted_to_utc(self) -> None:
        ts_z = normalize_timestamp_utc("2026-11-20T14:30:00Z")
        ts_plus_one = normalize_timestamp_utc("2026-11-20T15:30:00+01:00")
        ts_minus_five = normalize_timestamp_utc("2026-11-20T09:30:00-05:00")
        expected = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)

        assert ts_z == expected
        assert ts_z.tzinfo == UTC
        assert ts_plus_one == expected
        assert ts_plus_one.tzinfo == UTC
        assert ts_minus_five == expected
        assert ts_minus_five.tzinfo == UTC

    def test_aware_datetime_converted_to_utc(self) -> None:
        aware = datetime(2026, 11, 20, 16, 30, 0, tzinfo=timezone(timedelta(hours=2)))
        result = normalize_timestamp_utc(aware)
        assert result == datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
        assert result.tzinfo == UTC

    def test_unix_seconds_and_milliseconds(self) -> None:
        assert normalize_timestamp_utc(0) == datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert normalize_timestamp_utc(1_700_000_000) == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=UTC
        )
        assert normalize_timestamp_utc(1_700_000_000.5) == datetime(
            2023, 11, 14, 22, 13, 20, 500_000, tzinfo=UTC
        )
        assert normalize_timestamp_utc(1_700_000_000_000) == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=UTC
        )
        assert normalize_timestamp_utc(1_700_000_000_123) == datetime(
            2023, 11, 14, 22, 13, 20, 123_000, tzinfo=UTC
        )
        assert normalize_timestamp_utc(1_700_000_000_250.0) == datetime(
            2023, 11, 14, 22, 13, 20, 250_000, tzinfo=UTC
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "2026-11-20T14:30:00",
            "2026-11-20",
            "1700000000",
            "",
            "   ",
            "not-a-timestamp",
            datetime(2026, 11, 20, 14, 30, 0),
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            -1,
            -0.001,
            1e20,
            None,
            date(2026, 11, 20),
            ["2026-11-20T14:30:00Z"],
        ],
    )
    def test_invalid_timestamps_rejected(self, raw: Any) -> None:
        with pytest.raises(ValueError):
            normalize_timestamp_utc(raw)


class TestNormalizeSeverityAndEventType:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "INFO"),
            ("debug", "DEBUG"),
            ("INFO", "INFO"),
            (" warning ", "WARNING"),
            ("warn", "WARNING"),
            ("WARN", "WARNING"),
            ("err", "ERROR"),
            ("ERROR", "ERROR"),
            ("fatal", "CRITICAL"),
            ("crit", "CRITICAL"),
            ("CRITICAL", "CRITICAL"),
            ("trace", "DEBUG"),
        ],
    )
    def test_valid_severities(self, raw: str | None, expected: str) -> None:
        assert normalize_severity(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "notice", "severe", "panic", 123])
    def test_invalid_severities_rejected(self, raw: Any) -> None:
        with pytest.raises(ValueError):
            normalize_severity(raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "log"),
            ("  Metric  ", "metric"),
            ("HTTP_ERROR", "http_error"),
            ("x" * 64, "x" * 64),
        ],
    )
    def test_valid_event_types(self, raw: str | None, expected: str) -> None:
        assert normalize_event_type(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "x" * 65, 42])
    def test_invalid_event_types_rejected(self, raw: Any) -> None:
        with pytest.raises(ValueError):
            normalize_event_type(raw)


class TestComputeDedupHash:
    def test_timezone_equivalence_in_fingerprint_mode(self) -> None:
        ts_plus_one = normalize_timestamp_utc("2026-11-20T15:30:00+01:00")
        ts_z = normalize_timestamp_utc("2026-11-20T14:30:00Z")
        raw_plus_one = datetime(2026, 11, 20, 15, 30, 0, tzinfo=timezone(timedelta(hours=1)))

        h1 = compute_dedup_hash("payment-service", ts_plus_one, "log", "ERROR", "timeout")
        h2 = compute_dedup_hash("payment-service", ts_z, "log", "ERROR", "timeout")
        h3 = compute_dedup_hash("payment-service", raw_plus_one, "log", "ERROR", "timeout")

        expected_payload = "fp|payment-service|2026-11-20T14:30:00+00:00|log|ERROR|timeout||"
        expected_hash = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()

        assert len(h1) == 64
        assert h1 == h2 == h3 == expected_hash

    def test_external_id_mode_and_fallback(self) -> None:
        ts1 = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
        ts2 = datetime(2026, 11, 20, 15, 0, 0, tzinfo=UTC)

        h_ext_1 = compute_dedup_hash(
            "auth-api", ts1, "log", "ERROR", "msg-1", external_id="  evt-99  "
        )
        h_ext_2 = compute_dedup_hash(
            "auth-api", ts2, "alert", "CRITICAL", "msg-2", external_id="evt-99"
        )
        expected_ext = hashlib.sha256(b"ext|auth-api|evt-99").hexdigest()

        assert h_ext_1 == h_ext_2 == expected_ext

        # Different service with same external_id produces distinct hash.
        h_other_svc = compute_dedup_hash(
            "billing-api", ts1, "log", "ERROR", "msg-1", external_id="evt-99"
        )
        assert h_other_svc != h_ext_1

        # Whitespace-only external_id falls back to fingerprint mode.
        h_ws_ext = compute_dedup_hash(
            "auth-api", ts1, "log", "ERROR", "msg-1", external_id="   "
        )
        h_no_ext = compute_dedup_hash("auth-api", ts1, "log", "ERROR", "msg-1")
        assert h_ws_ext == h_no_ext
        assert h_ws_ext != h_ext_1

    def test_fingerprint_includes_trace_and_span(self) -> None:
        ts = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
        h_base = compute_dedup_hash("api", ts, "log", "INFO", "ok")
        h_trace = compute_dedup_hash("api", ts, "log", "INFO", "ok", trace_id="t1")
        h_span = compute_dedup_hash("api", ts, "log", "INFO", "ok", trace_id="t1", span_id="s1")

        assert len({h_base, h_trace, h_span}) == 3


class TestValidateAndNormalizeBatch:
    def test_mixed_batch_isolates_failures_and_preserves_indices(self) -> None:
        batch: list[Any] = [
            # 0: valid full record (+01:00)
            {
                "service": "Payment_Service ",
                "timestamp": "2026-11-20T15:30:00+01:00",
                "message": "  Card gateway timeout  ",
                "event_type": "HTTP_ERROR",
                "severity": "warn",
                "external_id": " ext-1 ",
                "trace_id": " trace-abc ",
                "span_id": " span-123 ",
                "attributes": {"status_code": 504, "retry": True},
                "ignored_extra_field": "ok",
            },
            # 1: missing required message
            {
                "service": "auth.api",
                "timestamp": "2026-11-20T14:30:00Z",
            },
            # 2: non-dict item
            "raw-string-record",
            # 3: invalid attributes (list instead of dict)
            {
                "service": "auth.api",
                "timestamp": "2026-11-20T14:30:00Z",
                "message": "bad attributes",
                "attributes": ["not", "a", "dict"],
            },
            # 4: naive timestamp
            {
                "service": "auth.api",
                "timestamp": "2026-11-20T14:30:00",
                "message": "naive ts",
            },
            # 5: non-finite float timestamp (must still produce JSONB-safe raw_payload)
            {
                "service": "auth.api",
                "timestamp": float("nan"),
                "message": "nan ts",
            },
            # 6: valid minimal record with Unix ms timestamp
            {
                "service": "auth.api",
                "timestamp": 1_700_000_000_123,
                "message": "Token refreshed",
            },
        ]

        result = validate_and_normalize_batch(batch)

        assert [e.record_index for e in result.valid_events] == [0, 6]
        assert [f.record_index for f in result.failures] == [1, 2, 3, 4, 5]

        ev0 = result.valid_events[0]
        assert ev0.service_name == "payment-service"
        assert ev0.timestamp_utc == datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
        assert ev0.event_type == "http_error"
        assert ev0.severity == "WARNING"
        assert ev0.message == "Card gateway timeout"
        assert ev0.external_id == "ext-1"
        assert ev0.trace_id == "trace-abc"
        assert ev0.span_id == "span-123"
        assert ev0.attributes == {"status_code": 504, "retry": True}
        assert len(ev0.dedup_hash) == 64

        ev6 = result.valid_events[1]
        assert ev6.service_name == "auth-api"
        assert ev6.timestamp_utc == datetime(2023, 11, 14, 22, 13, 20, 123_000, tzinfo=UTC)
        assert ev6.event_type == "log"
        assert ev6.severity == "INFO"
        assert ev6.external_id is None
        assert ev6.attributes == {}

        # Check failure codes and payloads.
        by_index = {f.record_index: f for f in result.failures}
        assert by_index[1].error_code == "VALIDATION_ERROR"
        assert by_index[1].raw_payload == batch[1]

        assert by_index[2].error_code == "INVALID_RECORD_TYPE"
        assert by_index[2].raw_payload == {"raw": repr("raw-string-record")}

        assert by_index[3].error_code == "VALIDATION_ERROR"
        assert by_index[3].raw_payload == batch[3]

        assert by_index[4].error_code == "VALIDATION_ERROR"
        assert by_index[4].raw_payload == batch[4]

        assert by_index[5].error_code == "VALIDATION_ERROR"
        # Ensure strict RFC 8259 JSON serializability for PostgreSQL JSONB.
        for failure in result.failures:
            json.dumps(failure.raw_payload, allow_nan=False)

    def test_batch_timezone_equivalent_records_produce_identical_dedup_hash(self) -> None:
        result = validate_and_normalize_batch(
            [
                {
                    "service": "Payment_Service",
                    "timestamp": "2026-11-20T15:30:00+01:00",
                    "message": "Timeout",
                    "severity": "err",
                },
                {
                    "service": "payment-service",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "Timeout",
                    "severity": "ERROR",
                },
            ]
        )
        assert len(result.failures) == 0
        assert len(result.valid_events) == 2
        assert result.valid_events[0].dedup_hash == result.valid_events[1].dedup_hash

    def test_optional_id_length_bounds_and_blank_coercion(self) -> None:
        result = validate_and_normalize_batch(
            [
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "ok",
                    "external_id": "   ",
                    "trace_id": "",
                    "span_id": "  ",
                },
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "bad ext",
                    "external_id": "x" * 257,
                },
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "bad trace",
                    "trace_id": "t" * 129,
                },
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "bad span",
                    "span_id": "s" * 65,
                },
            ]
        )
        assert len(result.valid_events) == 1
        ev = result.valid_events[0]
        assert ev.external_id is None
        assert ev.trace_id is None
        assert ev.span_id is None
        assert [f.record_index for f in result.failures] == [1, 2, 3]

    def test_normalized_event_is_frozen(self) -> None:
        result = validate_and_normalize_batch(
            [
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "immutable check",
                }
            ]
        )
        ev: NormalizedEvent = result.valid_events[0]
        with pytest.raises(ValidationError):
            ev.severity = "CRITICAL"  # type: ignore[misc]

    def test_empty_batch(self) -> None:
        result = validate_and_normalize_batch([])
        assert result.valid_events == []
        assert result.failures == []

    def test_additional_field_edge_cases(self) -> None:
        result = validate_and_normalize_batch(
            [
                # 0: explicit None severity and event_type fall back to defaults
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "ok",
                    "severity": None,
                    "event_type": None,
                },
                # 1: whitespace-only message rejected
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "   ",
                },
                # 2: attributes=None rejected
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "ok",
                    "attributes": None,
                },
                # 3: attributes as list of pairs rejected (no silent Pydantic coercion)
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "ok",
                    "attributes": [("k", "v")],
                },
                # 4: attributes with non-string key rejected, sanitized in raw_payload
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "ok",
                    "attributes": {1: "v"},
                },
                # 5: boolean timestamp rejected
                {
                    "service": "api",
                    "timestamp": True,
                    "message": "ok",
                },
                # 6: non-dict None item
                None,
            ]
        )
        assert [e.record_index for e in result.valid_events] == [0]
        assert result.valid_events[0].severity == "INFO"
        assert result.valid_events[0].event_type == "log"
        assert [f.record_index for f in result.failures] == [1, 2, 3, 4, 5, 6]
        assert result.failures[-1].error_code == "INVALID_RECORD_TYPE"
        assert result.failures[-1].raw_payload == {"raw": "None"}
        for failure in result.failures:
            json.dumps(failure.raw_payload, allow_nan=False)


def test_ingestion_core_has_no_framework_or_db_imports() -> None:
    forbidden = {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette"}
    for filename in ("normalizer.py", "validator.py"):
        path = REPO_ROOT / "services" / "ingestion" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        assert imported_roots.isdisjoint(forbidden), (
            f"{filename} imported forbidden modules: {imported_roots & forbidden}"
        )
