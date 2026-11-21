# Multivariate Anomaly Detection Benchmark (Synthetic, `seed=42`)

> **Data provenance notice:** All results below were measured on SignalForge's explicitly labeled **synthetic** 7-day multivariate benchmark (`generate_benchmark_dataset(seed=42, days=7, freq_minutes=1)`). They are reproducible baseline measurements for comparing detector behavior under controlled fault regimes, not production SLA claims.

## 1. Experimental Setup

- **Source artifact:** [results.json](../../experiments/benchmarks/anomaly_seed42/results.json)
- **Reproduction command:**
  ```powershell
  .venv\Scripts\python scripts/run_anomaly_benchmark.py
  ```
- **PyTorch CPU-only install (avoids 2.5 GB CUDA wheel on local laptop):**
  ```powershell
  uv pip install --python .venv\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu "torch>=2.3,<3.0"
  uv pip install --python .venv\Scripts\python.exe --no-deps -e ".[dev]"
  ```
- **Dataset & Leakage-Free Protocol:**
  - 7 features (`cpu_utilization`, `memory_utilization`, `request_latency_ms`, `request_throughput`, `error_rate`, `db_connections`, `queue_depth`) sampled every 1 minute (`10,080` rows total).
  - **Train split (60%, `6,048` rows):** 100% normal baseline (`0` faults). `MultivariatePreprocessor` (`StandardScaler`) and all three detectors fit strictly on `train` (LSTM Autoencoder uses an internal chronological 85% / 15% split of `train` for early stopping).
  - **Validation split (20%, `2,016` rows):** 2 injected fault scenarios (`cpu_saturation`, `network_latency`), used strictly for F1-maximizing threshold calibration (`calibrate_threshold`).
  - **Held-out test split (20%, `2,016` rows, positive rate `0.4395`):** 5 unseen fault scenarios (`db_connection_exhaustion`, `payment_service_failure`, `memory_leak`, `faulty_deployment`, `message_queue_backlog`).
  - Causal context prefixes (`window_size - 1 = 29` trailing scaled rows from the preceding split) are passed to `LSTMAutoencoderDetector` when scoring validation and test splits so every timestep has a causal window with zero future lookahead.
- **Hardware / Runtime:** Windows, Python 3.12.14, `torch==2.15.0+cpu` (forced `device="cpu"`), `scikit-learn==1.9.1`, `numpy==2.5.5`.

## 2. Held-Out Test Split Results (`seed=42`)

| Detector | Precision | Recall | F1 | PR-AUC | Calibrated Threshold | Faults Detected | Mean Delay (min) | FP / Hour | Fit Time (s) | Inference (ms / 1k rows) |
| --- | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| `RollingZScoreDetector` (`window=30`) | 0.4502 | 0.9131 | 0.6031 | 0.5176 | 1.2426 | 5 / 5 | 0.0 | 52.4602 | 0.0007 | 5.3449 |
| `IsolationForestDetector` (`n_estimators=200`) | 0.9986 | 0.8318 | 0.9076 | 0.9948 | 0.0871 | 5 / 5 | 9.8 | 0.0531 | 0.6658 | 37.6457 |
| `LSTMAutoencoderDetector` (`h=32, z=16, e=25`) | 1.0000 | 0.8307 | 0.9075 | 0.9929 | 6.7540 | 5 / 5 | 29.6 | 0.0000 | 149.3922 | 51.5050 |

Confusion matrix counts on the `2,016` held-out test rows (`886` anomaly points, `1,130` normal points = `18.833` normal hours):

| Detector | TP | FP | TN | FN |
| --- | ---: | ---: | ---: | ---: |
| `RollingZScoreDetector` | 809 | 988 | 142 | 77 |
| `IsolationForestDetector` | 737 | 1 | 1,129 | 149 |
| `LSTMAutoencoderDetector` | 736 | 0 | 1,130 | 150 |

*(LSTM Autoencoder training converged cleanly from its initial MSE down to `final_train_mse=0.128280` and `best_internal_val_mse=0.133028` at `best_epoch=25` of `25`; saved weights `weights.pt` are `53,695` bytes.)*

## 3. Per-Fault Detection Delay (Held-Out Test Split, Minutes)

| Held-Out Fault Scenario | Rolling Z-Score | Isolation Forest | LSTM Autoencoder |
| --- | ---: | ---: | ---: |
| `db_connection_exhaustion` | 0.0 | 0.0 | 0.0 |
| `payment_service_failure` | 0.0 | 0.0 | 0.0 |
| `memory_leak` (gradual ramp) | 0.0 | 48.0 | 148.0 |
| `faulty_deployment` | 0.0 | 0.0 | 0.0 |
| `message_queue_backlog` | 0.0 | 1.0 | 0.0 |
| **Mean Delay (detected faults)** | **0.0** | **9.8** | **29.6** |

## 4. Analysis, Trade-Offs & Honest Limitations

1. **Rolling Z-Score (`window=30`) adapts inside long fault plateaus:**
   Although it fits in `< 1 ms` and flags fault onsets immediately (`0.0` min delay), sustained fault plateaus (`120–282` min) pull the trailing 30-minute rolling mean/std upward mid-fault, collapsing mid-plateau Z-scores. Maximizing point-wise validation F1 therefore forces a low threshold (`1.2426`), causing `988` false positives (`52.46 FP/hr`).
2. **Isolation Forest delivers the strongest overall balance on this benchmark:**
   It matches LSTM Autoencoder on F1 (`0.9076` vs `0.9075`), slightly edges it on PR-AUC (`0.9948` vs `0.9929`), trains ~224x faster on CPU (`0.67 s` vs `149.39 s`), detects `memory_leak` 100 minutes earlier (`48.0` min vs `148.0` min), and incurs only `1` false positive across `18.83` normal hours (`0.0531 FP/hr`). Its main operational caveat under our no-`pickle` rule is that tree structures are reconstructed via deterministic refit rather than binary weight loading.
3. **LSTM Autoencoder achieves zero false positives (`1.0000` precision) and `0.0` min delay on all 4 non-ramp faults, but lags on `memory_leak`:**
   Because the validation split contains only 2 high-amplitude faults (`cpu_saturation`, `network_latency`), F1-maximizing validation calibration selected a high mean-MSE threshold (`6.7540`). During the early half of `memory_leak`, only `memory_utilization` drifts gradually while the other 6 features stay near normal, keeping 7-feature mean MSE below `6.7540` until GC thrashing elevates CPU, latency, and errors around minute `148.0`.
4. **Key limitations & next improvements:**
   - Single-seed synthetic benchmark (`seed=42`); multi-seed confidence intervals and real captured OpenTelemetry Demo telemetry evaluation are needed before generalizing conclusions.
   - Validation split has only 2 abrupt fault regimes; including a gradual-drift scenario in validation or aggregating LSTM reconstruction error via `max`/top-k per-feature squared error (rather than unweighted 7-feature mean MSE) would likely improve early detection on single-feature ramps.
   - `LSTMAutoencoderDetector` was still improving at epoch `25` (`best_epoch=25`), so slightly longer training or a cosine/plateau LR schedule may further tighten normal reconstruction error.
