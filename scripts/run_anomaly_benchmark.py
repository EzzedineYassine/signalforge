import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from services.anomaly_detection import (
    FEATURE_COLUMNS,
    IsolationForestDetector,
    LSTMAutoencoderDetector,
    MultivariatePreprocessor,
    RollingZScoreDetector,
    evaluate_predictions,
    generate_benchmark_dataset,
    split_chronological,
)


def run_benchmark(
    output_dir: Path,
    *,
    seed: int = 42,
    days: int = 7,
    freq_minutes: int = 1,
    window_size: int = 30,
    lstm_epochs: int = 25,
    save_artifacts: bool = True,
) -> dict[str, Any]:
    """Run leakage-free 3-way anomaly benchmark on the deterministic synthetic dataset."""
    dataset = generate_benchmark_dataset(seed=seed, days=days, freq_minutes=freq_minutes)
    splits = split_chronological(dataset.dataframe, train_ratio=0.6, val_ratio=0.2)

    preprocessor = MultivariatePreprocessor(
        feature_columns=FEATURE_COLUMNS,
        window_size=window_size,
    ).fit(splits.train)

    x_train = preprocessor.transform(splits.train)
    x_val = preprocessor.transform(splits.val)
    x_test = preprocessor.transform(splits.test)
    y_val = splits.val["is_anomaly"].to_numpy(dtype=np.int64)
    y_test = splits.test["is_anomaly"].to_numpy(dtype=np.int64)

    prefix_len = window_size - 1
    val_prefix = x_train[-prefix_len:] if prefix_len > 0 else None
    test_prefix = x_val[-prefix_len:] if prefix_len > 0 else None

    test_fault_scenarios = [
        event.scenario for event in dataset.fault_events if event.split == "test"
    ]
    val_fault_scenarios = [
        event.scenario for event in dataset.fault_events if event.split == "validation"
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output_dir / "artifacts"
    if save_artifacts:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        preprocessor.save(artifacts_dir / "preprocessor.json")

    detector_results: dict[str, dict[str, Any]] = {}

    # 1. Rolling Z-Score
    rz = RollingZScoreDetector(window_size=window_size)
    t0 = time.perf_counter()
    rz.fit(x_train)
    rz_fit_s = time.perf_counter() - t0
    rz_thresh = rz.calibrate_threshold(x_val, y_val)
    t1 = time.perf_counter()
    rz_scores = rz.score(x_test)
    rz_score_s = time.perf_counter() - t1
    rz_eval = evaluate_predictions(y_test, rz_scores, rz_thresh, freq_minutes=float(freq_minutes))
    if save_artifacts:
        rz.save(artifacts_dir / "rolling_zscore.json")
    detector_results["rolling_zscore"] = {
        **rz_eval.model_dump(),
        "per_fault_delays_by_scenario": dict(
            zip(test_fault_scenarios, rz_eval.per_fault_delays_minutes, strict=True)
        ),
        "fit_seconds": round(rz_fit_s, 6),
        "score_seconds": round(rz_score_s, 6),
        "inference_ms_per_1k": round((rz_score_s / len(x_test)) * 1_000_000.0, 4),
    }

    # 2. Isolation Forest
    iforest = IsolationForestDetector(n_estimators=200, random_state=seed, n_jobs=1)
    t0 = time.perf_counter()
    iforest.fit(x_train)
    if_fit_s = time.perf_counter() - t0
    if_thresh = iforest.calibrate_threshold(x_val, y_val)
    t1 = time.perf_counter()
    if_scores = iforest.score(x_test)
    if_score_s = time.perf_counter() - t1
    if_eval = evaluate_predictions(y_test, if_scores, if_thresh, freq_minutes=float(freq_minutes))
    if save_artifacts:
        iforest.save(artifacts_dir / "isolation_forest_config.json")
    detector_results["isolation_forest"] = {
        **if_eval.model_dump(),
        "per_fault_delays_by_scenario": dict(
            zip(test_fault_scenarios, if_eval.per_fault_delays_minutes, strict=True)
        ),
        "fit_seconds": round(if_fit_s, 6),
        "score_seconds": round(if_score_s, 6),
        "inference_ms_per_1k": round((if_score_s / len(x_test)) * 1_000_000.0, 4),
    }

    # 3. PyTorch LSTM Autoencoder (CPU)
    lstm = LSTMAutoencoderDetector(
        window_size=window_size,
        hidden_size=32,
        latent_size=16,
        num_layers=1,
        epochs=lstm_epochs,
        batch_size=64,
        learning_rate=1e-3,
        patience=5,
        random_state=seed,
        device="cpu",
    )
    t0 = time.perf_counter()
    lstm.fit(x_train)
    lstm_fit_s = time.perf_counter() - t0
    lstm_thresh = lstm.calibrate_threshold(x_val, y_val, context_prefix=val_prefix)
    t1 = time.perf_counter()
    lstm_scores = lstm.score(x_test, context_prefix=test_prefix)
    lstm_score_s = time.perf_counter() - t1
    lstm_eval = evaluate_predictions(
        y_test, lstm_scores, lstm_thresh, freq_minutes=float(freq_minutes)
    )
    if save_artifacts:
        lstm.save(artifacts_dir / "lstm_autoencoder")
    detector_results["lstm_autoencoder"] = {
        **lstm_eval.model_dump(),
        "per_fault_delays_by_scenario": dict(
            zip(test_fault_scenarios, lstm_eval.per_fault_delays_minutes, strict=True)
        ),
        "fit_seconds": round(lstm_fit_s, 6),
        "score_seconds": round(lstm_score_s, 6),
        "inference_ms_per_1k": round((lstm_score_s / len(x_test)) * 1_000_000.0, 4),
        "epochs_trained": len(lstm.train_losses_),
        "best_epoch": lstm.best_epoch_,
        "final_train_mse": lstm.train_losses_[-1] if lstm.train_losses_ else None,
        "best_internal_val_mse": min(lstm.val_losses_) if lstm.val_losses_ else None,
    }

    payload: dict[str, Any] = {
        "synthetic": True,
        "seed": seed,
        "days": days,
        "freq_minutes": freq_minutes,
        "window_size": window_size,
        "feature_columns": list(FEATURE_COLUMNS),
        "splits": {
            "train_rows": len(splits.train),
            "validation_rows": len(splits.val),
            "test_rows": len(splits.test),
            "validation_fault_scenarios": val_fault_scenarios,
            "test_fault_scenarios": test_fault_scenarios,
            "test_positive_rate": float(np.mean(y_test)),
        },
        "detectors": detector_results,
    }

    (output_dir / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run leakage-free 3-way anomaly detection benchmark on synthetic telemetry."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/benchmarks/anomaly_seed42"),
        help="Output directory for results.json and model artifacts.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--freq-minutes", type=int, default=1)
    parser.add_argument("--window-size", type=int, default=30)
    parser.add_argument("--lstm-epochs", type=int, default=25)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        results = run_benchmark(
            args.output_dir,
            seed=args.seed,
            days=args.days,
            freq_minutes=args.freq_minutes,
            window_size=args.window_size,
            lstm_epochs=args.lstm_epochs,
        )
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
