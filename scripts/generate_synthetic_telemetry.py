import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from services.anomaly_detection.preprocessing import split_chronological
from services.anomaly_detection.synthetic_scenarios import (
    FEATURE_COLUMNS,
    generate_benchmark_dataset,
)


def export_synthetic_benchmark(
    output_dir: Path,
    *,
    seed: int = 42,
    days: int = 7,
    freq_minutes: int = 1,
) -> dict[str, Any]:
    """Generate and write deterministic synthetic telemetry CSV + ground-truth JSON manifest."""
    dataset = generate_benchmark_dataset(seed=seed, days=days, freq_minutes=freq_minutes)
    splits = split_chronological(dataset.dataframe)

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "telemetry.csv"
    manifest_path = output_dir / "manifest.json"

    df_out = dataset.dataframe.copy()
    df_out["timestamp_utc"] = df_out["timestamp_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df_out.to_csv(csv_path, index=False)

    manifest: dict[str, Any] = {
        "synthetic": True,
        "is_synthetic": True,
        "seed": dataset.seed,
        "days": dataset.days,
        "freq_minutes": dataset.freq_minutes,
        "row_count": len(dataset.dataframe),
        "anomaly_row_count": int(dataset.dataframe["is_anomaly"].sum()),
        "feature_columns": list(FEATURE_COLUMNS),
        "splits": {
            "train": {
                "rows": len(splits.train),
                "anomaly_rows": int(splits.train["is_anomaly"].sum()),
                "start_time_utc": splits.train["timestamp_utc"].iloc[0].isoformat(),
                "end_time_utc": splits.train["timestamp_utc"].iloc[-1].isoformat(),
            },
            "validation": {
                "rows": len(splits.val),
                "anomaly_rows": int(splits.val["is_anomaly"].sum()),
                "start_time_utc": splits.val["timestamp_utc"].iloc[0].isoformat(),
                "end_time_utc": splits.val["timestamp_utc"].iloc[-1].isoformat(),
            },
            "test": {
                "rows": len(splits.test),
                "anomaly_rows": int(splits.test["is_anomaly"].sum()),
                "start_time_utc": splits.test["timestamp_utc"].iloc[0].isoformat(),
                "end_time_utc": splits.test["timestamp_utc"].iloc[-1].isoformat(),
            },
        },
        "fault_events": [event.model_dump(mode="json") for event in dataset.fault_events],
        "files": {
            "telemetry_csv": csv_path.name,
            "manifest_json": manifest_path.name,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "synthetic": True,
        "output_dir": str(output_dir),
        "csv_path": str(csv_path),
        "manifest_path": str(manifest_path),
        "row_count": manifest["row_count"],
        "anomaly_row_count": manifest["anomaly_row_count"],
        "fault_scenarios": [event.scenario for event in dataset.fault_events],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate deterministic synthetic multivariate telemetry benchmark data."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/synthetic/anomaly_benchmark"),
        help="Destination directory for telemetry.csv and manifest.json.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42).")
    parser.add_argument("--days", type=int, default=7, help="Duration in days (default: 7).")
    parser.add_argument(
        "--freq-minutes",
        type=int,
        default=1,
        help="Sampling interval in minutes (default: 1).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        summary = export_synthetic_benchmark(
            args.output_dir,
            seed=args.seed,
            days=args.days,
            freq_minutes=args.freq_minutes,
        )
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
