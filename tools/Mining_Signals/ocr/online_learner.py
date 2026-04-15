"""Background online learning for the ONNX digit CNN.

When all three OCR engines (ONNX, Tesseract, PaddleOCR) agree
unanimously on a digit's identity, this module performs a single
gradient step on a live PyTorch copy of the CNN, nudging it toward
the user's specific HUD font rendering.

Every ``EXPORT_INTERVAL`` steps the PyTorch model is re-exported to
ONNX and hot-swapped into the inference session, so the scan pipeline
immediately benefits from the improved weights.

**Graceful degradation**: if PyTorch is not installed (``import torch``
fails), all public methods become no-ops. The reservoir still collects
samples for offline retraining via ``train_model.py``.

Online-learned model is stored at::

    %LOCALAPPDATA%/SC_Toolbox/model_cnn_online.onnx

The shipped model in ``ocr/models/model_cnn.onnx`` is never modified.
Call ``reset_to_pretrained()`` to discard online-learned weights and
revert to the shipped model.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

EXPORT_INTERVAL = 50   # gradient steps between ONNX re-exports
LEARNING_RATE = 0.0001  # intentionally tiny — slow, safe drift

_ONLINE_MODEL_DIR = Path(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
) / "SC_Toolbox"
ONLINE_MODEL_PATH = _ONLINE_MODEL_DIR / "model_cnn_online.onnx"


class OnlineLearner:
    """Queue-based background fine-tuner for the digit CNN."""

    def __init__(self, model_dir: Path):
        """
        Parameters
        ----------
        model_dir : Path
            Directory containing ``model_cnn.onnx`` (the shipped model).
        """
        self._model_dir = model_dir
        self._available = False
        self._model = None
        self._optimizer = None
        self._criterion = None
        self._step_count = 0
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._worker: Optional[threading.Thread] = None
        self._try_init()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def step_count(self) -> int:
        return self._step_count

    # ── public API ───────────────────────────────────────────

    def submit(self, digit_char: str, image_28x28: np.ndarray) -> None:
        """Enqueue a confirmed digit sample for background training.

        Non-blocking. Drops the sample silently if the queue is full
        or PyTorch is unavailable.
        """
        if not self._available:
            return
        if digit_char not in "0123456789":
            return
        try:
            self._queue.put_nowait((digit_char, image_28x28.copy()))
        except queue.Full:
            return
        self._ensure_worker()

    def reset_to_pretrained(self) -> bool:
        """Discard online-learned weights and revert to shipped model.

        Returns True on success.
        """
        # Delete the online-learned file
        try:
            if ONLINE_MODEL_PATH.is_file():
                ONLINE_MODEL_PATH.unlink()
                log.info("online_learner: deleted online model")
        except Exception as exc:
            log.error("online_learner: could not delete online model: %s", exc)
            return False

        # Reload shipped weights into PyTorch model
        if self._available:
            self._reload_shipped_weights()

        # Hot-swap inference to shipped model
        try:
            from . import onnx_hud_reader
            shipped = self._model_dir / "model_cnn.onnx"
            if shipped.is_file():
                onnx_hud_reader.hot_swap_model(str(shipped))
        except Exception as exc:
            log.error("online_learner: hot-swap to shipped failed: %s", exc)
            return False

        self._step_count = 0
        log.info("online_learner: reset to pretrained weights")
        return True

    # ── internals ────────────────────────────────────────────

    def _try_init(self) -> None:
        """Lazy-load PyTorch and build model from shipped ONNX weights."""
        try:
            import torch  # noqa: F401
        except ImportError:
            log.info("online_learner: torch not installed, online learning disabled")
            return

        try:
            from .train_model import build_model, load_pretrained_weights

            model = build_model()

            # Load weights: prefer online-learned model if it exists,
            # otherwise fall back to shipped model.
            source = ONLINE_MODEL_PATH if ONLINE_MODEL_PATH.is_file() else (
                self._model_dir / "model_cnn.onnx"
            )
            if source.is_file():
                self._load_onnx_weights(model, source)

            import torch.optim as optim
            import torch.nn as nn

            self._model = model
            self._optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)
            self._criterion = nn.CrossEntropyLoss()
            self._available = True
            log.info("online_learner: ready (source=%s)", source.name)
        except Exception as exc:
            log.error("online_learner: init failed: %s", exc)

    def _load_onnx_weights(self, model, onnx_path: Path) -> None:
        """Load ONNX weight tensors into PyTorch model by shape-match."""
        import onnx
        from onnx import numpy_helper
        import torch

        onnx_model = onnx.load(str(onnx_path))
        onnx_weights = {
            init.name: numpy_helper.to_array(init)
            for init in onnx_model.graph.initializer
        }

        state = model.state_dict()
        loaded = 0
        used_onnx = set()
        for name, param in state.items():
            for onnx_name, onnx_arr in onnx_weights.items():
                if onnx_name in used_onnx:
                    continue
                if onnx_arr.shape == param.shape:
                    state[name] = torch.from_numpy(onnx_arr.copy())
                    loaded += 1
                    used_onnx.add(onnx_name)
                    break

        if loaded > 0:
            model.load_state_dict(state)
            log.debug("online_learner: loaded %d tensors from %s", loaded, onnx_path.name)

    def _reload_shipped_weights(self) -> None:
        """Reload the shipped ONNX weights into the live PyTorch model."""
        shipped = self._model_dir / "model_cnn.onnx"
        if not shipped.is_file() or self._model is None:
            return
        self._load_onnx_weights(self._model, shipped)
        # Reset optimizer state so momentum from old weights doesn't
        # interfere with fresh learning.
        import torch.optim as optim
        self._optimizer = optim.Adam(self._model.parameters(), lr=LEARNING_RATE)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._train_loop, daemon=True,
            name="online_learner",
        )
        self._worker.start()

    def _train_loop(self) -> None:
        """Drain queue, one gradient step per sample."""
        import torch

        while True:
            try:
                digit_char, img = self._queue.get(timeout=1.0)
            except queue.Empty:
                break  # queue drained, thread exits

            label_idx = int(digit_char)
            x = torch.from_numpy(
                img.astype(np.float32) / 255.0
            ).reshape(1, 1, 28, 28)
            y = torch.tensor([label_idx], dtype=torch.long)

            with self._lock:
                try:
                    self._model.train()
                    self._optimizer.zero_grad()
                    logits = self._model(x)
                    loss = self._criterion(logits, y)
                    loss.backward()
                    self._optimizer.step()
                    self._step_count += 1
                except Exception as exc:
                    log.debug("online_learner: step failed: %s", exc)
                    continue

            if self._step_count % EXPORT_INTERVAL == 0:
                self._export_and_swap()

    def _export_and_swap(self) -> None:
        """Export current PyTorch weights to ONNX and hot-swap."""
        import torch

        _ONLINE_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = ONLINE_MODEL_PATH.with_suffix(".tmp.onnx")

        with self._lock:
            try:
                self._model.eval()
                dummy = torch.randn(1, 1, 28, 28)
                torch.onnx.export(
                    self._model,
                    dummy,
                    str(tmp_path),
                    input_names=["input"],
                    output_names=["logits"],
                    dynamic_axes={
                        "input": {0: "batch"},
                        "logits": {0: "batch"},
                    },
                    opset_version=13,
                )
            except Exception as exc:
                log.error("online_learner: ONNX export failed: %s", exc)
                return

        # Atomic rename
        try:
            tmp_path.replace(ONLINE_MODEL_PATH)
        except Exception as exc:
            log.error("online_learner: rename failed: %s", exc)
            return

        # Hot-swap inference session
        try:
            from . import onnx_hud_reader
            onnx_hud_reader.hot_swap_model(str(ONLINE_MODEL_PATH))
            log.info(
                "online_learner: exported + hot-swapped after %d steps",
                self._step_count,
            )
        except Exception as exc:
            log.error("online_learner: hot-swap failed: %s", exc)
