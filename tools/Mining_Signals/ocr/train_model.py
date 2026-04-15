"""Fine-tune the ONNX digit recognition model on collected training data.

Usage:
    python -m ocr.train_model [--epochs 20] [--lr 0.001]

Reads training images from training_data/{0-9}/*.png (28×28 grayscale),
splits into train/val, fine-tunes the existing CNN, and exports a new
ONNX model to ocr/models/model_cnn_finetuned.onnx.

The original model is preserved — swap in the fine-tuned one by
renaming it to model_cnn.onnx when ready.

Requirements: pip install torch onnx (one-time, for training only)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_MODULE_DIR = Path(__file__).parent
_TRAINING_DIR = _MODULE_DIR.parent / "training_data"
_MODEL_DIR = _MODULE_DIR / "models"
_ORIGINAL_MODEL = _MODEL_DIR / "model_cnn.onnx"
_FINETUNED_MODEL = _MODEL_DIR / "model_cnn_finetuned.onnx"
_META_PATH = _MODEL_DIR / "model_cnn.json"

CHAR_CLASSES = "0123456789.-%"
NUM_CLASSES = len(CHAR_CLASSES)


def load_training_data() -> tuple[np.ndarray, np.ndarray]:
    """Load all training images and labels from the training_data directory.

    Returns (images, labels) as numpy arrays:
    - images: (N, 1, 28, 28) float32, normalized [0, 1]
    - labels: (N,) int64, digit index in CHAR_CLASSES
    """
    images: list[np.ndarray] = []
    labels: list[int] = []

    for digit_char in "0123456789":
        digit_dir = _TRAINING_DIR / digit_char
        if not digit_dir.is_dir():
            continue
        class_idx = CHAR_CLASSES.index(digit_char)

        for img_file in sorted(digit_dir.glob("*.png")):
            try:
                from PIL import Image
                img = Image.open(img_file).convert("L").resize((28, 28))
                arr = np.array(img, dtype=np.float32) / 255.0
                images.append(arr.reshape(1, 28, 28))
                labels.append(class_idx)
            except Exception as exc:
                log.warning("Skipping %s: %s", img_file, exc)

    if not images:
        return np.array([]), np.array([])

    return np.array(images, dtype=np.float32), np.array(labels, dtype=np.int64)


def print_dataset_stats(labels: np.ndarray) -> None:
    """Print per-class sample counts."""
    print("\nDataset statistics:")
    for i, ch in enumerate(CHAR_CLASSES):
        count = int(np.sum(labels == i))
        if count > 0:
            bar = "#" * min(count // 2, 40)
            print(f"  '{ch}': {count:5d} {bar}")
    print(f"  Total: {len(labels)} samples\n")


def build_model():
    """Build a small CNN matching the ONNX model architecture."""
    import torch
    import torch.nn as nn

    class DigitCNN(nn.Module):
        def __init__(self, num_classes: int = NUM_CLASSES):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 32, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(64, 64, 3, padding=1),
                nn.ReLU(),
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Linear(64 * 7 * 7, 128),
                nn.ReLU(),
                nn.Dropout(0.3),
                nn.Linear(128, num_classes),
            )

        def forward(self, x):
            x = self.features(x)
            x = self.classifier(x)
            return x

    return DigitCNN()


def load_pretrained_weights(model):
    """Try to load weights from the existing ONNX model into the PyTorch model.

    This is best-effort — if the architectures don't match exactly,
    we start from scratch (which is fine with enough training data).
    """
    try:
        import onnx
        from onnx import numpy_helper

        onnx_model = onnx.load(str(_ORIGINAL_MODEL))
        onnx_weights = {
            init.name: numpy_helper.to_array(init)
            for init in onnx_model.graph.initializer
        }

        state = model.state_dict()
        loaded = 0
        for name, param in state.items():
            # Try to match ONNX weight names to PyTorch names
            for onnx_name, onnx_arr in onnx_weights.items():
                if onnx_arr.shape == param.shape:
                    import torch
                    state[name] = torch.from_numpy(onnx_arr.copy())
                    loaded += 1
                    del onnx_weights[onnx_name]
                    break

        if loaded > 0:
            model.load_state_dict(state)
            print(f"Loaded {loaded} weight tensors from existing ONNX model")
        else:
            print("Could not match ONNX weights — training from scratch")
    except Exception as exc:
        print(f"Could not load pretrained weights: {exc} — training from scratch")


def train(epochs: int = 20, lr: float = 0.001, val_split: float = 0.15):
    """Main training loop."""
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import TensorDataset, DataLoader

    print("Loading training data...")
    images, labels = load_training_data()

    if len(images) == 0:
        print("No training data found in training_data/")
        print("Run the scanner for a while to collect labeled samples.")
        return

    print_dataset_stats(labels)

    if len(images) < 50:
        print(f"Only {len(images)} samples — need at least 50 for meaningful training.")
        print("Keep scanning to collect more data.")
        return

    # Shuffle and split
    indices = list(range(len(images)))
    random.shuffle(indices)
    split = int(len(indices) * (1 - val_split))
    train_idx, val_idx = indices[:split], indices[split:]

    X_train = torch.from_numpy(images[train_idx])
    y_train = torch.from_numpy(labels[train_idx])
    X_val = torch.from_numpy(images[val_idx])
    y_val = torch.from_numpy(labels[val_idx])

    train_ds = TensorDataset(X_train, y_train)
    val_ds = TensorDataset(X_val, y_val)
    train_loader = DataLoader(train_ds, batch_size=32, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=64)

    # Build model and try loading pretrained weights
    model = build_model()
    load_pretrained_weights(model)

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.5)

    print(f"Training for {epochs} epochs (lr={lr}, train={len(train_idx)}, val={len(val_idx)})")
    print("-" * 50)

    best_val_acc = 0.0
    best_state = None

    for epoch in range(epochs):
        # Train
        model.train()
        train_loss = 0.0
        train_correct = 0
        train_total = 0
        for X_batch, y_batch in train_loader:
            optimizer.zero_grad()
            out = model(X_batch)
            loss = criterion(out, y_batch)
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * len(X_batch)
            train_correct += (out.argmax(1) == y_batch).sum().item()
            train_total += len(X_batch)

        # Validate
        model.eval()
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for X_batch, y_batch in val_loader:
                out = model(X_batch)
                val_correct += (out.argmax(1) == y_batch).sum().item()
                val_total += len(X_batch)

        train_acc = train_correct / train_total * 100
        val_acc = val_correct / val_total * 100 if val_total > 0 else 0
        avg_loss = train_loss / train_total

        print(f"  Epoch {epoch+1:3d}: loss={avg_loss:.4f}  train_acc={train_acc:.1f}%  val_acc={val_acc:.1f}%")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        scheduler.step()

    print("-" * 50)
    print(f"Best validation accuracy: {best_val_acc:.1f}%")

    if best_state is None:
        print("No improvement — not saving.")
        return

    # Load best weights and export
    model.load_state_dict(best_state)
    model.eval()

    # Export to ONNX
    print(f"\nExporting to {_FINETUNED_MODEL}...")
    dummy = torch.randn(1, 1, 28, 28)
    torch.onnx.export(
        model, dummy,
        str(_FINETUNED_MODEL),
        input_names=["input"],
        output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=13,
    )

    # Save metadata
    meta = {
        "charClasses": CHAR_CLASSES,
        "numClasses": NUM_CLASSES,
        "inputShape": [1, 1, 28, 28],
        "valAccuracy": best_val_acc / 100.0,
        "trainSamples": len(train_idx),
        "valSamples": len(val_idx),
        "fineTuned": True,
    }
    meta_path = _MODEL_DIR / "model_cnn_finetuned.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Saved fine-tuned model to {_FINETUNED_MODEL}")
    print(f"Metadata saved to {meta_path}")
    print(f"\nTo use the fine-tuned model:")
    print(f"  1. Back up: models/model_cnn.onnx → models/model_cnn_original.onnx")
    print(f"  2. Rename: models/model_cnn_finetuned.onnx → models/model_cnn.onnx")
    print(f"  3. Restart the tool")


def main():
    parser = argparse.ArgumentParser(description="Fine-tune the ONNX digit model")
    parser.add_argument("--epochs", type=int, default=20, help="Training epochs (default: 20)")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001)")
    parser.add_argument("--stats", action="store_true", help="Just show dataset stats, don't train")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.stats:
        images, labels = load_training_data()
        if len(images) == 0:
            print("No training data yet. Run the scanner to collect samples.")
        else:
            print_dataset_stats(labels)
        return

    train(epochs=args.epochs, lr=args.lr)


if __name__ == "__main__":
    main()
