from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

FEATURE_COLUMNS: tuple[str, ...] = (
    "cpu_utilization",
    "memory_utilization",
    "request_latency_ms",
    "request_throughput",
    "error_rate",
    "db_connections",
    "queue_depth",
)

SCENARIO_NAMES: tuple[str, ...] = (
    "cpu_saturation",
    "db_connection_exhaustion",
    "payment_service_failure",
    "memory_leak",
    "network_latency",
    "faulty_deployment",
    "message_queue_backlog",
)


class FaultEvent(BaseModel):
    """Ground-truth fault interval metadata for a synthetic anomaly scenario."""

    model_config = ConfigDict(frozen=True)

    scenario: str
    service: str
    split: str
    start_time_utc: datetime
    end_time_utc: datetime
    affected_metrics: list[str]
    description: str
    is_synthetic: bool = True


@dataclass(frozen=True)
class SyntheticBenchmarkDataset:
    """Deterministic synthetic multivariate telemetry dataset and ground-truth manifest."""

    dataframe: pd.DataFrame
    fault_events: list[FaultEvent]
    seed: int
    days: int
    freq_minutes: int
    is_synthetic: bool = True


def _ar1_process(rng: np.random.Generator, length: int, phi: float = 0.92) -> np.ndarray:
    """Generate a zero-mean unit-scale AR(1) autocorrelated background series."""
    innovations = rng.normal(0.0, np.sqrt(1.0 - phi**2), size=length)
    out = np.empty(length, dtype=np.float64)
    prev = 0.0
    for i in range(length):
        prev = phi * prev + innovations[i]
        out[i] = prev
    return out


def _pulse_envelope(length: int, ramp_fraction: float = 0.15) -> np.ndarray:
    """Smooth trapezoidal envelope rising and decaying cleanly inside a fault window."""
    if length <= 2:
        return np.ones(length, dtype=np.float64)
    ramp = max(1, int(length * ramp_fraction))
    env = np.ones(length, dtype=np.float64)
    env[:ramp] = np.linspace(0.25, 1.0, ramp)
    env[-ramp:] = np.linspace(1.0, 0.25, ramp)
    return env


def generate_benchmark_dataset(
    seed: int = 42,
    days: int = 7,
    freq_minutes: int = 1,
    start_time_utc: datetime = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC),
) -> SyntheticBenchmarkDataset:
    """Generate a deterministic 7-feature synthetic telemetry benchmark with 7 fault scenarios.

    Split layout (aligned with 60% / 20% / 20% chronological splitting):
    - Train (first 60%): normal baseline only (0 faults).
    - Validation (next 20%): 2 injected faults (cpu_saturation, network_latency).
    - Test (final 20%): 5 held-out injected faults.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if freq_minutes < 1:
        raise ValueError("freq_minutes must be at least 1")
    if start_time_utc.tzinfo is None or start_time_utc.utcoffset() is None:
        raise ValueError("start_time_utc must be timezone-aware")

    start_utc = start_time_utc.astimezone(UTC)
    total_points = (days * 24 * 60) // freq_minutes
    if total_points < 250:
        raise ValueError("Dataset must contain at least 250 time points to fit all 7 scenarios")

    rng = np.random.default_rng(seed)
    timestamps = pd.date_range(start=start_utc, periods=total_points, freq=f"{freq_minutes}min")

    # Diurnal load curve with mild harmonic variation and autocorrelated drift
    hours = timestamps.hour.to_numpy(dtype=np.float64) + (
        timestamps.minute.to_numpy(dtype=np.float64) / 60.0
    )
    diurnal = (
        0.5
        + 0.42 * np.sin(2.0 * np.pi * (hours - 8.0) / 24.0)
        + 0.08 * np.sin(4.0 * np.pi * (hours - 6.0) / 24.0)
    )
    load_factor = np.clip(diurnal + 0.05 * _ar1_process(rng, total_points, phi=0.95), 0.05, 1.0)

    # Normal correlated baseline across the 7 standard operational metrics
    request_throughput = (
        110.0 + 260.0 * load_factor + rng.normal(0.0, 9.0, size=total_points)
    )
    cpu_utilization = (
        0.20
        + 0.36 * load_factor
        + 0.025 * _ar1_process(rng, total_points, phi=0.90)
        + rng.normal(0.0, 0.015, size=total_points)
    )
    memory_utilization = (
        0.42
        + 0.08 * load_factor
        + 0.02 * _ar1_process(rng, total_points, phi=0.97)
        + rng.normal(0.0, 0.008, size=total_points)
    )
    db_connections = (
        16.0
        + 36.0 * load_factor
        + 2.5 * _ar1_process(rng, total_points, phi=0.88)
        + rng.normal(0.0, 1.8, size=total_points)
    )
    queue_depth = (
        10.0
        + 22.0 * load_factor
        + 3.0 * _ar1_process(rng, total_points, phi=0.85)
        + rng.normal(0.0, 2.0, size=total_points)
    )
    request_latency_ms = (
        32.0
        + 40.0 * load_factor
        + 22.0 * cpu_utilization
        + 0.25 * db_connections
        + rng.normal(0.0, 3.5, size=total_points)
    )
    error_rate = (
        0.0025
        + 0.003 * load_factor
        + np.abs(rng.normal(0.0, 0.0012, size=total_points))
    )

    is_anomaly = np.zeros(total_points, dtype=np.int64)
    scenario_labels = np.full(total_points, "normal", dtype=object)

    train_end = int(total_points * 0.6)
    val_end = int(total_points * 0.8)
    val_len = val_end - train_end
    test_len = total_points - val_end

    fault_events: list[FaultEvent] = []

    def _window_bounds(
        split_start: int,
        split_length: int,
        start_frac: float,
        dur_frac: float,
    ) -> tuple[int, int]:
        s = split_start + int(split_length * start_frac)
        dur = max(6, int(split_length * dur_frac))
        e = min(split_start + split_length - 2, s + dur - 1)
        return s, e

    def _record_fault(
        *,
        scenario: str,
        service: str,
        split: str,
        start_idx: int,
        end_idx: int,
        affected_metrics: list[str],
        description: str,
    ) -> None:
        is_anomaly[start_idx : end_idx + 1] = 1
        scenario_labels[start_idx : end_idx + 1] = scenario
        fault_events.append(
            FaultEvent(
                scenario=scenario,
                service=service,
                split=split,
                start_time_utc=timestamps[start_idx].to_pydatetime(),
                end_time_utc=timestamps[end_idx].to_pydatetime(),
                affected_metrics=affected_metrics,
                description=description,
                is_synthetic=True,
            )
        )

    # --- Validation Split Faults (2 scenarios) ---
    # 1. cpu_saturation
    s, e = _window_bounds(train_end, val_len, start_frac=0.18, dur_frac=0.10)
    env = _pulse_envelope(e - s + 1)
    cpu_utilization[s : e + 1] += 0.48 * env + rng.normal(0.0, 0.015, size=e - s + 1)
    request_latency_ms[s : e + 1] += 210.0 * env + rng.normal(0.0, 10.0, size=e - s + 1)
    request_throughput[s : e + 1] *= 1.0 - 0.18 * env
    error_rate[s : e + 1] += 0.045 * env
    _record_fault(
        scenario="cpu_saturation",
        service="checkout-service",
        split="validation",
        start_idx=s,
        end_idx=e,
        affected_metrics=[
            "cpu_utilization",
            "request_latency_ms",
            "request_throughput",
            "error_rate",
        ],
        description="Synthetic CPU saturation causing latency elevation and modest throttling.",
    )

    # 2. network_latency
    s, e = _window_bounds(train_end, val_len, start_frac=0.62, dur_frac=0.10)
    env = _pulse_envelope(e - s + 1)
    request_latency_ms[s : e + 1] += 360.0 * env + rng.normal(0.0, 28.0, size=e - s + 1)
    queue_depth[s : e + 1] += 95.0 * env
    error_rate[s : e + 1] += 0.04 * env
    _record_fault(
        scenario="network_latency",
        service="api-gateway",
        split="validation",
        start_idx=s,
        end_idx=e,
        affected_metrics=["request_latency_ms", "queue_depth", "error_rate"],
        description="Synthetic network jitter spiking request latency and inflight queues.",
    )

    # --- Test Split Faults (5 held-out scenarios) ---
    # 3. db_connection_exhaustion
    s, e = _window_bounds(val_end, test_len, start_frac=0.06, dur_frac=0.07)
    env = _pulse_envelope(e - s + 1)
    db_connections[s : e + 1] += 125.0 * env + rng.normal(0.0, 4.0, size=e - s + 1)
    request_latency_ms[s : e + 1] += 420.0 * env + rng.normal(0.0, 18.0, size=e - s + 1)
    error_rate[s : e + 1] += 0.20 * env
    request_throughput[s : e + 1] *= 1.0 - 0.28 * env
    _record_fault(
        scenario="db_connection_exhaustion",
        service="orders-service",
        split="test",
        start_idx=s,
        end_idx=e,
        affected_metrics=[
            "db_connections",
            "request_latency_ms",
            "error_rate",
            "request_throughput",
        ],
        description="Synthetic PostgreSQL connection pool exhaustion causing timeouts and errors.",
    )

    # 4. payment_service_failure
    s, e = _window_bounds(val_end, test_len, start_frac=0.22, dur_frac=0.07)
    env = _pulse_envelope(e - s + 1)
    error_rate[s : e + 1] += 0.38 * env + rng.normal(0.0, 0.015, size=e - s + 1)
    request_latency_ms[s : e + 1] += 510.0 * env + rng.normal(0.0, 22.0, size=e - s + 1)
    request_throughput[s : e + 1] *= 1.0 - 0.45 * env
    _record_fault(
        scenario="payment_service_failure",
        service="payment-service",
        split="test",
        start_idx=s,
        end_idx=e,
        affected_metrics=["error_rate", "request_latency_ms", "request_throughput"],
        description="Synthetic payment gateway outage producing high HTTP 5xx rates and timeouts.",
    )

    # 5. memory_leak (gradual ramp culminating in GC pressure and errors)
    s, e = _window_bounds(val_end, test_len, start_frac=0.38, dur_frac=0.14)
    length = e - s + 1
    ramp = np.linspace(0.08, 1.0, length)
    late_stage = np.clip((ramp - 0.55) / 0.45, 0.0, 1.0)
    memory_utilization[s : e + 1] += 0.48 * ramp + rng.normal(0.0, 0.008, size=length)
    cpu_utilization[s : e + 1] += 0.25 * late_stage
    request_latency_ms[s : e + 1] += 210.0 * late_stage
    error_rate[s : e + 1] += 0.09 * late_stage
    _record_fault(
        scenario="memory_leak",
        service="recommendation-service",
        split="test",
        start_idx=s,
        end_idx=e,
        affected_metrics=[
            "memory_utilization",
            "cpu_utilization",
            "request_latency_ms",
            "error_rate",
        ],
        description="Synthetic gradual memory leak ramping heap usage until GC thrashing occurs.",
    )

    # 6. faulty_deployment (abrupt multi-metric step regression until rollback)
    s, e = _window_bounds(val_end, test_len, start_frac=0.61, dur_frac=0.07)
    length = e - s + 1
    step = np.ones(length, dtype=np.float64)
    error_rate[s : e + 1] += 0.24 * step + rng.normal(0.0, 0.01, size=length)
    cpu_utilization[s : e + 1] += 0.30 * step
    memory_utilization[s : e + 1] += 0.18 * step
    request_latency_ms[s : e + 1] += 265.0 * step + rng.normal(0.0, 12.0, size=length)
    _record_fault(
        scenario="faulty_deployment",
        service="checkout-service",
        split="test",
        start_idx=s,
        end_idx=e,
        affected_metrics=[
            "error_rate",
            "cpu_utilization",
            "memory_utilization",
            "request_latency_ms",
        ],
        description="Synthetic bad release causing abrupt error, CPU, memory, and latency jumps.",
    )

    # 7. message_queue_backlog
    s, e = _window_bounds(val_end, test_len, start_frac=0.78, dur_frac=0.09)
    env = _pulse_envelope(e - s + 1)
    queue_depth[s : e + 1] += 580.0 * env + rng.normal(0.0, 18.0, size=e - s + 1)
    request_latency_ms[s : e + 1] += 185.0 * env
    memory_utilization[s : e + 1] += 0.16 * env
    db_connections[s : e + 1] += 35.0 * env
    _record_fault(
        scenario="message_queue_backlog",
        service="notification-worker",
        split="test",
        start_idx=s,
        end_idx=e,
        affected_metrics=[
            "queue_depth",
            "request_latency_ms",
            "memory_utilization",
            "db_connections",
        ],
        description="Synthetic consumer slowdown creating sustained message queue depth buildup.",
    )

    # Clip all signals to physically realistic bounds
    df = pd.DataFrame(
        {
            "timestamp_utc": timestamps,
            "cpu_utilization": np.clip(cpu_utilization, 0.01, 0.995),
            "memory_utilization": np.clip(memory_utilization, 0.05, 0.995),
            "request_latency_ms": np.clip(request_latency_ms, 5.0, 10_000.0),
            "request_throughput": np.clip(request_throughput, 1.0, 5_000.0),
            "error_rate": np.clip(error_rate, 0.0, 1.0),
            "db_connections": np.clip(db_connections, 1.0, 500.0),
            "queue_depth": np.clip(queue_depth, 0.0, 5_000.0),
            "is_anomaly": is_anomaly,
            "scenario": scenario_labels,
            "is_synthetic": True,
        }
    )

    return SyntheticBenchmarkDataset(
        dataframe=df,
        fault_events=fault_events,
        seed=seed,
        days=days,
        freq_minutes=freq_minutes,
        is_synthetic=True,
    )
