import ast
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.src.models import Event, MetricPoint, Service, TraceSpan
from apps.api.tests.conftest import TEST_URL
from services.ingestion.parsers.otel import (
    extract_otlp_attributes,
    load_jsonl_file,
    map_otlp_log_severity,
    otlp_unix_nano_to_iso,
    parse_otlp_logs,
    parse_otlp_metrics,
    parse_otlp_traces,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_OTEL_DIR = REPO_ROOT / "data" / "samples" / "otel"
CLI_SCRIPT_PATH = REPO_ROOT / "scripts" / "ingest_otel_capture.py"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ingest_otel_capture", CLI_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOtlpPrimitives:
    def test_extract_otlp_attributes_all_value_types(self) -> None:
        raw_attrs = [
            {"key": "str_k", "value": {"stringValue": "hello"}},
            {"key": "int_str_k", "value": {"intValue": "42"}},
            {"key": "int_k", "value": {"intValue": 7}},
            {"key": "dbl_k", "value": {"doubleValue": 3.14}},
            {"key": "bool_k", "value": {"boolValue": True}},
            {
                "key": "arr_k",
                "value": {
                    "arrayValue": {
                        "values": [
                            {"stringValue": "a"},
                            {"intValue": "2"},
                            {"boolValue": False},
                        ]
                    }
                },
            },
            {
                "key": "kv_k",
                "value": {
                    "kvlistValue": {
                        "values": [{"key": "nested", "value": {"stringValue": "ok"}}]
                    }
                },
            },
        ]
        assert extract_otlp_attributes(raw_attrs) == {
            "str_k": "hello",
            "int_str_k": 42,
            "int_k": 7,
            "dbl_k": 3.14,
            "bool_k": True,
            "arr_k": ["a", 2, False],
            "kv_k": {"nested": "ok"},
        }

    def test_otlp_unix_nano_to_iso_preserves_microseconds(self) -> None:
        # 2026-11-20T15:00:00.123456+00:00
        nanos_str = "1795186800123456789"
        nanos_int = 1795186800123456789
        expected = "2026-11-20T15:00:00.123456+00:00"
        assert otlp_unix_nano_to_iso(nanos_str) == expected
        assert otlp_unix_nano_to_iso(nanos_int) == expected

    @pytest.mark.parametrize("bad", ["", "not-a-number", -1, True, False, 1.5])
    def test_otlp_unix_nano_rejects_invalid_inputs(self, bad: object) -> None:
        with pytest.raises(ValueError):
            otlp_unix_nano_to_iso(bad)  # type: ignore[arg-type]

    def test_load_jsonl_file_skips_blank_lines_and_reports_errors(self, tmp_path: Path) -> None:
        valid_file = tmp_path / "ok.jsonl"
        valid_file.write_text('\n{"a": 1}\n\n  {"b": 2}  \n', encoding="utf-8")
        assert load_jsonl_file(valid_file) == [{"a": 1}, {"b": 2}]

        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('{"ok": true}\n{bad-json\n', encoding="utf-8")
        with pytest.raises(ValueError, match=":2: malformed JSON"):
            load_jsonl_file(bad_file)

        non_obj = tmp_path / "list.jsonl"
        non_obj.write_text("[1, 2, 3]\n", encoding="utf-8")
        with pytest.raises(ValueError, match=":1: expected JSON object"):
            load_jsonl_file(non_obj)


class TestOtlpSignalParsers:
    def test_parse_otlp_traces_defaults_and_mappings(self) -> None:
        payload = {
            "resourceSpans": [
                {
                    "resource": {},
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "t-1",
                                    "spanId": "s-1",
                                    "parentSpanId": "",
                                    "name": "GET /health",
                                    "kind": 2,
                                    "startTimeUnixNano": "1795186800000000000",
                                    "endTimeUnixNano": "1795186800050000000",
                                },
                                {
                                    "traceId": "t-1",
                                    "spanId": "s-2",
                                    "parentSpanId": "s-1",
                                    "name": "db.query",
                                    "kind": "SPAN_KIND_CLIENT",
                                    "startTimeUnixNano": 1795186800010000000,
                                    "endTimeUnixNano": 1795186800040000000,
                                    "status": {"code": 2, "message": "deadlock"},
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        spans = parse_otlp_traces(payload)
        assert len(spans) == 2

        assert spans[0]["service"] == "unknown-service"
        assert spans[0]["parent_span_id"] is None
        assert spans[0]["span_kind"] == "SERVER"
        assert spans[0]["status_code"] == "UNSET"
        assert spans[0]["attributes"] == {}

        assert spans[1]["parent_span_id"] == "s-1"
        assert spans[1]["span_kind"] == "CLIENT"
        assert spans[1]["status_code"] == "ERROR"
        assert spans[1]["attributes"]["otel.status_message"] == "deadlock"

    def test_parse_otlp_metrics_gauge_sum_and_skips_histogram(self) -> None:
        payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "cart_service"}}
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "cpu.usage",
                                    "unit": "ratio",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": "1795186800000000000",
                                                "asDouble": 0.45,
                                                "attributes": [
                                                    {
                                                        "key": "core",
                                                        "value": {"intValue": "0"},
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                },
                                {
                                    "name": "orders.count",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": "1795186800010000000",
                                                "asInt": "15",
                                            },
                                            {
                                                "timeUnixNano": "1795186800020000000",
                                                "asInt": 20,
                                            },
                                        ]
                                    },
                                },
                                {
                                    "name": "latency.hist",
                                    "histogram": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": "1795186800030000000",
                                                "count": "3",
                                            }
                                        ]
                                    },
                                },
                            ]
                        }
                    ],
                }
            ]
        }

        points = parse_otlp_metrics(payload)
        assert len(points) == 3
        assert points[0]["service"] == "cart_service"
        assert points[0]["value"] == pytest.approx(0.45)
        assert points[0]["unit"] == "ratio"
        assert points[0]["labels"] == {"core": 0}
        assert points[1]["value"] == pytest.approx(15.0)
        assert points[1]["unit"] is None
        assert points[2]["value"] == pytest.approx(20.0)

    def test_parse_otlp_logs_severity_fallback_and_missing_service(self) -> None:
        payload = {
            "resourceLogs": [
                {
                    "resource": {},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "observedTimeUnixNano": "1795186800000000000",
                                    "severityNumber": 14,
                                    "body": {"stringValue": "Disk usage high"},
                                    "traceId": "t-9",
                                    "spanId": "s-9",
                                },
                                {
                                    "timeUnixNano": "1795186800010000000",
                                    "severityText": "FATAL",
                                    "body": {"stringValue": "OOM killed"},
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        logs = parse_otlp_logs(payload)
        assert len(logs) == 2
        assert logs[0]["service"] == "unknown-service"
        assert logs[0]["severity"] == "WARNING"
        assert logs[0]["message"] == "Disk usage high"
        assert logs[0]["trace_id"] == "t-9"
        assert logs[0]["span_id"] == "s-9"
        assert logs[1]["severity"] == "CRITICAL"

        assert map_otlp_log_severity(None, 3) == "DEBUG"
        assert map_otlp_log_severity(None, 9) == "INFO"
        assert map_otlp_log_severity(None, 18) == "ERROR"
        assert map_otlp_log_severity(None, 22) == "CRITICAL"
        assert map_otlp_log_severity(None, None) == "INFO"


def test_otel_parser_has_no_framework_or_db_imports() -> None:
    path = REPO_ROOT / "services" / "ingestion" / "parsers" / "otel.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert imported_roots.isdisjoint({"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette"})


class TestCaptureIngestionCli:
    def test_end_to_end_idempotent_ingestion_against_sample_otel(
        self, db_session: Session, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = _load_cli_module()

        # Run 1 via CLI main() targeting signalforge_test
        exit1 = cli.main(
            [
                "--input-dir",
                str(SAMPLE_OTEL_DIR),
                "--batch-size",
                "2",
                "--database-url",
                TEST_URL,
            ]
        )
        assert exit1 == 0
        summary1 = json.loads(capsys.readouterr().out)

        assert summary1["traces"] == {"received": 4, "accepted": 4, "duplicates": 0, "rejected": 0}
        assert summary1["metrics"] == {
            "received": 4,
            "accepted": 4,
            "duplicates": 0,
            "rejected": 0,
        }
        assert summary1["logs"] == {"received": 4, "accepted": 4, "duplicates": 0, "rejected": 0}

        # Verify services auto-discovered and normalized
        service_names = db_session.scalars(select(Service.name).order_by(Service.name)).all()
        assert service_names == ["checkout-service", "frontend", "payment-service"]

        # Verify parent_span_id chain preserved in trace_spans
        spans = db_session.scalars(
            select(TraceSpan).where(TraceSpan.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736")
        ).all()
        parent_map = {s.span_id: s.parent_span_id for s in spans}
        assert parent_map == {
            "00f067aa0ba902b7": None,
            "11f067aa0ba902c8": "00f067aa0ba902b7",
            "22f067aa0ba902d9": "11f067aa0ba902c8",
        }

        assert len(db_session.scalars(select(MetricPoint)).all()) == 4
        assert len(db_session.scalars(select(Event)).all()) == 4

        # Run 2: strictly idempotent
        exit2 = cli.main(
            [
                "--input-dir",
                str(SAMPLE_OTEL_DIR),
                "--batch-size",
                "2",
                "--database-url",
                TEST_URL,
            ]
        )
        assert exit2 == 0
        summary2 = json.loads(capsys.readouterr().out)
        for signal in ("traces", "metrics", "logs"):
            assert summary2[signal]["accepted"] == 0
            assert summary2[signal]["duplicates"] == summary2[signal]["received"]
            assert summary2[signal]["rejected"] == 0

    def test_cli_error_exit_codes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = _load_cli_module()

        # Non-existent directory
        rc_missing = cli.main(["--input-dir", str(tmp_path / "does-not-exist")])
        assert rc_missing == 1
        assert "does not exist" in capsys.readouterr().err

        # Empty directory (no OTLP JSONL files)
        rc_empty = cli.main(["--input-dir", str(tmp_path)])
        assert rc_empty == 1
        assert "No OTLP JSONL files" in capsys.readouterr().err

        # Malformed JSONL file
        (tmp_path / "traces.jsonl").write_text("{broken-json\n", encoding="utf-8")
        rc_bad = cli.main(["--input-dir", str(tmp_path), "--database-url", TEST_URL])
        assert rc_bad == 1
        assert "malformed JSON" in capsys.readouterr().err
