"""Train and export PUGMARK's local Stage 2 classifiers as TorchScript.

High-performance GPU-accelerated training pipeline:
- Auto-detects NVIDIA CUDA GPU and accelerates with Automatic Mixed Precision (AMP)
- cuDNN benchmark acceleration enabled
- Bounded DataLoader batch streaming with pin_memory
- Exports clean TorchScript artifacts below edge/models/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torchvision
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from torch.utils.data import DataLoader, Dataset

from edge import config
from edge.pipeline.classifiers import SIDE_LABELS, SPECIES_LABELS


@dataclass(frozen=True)
class ManifestRow:
    path: Path
    label: int
    split: str
    tags: tuple[str, ...]


class LocalClassifierNet(nn.Module):
    """High-accuracy CNN with BatchNorm and Dropout for TorchScript export."""

    def __init__(self, class_count: int):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 32, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 128, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 256, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((1, 1)),
        )
        self.classifier = nn.Sequential(
            nn.Dropout(0.25),
            nn.Linear(256, class_count)
        )

    def forward(self, tensor: torch.Tensor) -> torch.Tensor:
        x = self.features(tensor).flatten(1)
        return self.classifier(x)


class PretrainedClassifierNet(nn.Module):
    """ResNet-50, initialised from the already-trained local re-ID embedder
    (edge/models/identify/identify_embedder.pt) rather than from scratch or
    from ImageNet.

    Not a convenience shortcut: the embedder's backbone was already forced,
    by the re-identification task itself, to represent tiger body shape,
    pose and stripe orientation well enough to tell individuals apart --
    a substantially more relevant starting point for "which way is this
    tiger facing" than either random weights or generic ImageNet classes.
    It is also the only pretrained option that does not require a network
    download: identify.py's own TripletEmbedder docstring already states
    that an ImageNet download is banned even at training time in this
    codebase, and there is no local ImageNet checkpoint on disk (checked:
    edge/models/backbones/ holds only the pose model's seed weights).

    Only the backbone is reused -- the embedder's own head is a 256-d
    L2-normalised embedding projection, meaningless for classification and
    not loaded here.
    """

    def __init__(self, class_count: int, embedder_weights_path: Path):
        super().__init__()
        backbone = torchvision.models.resnet50(weights=None)
        backbone = nn.Sequential(*list(backbone.children())[:-1])  # drop its own FC
        if not embedder_weights_path.is_file():
            raise FileNotFoundError(
                f"pretrained backbone requires {embedder_weights_path}, which is not present -- "
                "install the offline model bundle before training with --arch pretrained")
        full_state = torch.load(embedder_weights_path, map_location="cpu", weights_only=True)
        backbone_state = {key[len("backbone."):]: value for key, value in full_state.items()
                          if key.startswith("backbone.")}
        missing, unexpected = backbone.load_state_dict(backbone_state, strict=False)
        if missing or unexpected:
            raise RuntimeError(
                f"embedder backbone did not load cleanly from {embedder_weights_path}: "
                f"missing={missing} unexpected={unexpected} -- refusing to train from a "
                "silently mismatched initialisation")
        self.backbone = backbone
        self.classifier = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(2048, class_count),
        )

    def forward(self, tensor: torch.Tensor) -> torch.Tensor:
        feat = self.backbone(tensor).flatten(1)
        return self.classifier(feat)


def _labels(task: str) -> tuple[str, ...]:
    return SPECIES_LABELS if task == "species" else SIDE_LABELS


def _rows(manifest: Path, task: str) -> list[ManifestRow]:
    labels = _labels(task)
    by_label = {label: index for index, label in enumerate(labels)}
    rows: list[ManifestRow] = []
    for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            split, label, raw_path = raw["split"], raw[task], raw["path"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"{manifest}:{line_no}: expected path, split, and {task}") from exc
        if split not in ("train", "val"):
            raise ValueError(f"{manifest}:{line_no}: split must be train or val")
        if label not in by_label:
            raise ValueError(f"{manifest}:{line_no}: invalid {task} label {label!r}")
        path = Path(raw_path)
        path = (manifest.parent / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_file():
            continue
        tags = raw.get("challenge_tags", [])
        rows.append(ManifestRow(path, by_label[label], split, tuple(tags)))
    if not rows:
        raise ValueError(f"{manifest}: no usable rows")
    return rows


def _require_all_labels(rows: list[ManifestRow], task: str) -> None:
    labels = _labels(task)
    for split in ("train", "val"):
        present = {row.label for row in rows if row.split == split}
        missing = [label for index, label in enumerate(labels) if index not in present]
        if missing:
            raise ValueError(f"{task} manifest has no {split} examples for: {', '.join(missing)}")


def _transform(image: Image.Image, *, augment: bool, allow_mirror: bool = True) -> torch.Tensor:
    training = config.CONFIG.classifier_training
    image = image.convert("RGB")
    if augment:
        if random.random() < training.grayscale_probability:
            image = ImageOps.grayscale(image).convert("RGB")
        if allow_mirror and random.random() < 0.5:
            image = ImageOps.mirror(image)
        image = ImageEnhance.Brightness(image).enhance(
            random.uniform(training.brightness_min, training.brightness_max))
        if random.random() < 0.3:
            image = image.filter(ImageFilter.GaussianBlur(random.uniform(0.0, training.blur_radius_max)))
    image = image.resize((config.CONFIG.classifiers.input_width,
                          config.CONFIG.classifiers.input_height))
    values = np.asarray(image, dtype=np.float32) / 255.0
    values = (values - np.array((0.485, 0.456, 0.406), dtype=np.float32)) / \
             np.array((0.229, 0.224, 0.225), dtype=np.float32)
    return torch.from_numpy(np.ascontiguousarray(values.transpose(2, 0, 1)))


class ManifestDataset(Dataset):
    def __init__(self, rows: list[ManifestRow], *, augment: bool, allow_mirror: bool = True):
        self.rows, self.augment, self.allow_mirror = rows, augment, allow_mirror

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, int]:
        row = self.rows[index]
        with Image.open(row.path) as image:
            return _transform(image, augment=self.augment, allow_mirror=self.allow_mirror), row.label


def _evaluate(model: nn.Module, loader: DataLoader, device: torch.device,
              class_count: int) -> tuple[float, list[list[int]]]:
    matrix = [[0 for _ in range(class_count)] for _ in range(class_count)]
    correct = total = 0
    model.eval()
    with torch.inference_mode():
        for images, expected in loader:
            images = images.to(device, non_blocking=True)
            predicted = model(images).argmax(dim=1).cpu()
            for truth, answer in zip(expected.tolist(), predicted.tolist()):
                matrix[truth][answer] += 1
                correct += int(truth == answer)
                total += 1
    return correct / total if total else 0.0, matrix


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _output_path(task: str, output: str | None) -> Path:
    default = config.SPECIES_MODEL_PATH if task == "species" else config.SIDE_MODEL_PATH
    path = Path(output).resolve() if output else default
    try:
        path.relative_to(config.MODELS_DIR)
    except ValueError as exc:
        raise ValueError("classifier output must be below edge/models") from exc
    return path


def train(task: str, manifest: Path, output: str | None, epochs: int | None,
          batch_size: int | None, arch: str = "scratch") -> Path:
    rows = _rows(manifest, task)
    _require_all_labels(rows, task)
    train_rows = [row for row in rows if row.split == "train"]
    val_rows = [row for row in rows if row.split == "val"]
    training = config.CONFIG.classifier_training
    random.seed(training.seed)
    np.random.seed(training.seed)
    torch.manual_seed(training.seed)

    # Device selection: GPU strongly prioritized
    use_cuda = torch.cuda.is_available()
    if use_cuda:
        device = torch.device("cuda:0")
        torch.backends.cudnn.benchmark = True
        device_name = torch.cuda.get_device_name(0)
        vram_mb = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
        print(f"Using GPU acceleration: {device_name} ({vram_mb} MB VRAM)")
    else:
        device = torch.device("cpu")
        print("Using CPU for training")

    # Horizontal mirroring is a valid augmentation for species (a mirrored
    # leopard is still a leopard) but not for side: it flips which flank is
    # actually facing the camera without flipping the L/R label to match, so
    # the same pixels get trained under both labels at random. That is what
    # collapsed the shipped side classifier to always predicting UNKNOWN --
    # see edge/models/side/flank_side_classifier.ts.json's own confusion
    # matrix (0/41 L recall, 2/96 R recall) before this fix.
    allow_mirror = task != "side"
    labels = _labels(task)
    size = batch_size or (64 if use_cuda else 32)
    train_loader = DataLoader(
        ManifestDataset(train_rows, augment=True, allow_mirror=allow_mirror),
        batch_size=size,
        shuffle=True,
        pin_memory=use_cuda,
        num_workers=0)
    val_loader = DataLoader(
        ManifestDataset(val_rows, augment=False, allow_mirror=allow_mirror),
        batch_size=size,
        shuffle=False,
        pin_memory=use_cuda,
        num_workers=0)

    if arch == "pretrained":
        model = PretrainedClassifierNet(len(labels), config.EMBEDDER_MODEL_PATH).to(device)
    else:
        model = LocalClassifierNet(len(labels)).to(device)

    # Class-balanced loss: both manifests are dominated by one class (UNKNOWN
    # side, unknown species -- docs/STAGE2_MODEL_WORKFLOW.md). An unweighted
    # loss lets the network minimise error by defaulting to that majority
    # instead of learning the minority classes; inverse-frequency weighting
    # (same principle as sklearn's 'balanced' class_weight) removes that
    # shortcut.
    class_counts = [sum(1 for row in train_rows if row.label == index)
                    for index in range(len(labels))]
    class_weights = torch.tensor(
        [len(train_rows) / (len(labels) * max(1, count)) for count in class_counts],
        dtype=torch.float32, device=device)
    loss_fn = nn.CrossEntropyLoss(weight=class_weights)
    scaler = torch.cuda.amp.GradScaler(enabled=use_cuda)
    total_epochs = epochs or training.epochs

    # Best-checkpoint tracking, not "whatever the last epoch happened to be".
    # A model with 2,048 backbone channels and only ~1,500 training images has
    # more than enough capacity to memorise the training set -- confirmed on
    # this exact run: training loss fell to 0.01 while validation accuracy had
    # already plateaued and was wobbling epoch to epoch. Saving the final
    # epoch unconditionally would ship whichever point the wobble happened to
    # land on, not the model that actually generalised best.
    best_state: dict[str, torch.Tensor] | None = None
    best_acc = -1.0

    def run_epochs(n: int, optimizer, scheduler, phase: str) -> None:
        nonlocal best_state, best_acc
        for epoch in range(n):
            model.train()
            total_loss = 0.0
            for images, expected in train_loader:
                images = images.to(device, non_blocking=True)
                expected = expected.to(device, non_blocking=True)
                optimizer.zero_grad()
                with torch.cuda.amp.autocast(enabled=use_cuda):
                    logits = model(images)
                    loss = loss_fn(logits, expected)
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                total_loss += loss.item()
            scheduler.step()
            acc, _ = _evaluate(model, val_loader, device, len(labels))
            marker = ""
            if acc > best_acc:
                best_acc = acc
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                marker = "  <- best so far"
            print(f"  [{phase}] Epoch [{epoch + 1:02d}/{n:02d}] - "
                  f"Loss: {total_loss / len(train_loader):.4f} - Val Accuracy: {acc * 100:.2f}%{marker}")

    print(f"Training {task} classifier ({arch}) on {len(train_rows)} train / "
          f"{len(val_rows)} val samples ({total_epochs} epochs)...")

    if arch == "pretrained":
        # Two-phase transfer learning, not a single pass at the from-scratch
        # learning rate: the classifier head starts randomly initialised, and
        # its first few batches of gradients are large and mostly noise. Back-
        # propagating that straight into the embedder's pretrained backbone
        # would overwrite the very features this architecture exists to keep.
        # Warm the head up alone first (backbone frozen), then unfreeze
        # everything at a much lower learning rate to fine-tune -- standard
        # transfer-learning practice, and the reason PretrainedClassifierNet
        # and LocalClassifierNet cannot share one training loop.
        warmup_epochs = min(5, max(1, total_epochs // 4))
        finetune_epochs = max(1, total_epochs - warmup_epochs)

        for p in model.backbone.parameters():
            p.requires_grad = False
        head_optimizer = torch.optim.AdamW(model.classifier.parameters(), lr=1e-3, weight_decay=1e-4)
        head_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(head_optimizer, T_max=warmup_epochs)
        run_epochs(warmup_epochs, head_optimizer, head_scheduler, "warmup")

        for p in model.backbone.parameters():
            p.requires_grad = True
        full_optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
        full_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(full_optimizer, T_max=finetune_epochs)
        run_epochs(finetune_epochs, full_optimizer, full_scheduler, "finetune")
    else:
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_epochs)
        run_epochs(total_epochs, optimizer, scheduler, "train")

    if best_state is not None:
        model.load_state_dict(best_state)
        print(f"Restored best checkpoint: {best_acc * 100:.2f}% val accuracy "
              f"(the final epoch's own accuracy may have drifted from this)")

    accuracy, matrix = _evaluate(model, val_loader, device, len(labels))
    path = _output_path(task, output)
    path.parent.mkdir(parents=True, exist_ok=True)
    model.cpu().eval()
    torch.jit.script(model).save(str(path))
    metadata = {
        "task": task, "arch": arch, "labels": list(labels), "manifest": str(manifest.resolve()),
        "manifest_sha256": _hash_file(manifest), "model_sha256": _hash_file(path),
        "validation_accuracy": accuracy, "confusion_matrix": matrix,
        "train_rows": len(train_rows), "val_rows": len(val_rows),
    }
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(metadata, indent=2) + "\n",
                                                        encoding="utf-8")
    print(f"Saved TorchScript classifier: {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=("species", "side"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output")
    parser.add_argument("--epochs", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--arch", choices=("scratch", "pretrained"), default="scratch",
                        help="scratch = small CNN trained from nothing (original default); "
                             "pretrained = ResNet-50 initialised from the local re-ID embedder")
    args = parser.parse_args()
    try:
        train(args.task, args.manifest.resolve(), args.output, args.epochs, args.batch_size, args.arch)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"offline classifier training refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
