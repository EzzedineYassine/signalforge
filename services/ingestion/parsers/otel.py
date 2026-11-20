import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SPAN_KIND_BY_INT: dict[int, str] = {
    0: "INTERNAL",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}

_SPAN_KIND_BY_STR: dict[str, str] = {
    "SPAN_KIND_UNSPECIFIED": "INTERNAL",
    "SPAN_KIND_INTERNAL": "INTERNAL",
    "SPAN_KIND_SERVER": "SERVER",
    "SPAN_KIND_CLIENT": "CLIENT",
    "SPAN_KIND_PRODUCER": "PRODUCER",
    "SPAN_KIND_CONSUMER": "CONSUMER",
    "INTERNAL": "INTERNAL",
    "SERVER": "SERVER",
    "CLIENT": "CLIENT",
    "PRODUCER": "PRODUCER",
    "CONSUMER": "CONSUMER",
}

_SPAN_STATUS_BY_INT: dict[int, str] = {
    0: "UNSET",
    1: "OK",
    2: "ERROR",
}

_SPAN_STATUS_BY_STR: dict[str, str] = {
    "STATUS_CODE_UNSET": "UNSET",
    "STATUS_CODE_OK": "OK",
    "STATUS_CODE_ERROR": "ERROR",
    "UNSET": "UNSET",
    "OK": "OK",
    "ERROR": "ERROR",
}


def otlp_unix_nano_to_iso(value: str | int) -> str:
    """Convert an OTLP nanosecond Unix timestamp (int or stringified int64) to UTC ISO-8601."""
    if isinstance(value, bool):
        raise ValueError("OTLP timestamp cannot be a boolean")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("OTLP timestamp string cannot be empty")
        try:
            nanos = int(stripped)
        except ValueError as exc:
            raise ValueError(f"Invalid OTLP nanosecond timestamp: {value!r}") from exc
    elif isinstance(value, int):
        nanos = value
    else:
        raise ValueError(f"Unsupported OTLP timestamp type: {type(value).__name__}")

    if nanos < 0:
        raise ValueError(f"Negative OTLP timestamp is not allowed: {value!r}")

    seconds, remainder_ns = divmod(nanos, 1_000_000_000)
    microseconds = remainder_ns // 1_000
    try:
        dt = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"OTLP timestamp out of range: {value!r}") from exc
    return dt.isoformat()


def extract_otlp_any_value(raw: Any) -> Any:
    """Decode an OTLP AnyValue dict into a standard JSON-compatible Python value."""
    if not isinstance(raw, dict):
        return raw

    if "stringValue" in raw:
        return str(raw["stringValue"])
    if "boolValue" in raw:
        return bool(raw["boolValue"])
    if "intValue" in raw:
        int_raw = raw["intValue"]
        if isinstance(int_raw, bool):
            return int(int_raw)
        try:
            return int(int_raw)
        except (TypeError, ValueError):
            return str(int_raw)
    if "doubleValue" in raw:
        try:
            return float(raw["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "bytesValue" in raw:
        return str(raw["bytesValue"])
    if "arrayValue" in raw:
        arr = raw["arrayValue"]
        values = arr.get("values", []) if isinstance(arr, dict) else []
        return [extract_otlp_any_value(v) for v in values]
    if "kvlistValue" in raw:
        kvlist = raw["kvlistValue"]
        values = kvlist.get("values", []) if isinstance(kvlist, dict) else []
        return extract_otlp_attributes(values)
    return None


def extract_otlp_attributes(attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Convert an OTLP KeyValue list into a flat JSON-compatible Python dict."""
    if not isinstance(attributes, list):
        return {}
    result: dict[str, Any] = {}
    for item in attributes:
        if not isinstance(item, dict) or "key" not in item:
            continue
        key = str(item["key"])
        result[key] = extract_otlp_any_value(item.get("value"))
    return result


def extract_service_name(resource: dict[str, Any] | None) -> str:
    """Extract service.name from an OTLP Resource dict, defaulting to 'unknown-service'."""
    if not isinstance(resource, dict):
        return "unknown-service"
    attrs = extract_otlp_attributes(resource.get("attributes"))
    raw_name = attrs.get("service.name")
    if isinstance(raw_name, str) and raw_name.strip():
        return raw_name.strip()
    return "unknown-service"


def map_otlp_span_kind(raw_kind: int | str | None) -> str:
    """Map OTLP numeric or string SpanKind to SignalForge canonical span_kind."""
    if raw_kind is None:
        return "INTERNAL"
    if isinstance(raw_kind, int) and not isinstance(raw_kind, bool):
        return _SPAN_KIND_BY_INT.get(raw_kind, "INTERNAL")
    if isinstance(raw_kind, str):
        return _SPAN_KIND_BY_STR.get(raw_kind.strip().upper(), "INTERNAL")
    return "INTERNAL"


def map_otlp_span_status(raw_code: int | str | None) -> str:
    """Map OTLP numeric or string StatusCode to SignalForge canonical status_code."""
    if raw_code is None:
        return "UNSET"
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        return _SPAN_STATUS_BY_INT.get(raw_code, "UNSET")
    if isinstance(raw_code, str):
        return _SPAN_STATUS_BY_STR.get(raw_code.strip().upper(), "UNSET")
    return "UNSET"


def map_otlp_log_severity(
    severity_text: str | None = None,
    severity_number: int | None = None,
) -> str:
    """Map OTLP severityText or severityNumber (1..24) to canonical SignalForge severity."""
    if isinstance(severity_text, str):
        upper = severity_text.strip().upper()
        if upper.startswith(("TRACE", "DEBUG")):
            return "DEBUG"
        if upper.startswith("INFO"):
            return "INFO"
        if upper.startswith("WARN"):
            return "WARNING"
        if upper.startswith("ERR"):
            return "ERROR"
        if upper.startswith(("FATAL", "CRIT")):
            return "CRITICAL"

    if isinstance(severity_number, int) and not isinstance(severity_number, bool):
        if 1 <= severity_number <= 8:
            return "DEBUG"
        if 9 <= severity_number <= 12:
            return "INFO"
        if 13 <= severity_number <= 16:
            return "WARNING"
        if 17 <= severity_number <= 20:
            return "ERROR"
        if 21 <= severity_number <= 24:
            return "CRITICAL"

    return "INFO"


def _maybe_iso_nano(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return otlp_unix_nano_to_iso(value)


def parse_otlp_traces(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse an OTLP JSON ExportTraceServiceRequest into raw trace span dicts."""
    if not isinstance(payload, dict):
        return []

    records: list[dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        service = extract_service_name(resource_span.get("resource"))
        scope_spans = (
            resource_span.get("scopeSpans")
            or resource_span.get("instrumentationLibrarySpans")
            or []
        )
        for scope_span in scope_spans:
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                status_obj = span.get("status") if isinstance(span.get("status"), dict) else {}
                attributes = extract_otlp_attributes(span.get("attributes"))
                status_msg = status_obj.get("message")
                if isinstance(status_msg, str) and status_msg.strip():
                    attributes.setdefault("otel.status_message", status_msg.strip())

                parent_span_id = span.get("parentSpanId")
                if isinstance(parent_span_id, str) and not parent_span_id.strip():
                    parent_span_id = None

                records.append(
                    {
                        "service": service,
                        "trace_id": span.get("traceId"),
                        "span_id": span.get("spanId"),
                        "parent_span_id": parent_span_id,
                        "operation_name": span.get("name"),
                        "span_kind": map_otlp_span_kind(span.get("kind")),
                        "status_code": map_otlp_span_status(status_obj.get("code")),
                        "start_time": _maybe_iso_nano(span.get("startTimeUnixNano")),
                        "end_time": _maybe_iso_nano(span.get("endTimeUnixNano")),
                        "attributes": attributes,
                    }
                )
    return records


def _extract_datapoint_value(dp: dict[str, Any]) -> float | None:
    if "asDouble" in dp and dp["asDouble"] is not None:
        return float(dp["asDouble"])
    if "asInt" in dp and dp["asInt"] is not None:
        raw_int = dp["asInt"]
        if isinstance(raw_int, bool):
            return None
        return float(int(raw_int))
    return None


def parse_otlp_metrics(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse OTLP JSON gauge/sum metrics into raw metric point dicts.

    Unsupported metric shapes (such as histogram-only metrics) are skipped cleanly.
    """
    if not isinstance(payload, dict):
        return []

    records: list[dict[str, Any]] = []
    for resource_metric in payload.get("resourceMetrics") or []:
        if not isinstance(resource_metric, dict):
            continue
        service = extract_service_name(resource_metric.get("resource"))
        scope_metrics = (
            resource_metric.get("scopeMetrics")
            or resource_metric.get("instrumentationLibraryMetrics")
            or []
        )
        for scope_metric in scope_metrics:
            if not isinstance(scope_metric, dict):
                continue
            for metric in scope_metric.get("metrics") or []:
                if not isinstance(metric, dict):
                    continue
                metric_name = metric.get("name")
                raw_unit = metric.get("unit")
                unit = raw_unit.strip() if isinstance(raw_unit, str) and raw_unit.strip() else None

                gauge = metric.get("gauge")
                metric_sum = metric.get("sum")
                if isinstance(gauge, dict):
                    data_points = gauge.get("dataPoints") or []
                elif isinstance(metric_sum, dict):
                    data_points = metric_sum.get("dataPoints") or []
                else:
                    # Skip unsupported metric aggregations (e.g. histogram, summary) cleanly.
                    continue

                for dp in data_points:
                    if not isinstance(dp, dict):
                        continue
                    raw_ts = dp.get("timeUnixNano") or dp.get("startTimeUnixNano")
                    records.append(
                        {
                            "service": service,
                            "metric_name": metric_name,
                            "timestamp": _maybe_iso_nano(raw_ts),
                            "value": _extract_datapoint_value(dp),
                            "unit": unit,
                            "labels": extract_otlp_attributes(dp.get("attributes")),
                        }
                    )
    return records


def parse_otlp_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse an OTLP JSON ExportLogsServiceRequest into raw event dicts."""
    if not isinstance(payload, dict):
        return []

    records: list[dict[str, Any]] = []
    for resource_log in payload.get("resourceLogs") or []:
        if not isinstance(resource_log, dict):
            continue
        service = extract_service_name(resource_log.get("resource"))
        scope_logs = (
            resource_log.get("scopeLogs")
            or resource_log.get("instrumentationLibraryLogs")
            or []
        )
        for scope_log in scope_logs:
            if not isinstance(scope_log, dict):
                continue
            for log_record in scope_log.get("logRecords") or []:
                if not isinstance(log_record, dict):
                    continue
                raw_ts = log_record.get("timeUnixNano") or log_record.get("observedTimeUnixNano")
                body_value = extract_otlp_any_value(log_record.get("body"))
                if isinstance(body_value, str):
                    message = body_value
                elif body_value is not None:
                    message = json.dumps(body_value, sort_keys=True)
                else:
                    message = ""

                trace_id = log_record.get("traceId") or None
                span_id = log_record.get("spanId") or None
                attributes = extract_otlp_attributes(log_record.get("attributes"))

                records.append(
                    {
                        "service": service,
                        "timestamp": _maybe_iso_nano(raw_ts),
                        "event_type": "otel_log",
                        "severity": map_otlp_log_severity(
                            log_record.get("severityText"),
                            log_record.get("severityNumber"),
                        ),
                        "message": message,
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "attributes": attributes,
                    }
                )
    return records


def load_jsonl_file(path: Path) -> list[dict[str, Any]]:
    """Read newline-delimited JSON objects, ignoring blank lines and reporting bad line numbers."""
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: malformed JSON ({exc.msg})") from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"{path}:{line_no}: expected JSON object, got {type(parsed).__name__}"
                )
            items.append(parsed)
    return items
