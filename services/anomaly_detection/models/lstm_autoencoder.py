import json
import math
from pathlib import Path

import numpy as np
import torch
from torch import nn

from services.anomaly_detection.baselines.base import (
    BaseAnomalyDetector,
    validate_feature_matrix,
)
from services.anomaly_detection.evaluation.metrics import (
    calibrate_threshold as select_val_threshold,
)
from services.anomaly_detection.preprocessing.pipeline import build_windows


class _LSTMAutoencoderNet(nn.Module):
    """Sequence-to-sequence LSTM Autoencoder for multivariate time-series windows."""

    def __init__(
        self,
        n_features: int,
        hidden_size: int,
        latent_size: int,
        num_layers: int,
    ) -> None:
        super().__init__()
        self.encoder = nn.LSTM(
            input_size=n_features,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
        )
        self.to_latent = nn.Linear(hidden_size, latent_size)
        self.decoder = nn.LSTM(
            input_size=latent_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
        )
        self.output_proj = nn.Linear(hidden_size, n_features)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, (h_n, _) = self.encoder(x)
        latent = self.to_latent(h_n[-1])
        repeated = latent.unsqueeze(1).repeat(1, x.size(1), 1)
        decoded, _ = self.decoder(repeated)
        return self.output_proj(decoded)


class LSTMAutoencoderDetector(BaseAnomalyDetector):
    """PyTorch LSTM Autoencoder anomaly detector operating on causal sliding windows.

    Trains strictly on normal training data (`X_train`) using an internal chronological
    85% / 15% train / early-stopping split (never touching external validation or test
    splits). Produces strictly causal per-timestep reconstruction MSE scores.
    """

    def __init__(
        self,
        window_size: int = 30,
        hidden_size: int = 32,
        latent_size: int = 16,
        num_layers: int = 1,
        epochs: int = 25,
        batch_size: int = 64,
        learning_rate: float = 1e-3,
        patience: int = 5,
        random_state: int = 42,
        device: str = "cpu",
    ) -> None:
        super().__init__()
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if hidden_size < 1:
            raise ValueError("hidden_size must be at least 1")
        if latent_size < 1:
            raise ValueError("latent_size must be at least 1")
        if num_layers < 1:
            raise ValueError("num_layers must be at least 1")
        if epochs < 1:
            raise ValueError("epochs must be at least 1")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if not math.isfinite(learning_rate) or learning_rate <= 0.0:
            raise ValueError("learning_rate must be a finite positive float")
        if patience < 1:
            raise ValueError("patience must be at least 1")

        self.window_size = window_size
        self.hidden_size = hidden_size
        self.latent_size = latent_size
        self.num_layers = num_layers
        self.epochs = epochs
        self.batch_size = batch_size
        self.learning_rate = float(learning_rate)
        self.patience = patience
        self.random_state = random_state
        self.device = device

        self._model: _LSTMAutoencoderNet | None = None
        self.train_losses_: list[float] = []
        self.val_losses_: list[float] = []
        self.best_epoch_: int | None = None

    def _build_training_windows(self, arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if len(arr) < self.window_size:
            raise ValueError(
                f"Training sequence length ({len(arr)}) is smaller than "
                f"window_size ({self.window_size})"
            )

        split_idx = int(len(arr) * 0.85)
        if split_idx >= self.window_size and (len(arr) - split_idx) >= self.window_size:
            train_win, _ = build_windows(arr[:split_idx], window_size=self.window_size, stride=1)
            es_win, _ = build_windows(arr[split_idx:], window_size=self.window_size, stride=1)
            return train_win, es_win

        all_win, _ = build_windows(arr, window_size=self.window_size, stride=1)
        if len(all_win) < 2:
            return all_win, all_win
        win_split = max(1, min(len(all_win) - 1, int(len(all_win) * 0.85)))
        return all_win[:win_split], all_win[win_split:]

    def fit(self, x_train: np.ndarray) -> "LSTMAutoencoderDetector":
        arr = validate_feature_matrix(x_train)
        n_features = arr.shape[1]
        train_windows, es_windows = self._build_training_windows(arr)

        torch.manual_seed(self.random_state)
        device = torch.device(self.device)
        model = _LSTMAutoencoderNet(
            n_features=n_features,
            hidden_size=self.hidden_size,
            latent_size=self.latent_size,
            num_layers=self.num_layers,
        ).to(device)

        optimizer = torch.optim.Adam(model.parameters(), lr=self.learning_rate)
        criterion = nn.MSELoss()

        x_tr_tensor = torch.tensor(train_windows, dtype=torch.float32, device=device)
        x_es_tensor = torch.tensor(es_windows, dtype=torch.float32, device=device)
        n_train = x_tr_tensor.size(0)

        self.train_losses_ = []
        self.val_losses_ = []
        best_val_loss = float("inf")
        best_state: dict[str, torch.Tensor] | None = None
        best_epoch = 1
        epochs_no_improve = 0

        for epoch in range(1, self.epochs + 1):
            model.train()
            gen = torch.Generator()
            gen.manual_seed(self.random_state + epoch)
            perm = torch.randperm(n_train, generator=gen)

            train_loss_sum = 0.0
            for start in range(0, n_train, self.batch_size):
                batch_idx = perm[start : start + self.batch_size]
                batch = x_tr_tensor[batch_idx]
                optimizer.zero_grad()
                recon = model(batch)
                loss = criterion(recon, batch)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
                optimizer.step()
                train_loss_sum += float(loss.item()) * batch.size(0)

            epoch_train_loss = train_loss_sum / n_train
            self.train_losses_.append(epoch_train_loss)

            model.eval()
            with torch.no_grad():
                es_loss_sum = 0.0
                n_es = x_es_tensor.size(0)
                for start in range(0, n_es, max(self.batch_size, 256)):
                    es_batch = x_es_tensor[start : start + max(self.batch_size, 256)]
                    es_recon = model(es_batch)
                    es_loss_sum += float(criterion(es_recon, es_batch).item()) * es_batch.size(0)
                epoch_val_loss = es_loss_sum / n_es
            self.val_losses_.append(epoch_val_loss)

            if epoch_val_loss < best_val_loss - 1e-7:
                best_val_loss = epoch_val_loss
                best_epoch = epoch
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                epochs_no_improve = 0
            else:
                epochs_no_improve += 1
                if epochs_no_improve >= self.patience:
                    break

        if best_state is not None:
            model.load_state_dict(best_state)
        model.eval()

        self._model = model
        self._n_features = n_features
        self._is_fitted = True
        self.best_epoch_ = best_epoch
        return self

    def score(
        self,
        x: np.ndarray,
        context_prefix: np.ndarray | None = None,
    ) -> np.ndarray:
        """Compute strictly causal per-timestep reconstruction MSE (length == len(x)).

        When `context_prefix` (at least `window_size - 1` trailing rows from the preceding
        split) is supplied, it seeds the initial causal windows cleanly. Otherwise `x[0]`
        is repeated `window_size - 1` times on the left so inference remains strictly causal.
        """
        if not self._is_fitted or self._model is None or self._n_features is None:
            raise RuntimeError("LSTMAutoencoderDetector must be fitted before calling score()")

        arr = validate_feature_matrix(x, expected_features=self._n_features)
        pad_len = self.window_size - 1

        if pad_len > 0:
            if context_prefix is not None:
                prefix_arr = validate_feature_matrix(
                    context_prefix, expected_features=self._n_features
                )
                if len(prefix_arr) < pad_len:
                    raise ValueError(
                        f"context_prefix must contain at least {pad_len} rows, "
                        f"got {len(prefix_arr)}"
                    )
                prefix = prefix_arr[-pad_len:]
            else:
                prefix = np.repeat(arr[0:1], pad_len, axis=0)
            padded = np.concatenate([prefix, arr], axis=0)
        else:
            padded = arr

        windows, _ = build_windows(padded, window_size=self.window_size, stride=1)
        device = torch.device(self.device)
        self._model.eval()

        scores = np.empty(len(arr), dtype=np.float64)
        infer_batch = max(self.batch_size, 256)
        with torch.no_grad():
            for start in range(0, len(windows), infer_batch):
                end = min(start + infer_batch, len(windows))
                batch = torch.tensor(windows[start:end], dtype=torch.float32, device=device)
                recon = self._model(batch)
                # Reconstruction MSE of the current (last) timestep given its causal window.
                step_mse = torch.mean((recon[:, -1, :] - batch[:, -1, :]) ** 2, dim=1)
                scores[start:end] = step_mse.detach().cpu().numpy().astype(np.float64)

        return scores

    def calibrate_threshold(
        self,
        x_val: np.ndarray,
        y_val: np.ndarray,
        context_prefix: np.ndarray | None = None,
    ) -> float:
        if not self._is_fitted:
            raise RuntimeError(
                "LSTMAutoencoderDetector must be fitted before calling calibrate_threshold()"
            )
        val_scores = self.score(x_val, context_prefix=context_prefix)
        best = select_val_threshold(val_scores, y_val)
        self._threshold = best
        return best

    def predict(
        self,
        x: np.ndarray,
        threshold: float | None = None,
        context_prefix: np.ndarray | None = None,
    ) -> np.ndarray:
        if not self._is_fitted:
            raise RuntimeError("LSTMAutoencoderDetector must be fitted before calling predict()")
        active_threshold = self._threshold if threshold is None else threshold
        if active_threshold is None:
            raise RuntimeError(
                "LSTMAutoencoderDetector threshold is not set; "
                "call calibrate_threshold() or pass threshold"
            )
        if not math.isfinite(active_threshold):
            raise ValueError("Threshold must be a finite number")
        scores = self.score(x, context_prefix=context_prefix)
        return (scores >= active_threshold).astype(np.int64)

    def save(self, path: Path) -> None:
        """Persist hyperparameters/threshold to config.json and model state_dict to weights.pt."""
        if not self._is_fitted or self._model is None or self._n_features is None:
            raise RuntimeError("LSTMAutoencoderDetector must be fitted before calling save()")

        path.mkdir(parents=True, exist_ok=True)
        config_payload = {
            "detector": "lstm_autoencoder",
            "format_version": 1,
            "window_size": self.window_size,
            "hidden_size": self.hidden_size,
            "latent_size": self.latent_size,
            "num_layers": self.num_layers,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "learning_rate": self.learning_rate,
            "patience": self.patience,
            "random_state": self.random_state,
            "device": self.device,
            "n_features": self._n_features,
            "threshold": self._threshold,
            "best_epoch": self.best_epoch_,
            "train_losses": self.train_losses_,
            "val_losses": self.val_losses_,
        }
        (path / "config.json").write_text(json.dumps(config_payload, indent=2), encoding="utf-8")
        torch.save(self._model.state_dict(), path / "weights.pt")

    @classmethod
    def load(cls, path: Path) -> "LSTMAutoencoderDetector":
        """Load config.json and weights.pt safely using torch.load(..., weights_only=True)."""
        config_path = path / "config.json"
        weights_path = path / "weights.pt"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if payload.get("detector") != "lstm_autoencoder":
            raise ValueError(
                f"Unexpected detector type in {config_path}: {payload.get('detector')!r}"
            )

        n_features = int(payload["n_features"])
        detector = cls(
            window_size=int(payload["window_size"]),
            hidden_size=int(payload["hidden_size"]),
            latent_size=int(payload["latent_size"]),
            num_layers=int(payload["num_layers"]),
            epochs=int(payload["epochs"]),
            batch_size=int(payload["batch_size"]),
            learning_rate=float(payload["learning_rate"]),
            patience=int(payload["patience"]),
            random_state=int(payload["random_state"]),
            device=str(payload.get("device", "cpu")),
        )
        model = _LSTMAutoencoderNet(
            n_features=n_features,
            hidden_size=detector.hidden_size,
            latent_size=detector.latent_size,
            num_layers=detector.num_layers,
        )
        state_dict = torch.load(weights_path, map_location="cpu", weights_only=True)
        model.load_state_dict(state_dict)
        model.to(torch.device(detector.device))
        model.eval()

        detector._model = model
        detector._n_features = n_features
        detector._is_fitted = True
        detector.best_epoch_ = payload.get("best_epoch")
        detector.train_losses_ = list(payload.get("train_losses", []))
        detector.val_losses_ = list(payload.get("val_losses", []))
        if payload.get("threshold") is not None:
            detector.set_threshold(float(payload["threshold"]))
        return detector
