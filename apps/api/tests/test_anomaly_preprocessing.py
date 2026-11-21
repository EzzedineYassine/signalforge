import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from services.anomaly_detection import (
    FEATURE_COLUMNS,
    SCENARIO_NAMES,
    MultivariatePreprocessor,
    build_windows,
    generate_benchmark_dataset,
    split_chronological,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestSyntheticBenchmarkGenerator:
    def test_determinism_same_seed_vs_different_seed(self) -> None:
        ds1 = generate_benchmark_dataset(seed=42, days=2, freq_minutes=1)
        ds2 = generate_benchmark_dataset(seed=42, days=2, freq_minutes=1)
        ds3 = generate_benchmark_dataset(seed=99, days=2, freq_minutes=1)

        pd.testing.assert_frame_equal(ds1.dataframe, ds2.dataframe)
        assert ds1.fault_events == ds2.fault_events

        with pytest.raises(AssertionError):
            pd.testing.assert_frame_equal(ds1.dataframe, ds3.dataframe)

    def test_default_7_day_shape_fault_placement_and_synthetic_flag(self) -> None:
        ds = generate_benchmark_dataset(seed=42, days=7, freq_minutes=1)
        df = ds.dataframe

        assert len(df) == 7 * 1440
        assert ds.is_synthetic is True
        assert bool(df["is_synthetic"].all()) is True
        assert set(FEATURE_COLUMNS) <= set(df.columns)
        assert np.all(np.isfinite(df[list(FEATURE_COLUMNS)].to_numpy(dtype=np.float64)))
        assert str(df["timestamp_utc"].dtype.tz) == "UTC"

        # All 7 scenarios present in fault manifest and marked synthetic
        manifest_scenarios = [e.scenario for e in ds.fault_events]
        assert len(manifest_scenarios) == 7
        assert set(manifest_scenarios) == set(SCENARIO_NAMES)
        assert all(e.is_synthetic for e in ds.fault_events)

        # Clean split placement: 0 faults in train, 2 in val, 5 in test
        splits = split_chronological(df, train_ratio=0.6, val_ratio=0.2)
        assert int(splits.train["is_anomaly"].sum()) == 0
        assert set(splits.train["scenario"].unique()) == {"normal"}

        val_scenarios = set(splits.val["scenario"].unique()) - {"normal"}
        test_scenarios = set(splits.test["scenario"].unique()) - {"normal"}
        assert len(val_scenarios) == 2
        assert len(test_scenarios) == 5
        assert val_scenarios.isdisjoint(test_scenarios)
        assert val_scenarios | test_scenarios == set(SCENARIO_NAMES)

        # Ground-truth manifest timestamps align exactly with labeled rows
        for event in ds.fault_events:
            window_rows = df[
                (df["timestamp_utc"] >= event.start_time_utc)
                & (df["timestamp_utc"] <= event.end_time_utc)
            ]
            assert len(window_rows) > 0
            assert (window_rows["is_anomaly"] == 1).all()
            assert (window_rows["scenario"] == event.scenario).all()

    def test_memory_leak_ramps_gradually(self) -> None:
        ds = generate_benchmark_dataset(seed=42, days=7, freq_minutes=1)
        leak_rows = ds.dataframe[ds.dataframe["scenario"] == "memory_leak"]
        first_quarter = leak_rows["memory_utilization"].iloc[: len(leak_rows) // 4].mean()
        last_quarter = leak_rows["memory_utilization"].iloc[-len(leak_rows) // 4 :].mean()
        assert last_quarter - first_quarter > 0.25


class TestChronologicalSplit:
    def test_disjoint_monotonic_splits(self) -> None:
        ds = generate_benchmark_dataset(seed=42, days=2, freq_minutes=1)
        splits = split_chronological(ds.dataframe, train_ratio=0.6, val_ratio=0.2)

        assert len(splits.train) + len(splits.val) + len(splits.test) == len(ds.dataframe)
        assert splits.train["timestamp_utc"].max() < splits.val["timestamp_utc"].min()
        assert splits.val["timestamp_utc"].max() < splits.test["timestamp_utc"].min()

    def test_rejects_unsorted_naive_bad_ratios_and_missing_columns(self) -> None:
        ds = generate_benchmark_dataset(seed=42, days=1, freq_minutes=5)
        df = ds.dataframe

        # Unsorted timestamps
        shuffled = df.sample(frac=1.0, random_state=7).reset_index(drop=True)
        with pytest.raises(ValueError, match="ascending chronological"):
            split_chronological(shuffled)

        # Naive timestamps
        naive_df = df.copy()
        naive_df["timestamp_utc"] = naive_df["timestamp_utc"].dt.tz_localize(None)
        with pytest.raises(ValueError, match="timezone-aware"):
            split_chronological(naive_df)

        # Bad ratios
        for tr, vr in [(0.0, 0.2), (0.6, 0.0), (0.7, 0.3), (0.8, 0.3)]:
            with pytest.raises(ValueError, match="Invalid split ratios"):
                split_chronological(df, train_ratio=tr, val_ratio=vr)

        # Missing required feature column
        with pytest.raises(ValueError, match="Missing required column"):
            split_chronological(df.drop(columns=["queue_depth"]))


class TestMultivariatePreprocessorAndWindowing:
    def test_zero_data_leakage_from_val_and_test(self) -> None:
        ds = generate_benchmark_dataset(seed=42, days=2, freq_minutes=1)
        splits = split_chronological(ds.dataframe)

        prep_clean = MultivariatePreprocessor(window_size=30).fit(splits.train)

        # Corrupt val and test with extreme outliers; refit on train only must match bit-for-bit
        val_mutated = splits.val.copy()
        test_mutated = splits.test.copy()
        val_mutated["cpu_utilization"] = 999_999.0
        test_mutated["request_latency_ms"] = -500_000.0

        prep_after = MultivariatePreprocessor(window_size=30).fit(splits.train)
        np.testing.assert_array_equal(prep_clean.mean_, prep_after.mean_)
        np.testing.assert_array_equal(prep_clean.scale_, prep_after.scale_)

        # Transforming mutated val/test must reflect the huge scaled values without changing state
        scaled_val = prep_after.transform(val_mutated)
        assert np.abs(scaled_val[:, 0]).mean() > 1000.0
        np.testing.assert_array_equal(prep_clean.mean_, prep_after.mean_)
        np.testing.assert_array_equal(prep_clean.scale_, prep_after.scale_)

    def test_windowing_shape_and_causal_end_label_alignment(self) -> None:
        n_samples = 50
        n_features = len(FEATURE_COLUMNS)
        values = np.arange(n_samples * n_features, dtype=np.float64).reshape(n_samples, n_features)
        labels = np.zeros(n_samples, dtype=np.int64)
        labels[29] = 1
        labels[35] = 1

        x_win, y_win = build_windows(values, labels=labels, window_size=10, stride=1)
        assert x_win.shape == (41, 10, n_features)
        assert y_win is not None
        assert y_win.shape == (41,)

        # Window i covers indices [i .. i + 9] and its label is strictly labels[i + 9]
        for i in range(len(x_win)):
            np.testing.assert_array_equal(x_win[i], values[i : i + 10])
            assert y_win[i] == labels[i + 9]

        # Stride > 1 alignment
        x_s3, y_s3 = build_windows(values, labels=labels, window_size=10, stride=3)
        expected_ends = np.arange(9, n_samples, 3)
        assert x_s3.shape == (len(expected_ends), 10, n_features)
        assert y_s3 is not None
        np.testing.assert_array_equal(y_s3, labels[expected_ends])

        # Labels=None works cleanly
        x_only, y_none = build_windows(values, labels=None, window_size=10, stride=1)
        assert x_only.shape == (41, 10, n_features)
        assert y_none is None

    def test_windowing_and_preprocessor_error_handling(self) -> None:
        ds = generate_benchmark_dataset(seed=42, days=1, freq_minutes=5)
        splits = split_chronological(ds.dataframe)

        # Unfitted transform/save/mean_/scale_ raise RuntimeError
        unfitted = MultivariatePreprocessor()
        with pytest.raises(RuntimeError):
            unfitted.transform(splits.train)
        with pytest.raises(RuntimeError):
            _ = unfitted.mean_

        # NaN and inf rejected on fit and transform
        fitted = MultivariatePreprocessor().fit(splits.train)
        nan_df = splits.val.copy()
        nan_df.loc[0, "cpu_utilization"] = np.nan
        with pytest.raises(ValueError, match="NaN or infinite"):
            fitted.transform(nan_df)

        inf_df = splits.val.copy()
        inf_df.loc[0, "error_rate"] = np.inf
        with pytest.raises(ValueError, match="NaN or infinite"):
            fitted.transform(inf_df)

        # Missing feature column rejected
        with pytest.raises(ValueError, match="Missing required feature"):
            fitted.transform(splits.val.drop(columns=["cpu_utilization"]))

        # Invalid window sizes rejected
        arr = np.zeros((5, 7), dtype=np.float64)
        with pytest.raises(ValueError):
            build_windows(arr, window_size=0)
        with pytest.raises(ValueError):
            build_windows(arr, window_size=6)

    def test_json_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        ds = generate_benchmark_dataset(seed=42, days=2, freq_minutes=2)
        splits = split_chronological(ds.dataframe)

        prep = MultivariatePreprocessor(window_size=15).fit(splits.train)
        expected = prep.transform(splits.test)

        state_file = tmp_path / "preprocessor.json"
        prep.save(state_file)

        # Verify pure valid JSON serialization
        raw_state = json.loads(state_file.read_text(encoding="utf-8"))
        assert raw_state["feature_columns"] == list(FEATURE_COLUMNS)
        assert raw_state["window_size"] == 15

        loaded = MultivariatePreprocessor.load(state_file)
        actual = loaded.transform(splits.test)
        assert loaded.window_size == 15
        np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-12)
        np.testing.assert_array_equal(loaded.mean_, prep.mean_)
        np.testing.assert_array_equal(loaded.scale_, prep.scale_)


def test_anomaly_detection_has_no_framework_or_db_imports() -> None:
    forbidden = {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette", "pickle", "joblib"}
    pkg_dir = REPO_ROOT / "services" / "anomaly_detection"
    py_files = sorted(pkg_dir.rglob("*.py"))
    assert len(py_files) >= 3

    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        assert imported_roots.isdisjoint(forbidden), (
            f"{path.relative_to(REPO_ROOT)} imported forbidden: {imported_roots & forbidden}"
        )
