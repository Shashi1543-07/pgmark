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
import os
import random
import time
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

    def __init__(self, class_count: int, dropout: float | None = None):
        super().__init__()
        dropout = config.CONFIG.classifier_training.dropout_scratch if dropout is None else dropout
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
            nn.Dropout(dropout),
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

    def __init__(self, class_count: int, embedder_weights_path: Path, dropout: float | None = None):
        super().__init__()
        dropout = config.CONFIG.classifier_training.dropout_pretrained if dropout is None else dropout
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
            nn.Dropout(dropout),
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
        if random.random() < training.crop_probability:
            # Crop a random sub-region rather than always feeding the model
            # the exact same pixel grid for a given source image every
            # epoch -- with ~1,500 training images and a 2,048-channel
            # backbone, that exact-pixel repetition is what memorisation
            # latches onto (measured: 99.9% train accuracy vs 67.1% val on
            # the pre-crop-augmentation side classifier).
            width, height = image.size
            scale = random.uniform(training.crop_min_scale, 1.0)
            crop_w, crop_h = max(1, int(width * scale)), max(1, int(height * scale))
            left = random.randint(0, width - crop_w)
            top = random.randint(0, height - crop_h)
            image = image.crop((left, top, left + crop_w, top + crop_h))
        image = ImageEnhance.Brightness(image).enhance(
            random.uniform(training.brightness_min, training.brightness_max))
        image = ImageEnhance.Color(image).enhance(
            random.uniform(training.saturation_min, training.saturation_max))
        image = ImageEnhance.Contrast(image).enhance(
            random.uniform(training.contrast_min, training.contrast_max))
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


def _build_optimizer(params, optimizer_name: str, phase: str, weight_decay: float):
    """phase is 'head'/'scratch' (randomly-initialised weights, needs a
    larger step) or 'finetune' (already-trained weights, needs a smaller
    one) -- the same 10x split the original AdamW-only code used, just
    generalised to also cover SGD, which needs its own, larger magnitude
    since it has no per-parameter adaptive scaling to compensate."""
    training = config.CONFIG.classifier_training
    high_phase = phase in ("head", "scratch")
    if optimizer_name == "sgd":
        lr = training.sgd_head_lr if high_phase else training.sgd_finetune_lr
        return torch.optim.SGD(params, lr=lr, momentum=training.sgd_momentum,
                                weight_decay=weight_decay, nesterov=True)
    lr = 1e-3 if high_phase else 1e-4
    return torch.optim.AdamW(params, lr=lr, weight_decay=weight_decay)


def _checkpoint_path(output_path: Path) -> Path:
    return output_path.with_suffix(".checkpoint.pt")


def _save_checkpoint(ckpt_path: Path, *, phase: str, epoch: int, model, optimizer, scheduler,
                      best_state, best_acc: float, task: str, arch: str, optimizer_name: str,
                      weight_decay: float, dropout_used: float, total_epochs: int,
                      manifest_sha256: str) -> None:
    """Written after every epoch, unconditionally, so a killed process loses
    at most one epoch of work instead of the whole run. best_state above
    only ever lived in RAM -- a crash before this existed meant restarting
    from epoch 0 no matter how far training had actually got, which is a
    real risk for an unattended multi-hour run on a laptop that can sleep,
    lose power or just get bumped.

    A failed save here must never crash a training run that was otherwise
    healthy -- that would turn a safety feature into a new source of the
    exact failure it exists to prevent. Confirmed by testing: on Windows the
    atomic rename below can transiently fail with PermissionError if
    anything else -- antivirus, an indexer, a tool peeking at progress --
    briefly has the destination file open (POSIX rename() would not care,
    but os.replace() maps to MoveFileExW there and Windows enforces sharing
    locks strictly). Retried a few times, then given up on quietly rather
    than raised."""
    tmp = ckpt_path.with_suffix(ckpt_path.suffix + ".tmp")
    try:
        torch.save({
            "phase": phase, "epoch": epoch,
            "model_state": model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "scheduler_state": scheduler.state_dict(),
            "best_state": best_state, "best_acc": best_acc,
            "task": task, "arch": arch, "optimizer_name": optimizer_name,
            "weight_decay": weight_decay, "dropout": dropout_used,
            "total_epochs": total_epochs, "manifest_sha256": manifest_sha256,
        }, tmp)
        for attempt in range(5):
            try:
                tmp.replace(ckpt_path)  # atomic on both POSIX and Windows
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.3)
    except OSError as exc:
        print(f"Warning: could not write checkpoint this epoch ({exc}) -- "
              "training continues, will try again next epoch")
        tmp.unlink(missing_ok=True)


def _load_checkpoint(ckpt_path: Path, *, task: str, arch: str, optimizer_name: str,
                      weight_decay: float, dropout_used: float, total_epochs: int,
                      manifest_sha256: str) -> dict:
    if not ckpt_path.is_file():
        raise FileNotFoundError(
            f"--resume was passed but no checkpoint exists at {ckpt_path} -- "
            "nothing to resume from; omit --resume to start a fresh run")
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    # Refuse a mismatched resume rather than silently continuing training
    # under different settings than the checkpoint was saved with -- e.g.
    # loading AdamW momentum buffers into a freshly-built SGD optimizer
    # would not error, it would just train wrong, quietly.
    expected = {"task": task, "arch": arch, "optimizer_name": optimizer_name,
                "weight_decay": weight_decay, "dropout": dropout_used,
                "total_epochs": total_epochs, "manifest_sha256": manifest_sha256}
    mismatches = [f"{key} (checkpoint={ckpt[key]!r}, this run={want!r})"
                  for key, want in expected.items() if ckpt[key] != want]
    if mismatches:
        raise ValueError(
            f"checkpoint at {ckpt_path} does not match this run's settings, refusing to "
            f"resume from a mismatched configuration: {'; '.join(mismatches)}")
    return ckpt


def train(task: str, manifest: Path, output: str | None, epochs: int | None,
          batch_size: int | None, arch: str = "scratch", optimizer_name: str = "adamw",
          weight_decay: float | None = None, dropout: float | None = None,
          resume: bool = False, max_minutes: float | None = None) -> Path:
    rows = _rows(manifest, task)
    _require_all_labels(rows, task)
    train_rows = [row for row in rows if row.split == "train"]
    val_rows = [row for row in rows if row.split == "val"]
    training = config.CONFIG.classifier_training
    random.seed(training.seed)
    np.random.seed(training.seed)
    torch.manual_seed(training.seed)
    weight_decay = training.weight_decay if weight_decay is None else weight_decay
    total_epochs = epochs or training.epochs
    path = _output_path(task, output)
    ckpt_path = _checkpoint_path(path)
    manifest_sha256 = _hash_file(manifest)
    dropout_used = (training.dropout_pretrained if arch == "pretrained" else training.dropout_scratch) \
        if dropout is None else dropout

    resume_state = None
    if resume:
        resume_state = _load_checkpoint(
            ckpt_path, task=task, arch=arch, optimizer_name=optimizer_name,
            weight_decay=weight_decay, dropout_used=dropout_used, total_epochs=total_epochs,
            manifest_sha256=manifest_sha256)
        print(f"Resuming from checkpoint: phase={resume_state['phase']} "
              f"epoch={resume_state['epoch'] + 1} best_acc={resume_state['best_acc'] * 100:.2f}%")
    elif ckpt_path.is_file():
        print(f"Note: an existing checkpoint at {ckpt_path} will be overwritten "
              f"as this run progresses (pass --resume to continue from it instead)")

    # A wall-clock ceiling, not just an epoch count -- epoch count alone
    # cannot bound how long a run actually takes, and it showed: an
    # unbounded species run on 55,000 images ran past 11 hours with no way
    # to know how much longer it needed. Checked once per epoch (the
    # granularity available -- there is no mid-epoch checkpoint), so this is
    # a ceiling on top of that, not a promise of an exact stop time.
    deadline = time.time() + max_minutes * 60 if max_minutes else None
    if deadline:
        print(f"Time budget: stopping after at most {max_minutes:.0f} minutes "
              f"(exports the best checkpoint seen so far, whichever epoch it was)")

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
    # num_workers=0 loads and augments every image synchronously on the main
    # thread -- invisible at side's ~1,900 images, a real bottleneck at
    # species' ~55,000 (confirmed: an overnight run was still mid-epoch
    # after 11 hours with GPU utilisation reading near 0% between batches,
    # consistent with the CPU-bound PIL pipeline being the actual limiter,
    # not the model or the GPU). __main__ guard already present below, which
    # is what Windows' spawn-based multiprocessing requires for this to be
    # safe.
    # 6 workers per loader (12 total, both persistent) crashed a real species
    # run: "Couldn't open shared file mapping ... error code: 1455" --
    # Windows' commit limit for the page-file-backed shared memory each
    # worker holds open. Confirmed real, not theoretical (this exact crash
    # happened at finetune epoch 2). Pulled back on both axes: fewer workers,
    # and val_loader's are no longer persistent -- validation is one short
    # burst per epoch, not continuous work, so there is no reason for its
    # workers to sit holding memory for the whole epoch in between.
    workers = min(4, os.cpu_count() or 0)
    train_loader = DataLoader(
        ManifestDataset(train_rows, augment=True, allow_mirror=allow_mirror),
        batch_size=size,
        shuffle=True,
        pin_memory=use_cuda,
        num_workers=workers,
        persistent_workers=workers > 0)
    val_loader = DataLoader(
        ManifestDataset(val_rows, augment=False, allow_mirror=allow_mirror),
        batch_size=size,
        shuffle=False,
        pin_memory=use_cuda,
        num_workers=workers,
        persistent_workers=False)

    if arch == "pretrained":
        model = PretrainedClassifierNet(len(labels), config.EMBEDDER_MODEL_PATH, dropout=dropout).to(device)
    else:
        model = LocalClassifierNet(len(labels), dropout=dropout).to(device)

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

    # Best-checkpoint tracking, not "whatever the last epoch happened to be".
    # A model with 2,048 backbone channels and only ~1,500 training images has
    # more than enough capacity to memorise the training set -- confirmed on
    # this exact run: training loss fell to 0.01 while validation accuracy had
    # already plateaued and was wobbling epoch to epoch. Saving the final
    # epoch unconditionally would ship whichever point the wobble happened to
    # land on, not the model that actually generalised best.
    best_state: dict[str, torch.Tensor] | None = None
    best_acc = -1.0
    if resume_state is not None:
        best_state = resume_state["best_state"]
        best_acc = resume_state["best_acc"]

    def run_epochs(n: int, optimizer, scheduler, phase: str, start_epoch: int = 0) -> bool:
        """Returns True if the time budget was hit before this phase's
        epochs finished -- callers use that to skip starting a fresh phase
        with no time left for it, rather than begin work that cannot
        possibly complete in the remaining budget."""
        nonlocal best_state, best_acc
        for epoch in range(start_epoch, n):
            if deadline is not None and time.time() >= deadline:
                print(f"  [{phase}] Time budget reached before epoch {epoch + 1:02d}/{n:02d} -- "
                      "stopping here; exporting the best checkpoint seen so far.")
                return True
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
            _save_checkpoint(
                ckpt_path, phase=phase, epoch=epoch, model=model, optimizer=optimizer,
                scheduler=scheduler, best_state=best_state, best_acc=best_acc,
                task=task, arch=arch, optimizer_name=optimizer_name, weight_decay=weight_decay,
                dropout_used=dropout_used, total_epochs=total_epochs, manifest_sha256=manifest_sha256)
        return False

    print(f"Training {task} classifier ({arch}) on {len(train_rows)} train / "
          f"{len(val_rows)} val samples ({total_epochs} epochs, optimizer={optimizer_name}, "
          f"weight_decay={weight_decay}, dropout={dropout_used})...")

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

        # A checkpoint saved mid-finetune means warmup already ran to
        # completion in a previous process -- re-running it here would
        # overwrite the fine-tuned weights this resume exists to preserve
        # with a fresh, backbone-frozen pass.
        timed_out = False
        if resume_state is None or resume_state["phase"] == "warmup":
            for p in model.backbone.parameters():
                p.requires_grad = False
            head_optimizer = _build_optimizer(model.classifier.parameters(), optimizer_name, "head", weight_decay)
            head_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(head_optimizer, T_max=warmup_epochs)
            start = 0
            if resume_state is not None:
                model.load_state_dict(resume_state["model_state"])
                head_optimizer.load_state_dict(resume_state["optimizer_state"])
                head_scheduler.load_state_dict(resume_state["scheduler_state"])
                start = resume_state["epoch"] + 1
            timed_out = run_epochs(warmup_epochs, head_optimizer, head_scheduler, "warmup", start_epoch=start)

        # Starting finetune with no time left for it would mean unfreezing
        # the whole backbone and constructing a fresh optimiser only to
        # immediately hit the same deadline -- skip straight to export
        # instead, on whatever best_state warmup already found.
        if not timed_out:
            for p in model.backbone.parameters():
                p.requires_grad = True
            full_optimizer = _build_optimizer(model.parameters(), optimizer_name, "finetune", weight_decay)
            full_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(full_optimizer, T_max=finetune_epochs)
            start = 0
            if resume_state is not None and resume_state["phase"] == "finetune":
                model.load_state_dict(resume_state["model_state"])
                full_optimizer.load_state_dict(resume_state["optimizer_state"])
                full_scheduler.load_state_dict(resume_state["scheduler_state"])
                start = resume_state["epoch"] + 1
            timed_out = run_epochs(finetune_epochs, full_optimizer, full_scheduler, "finetune", start_epoch=start)
    else:
        optimizer = _build_optimizer(model.parameters(), optimizer_name, "scratch", weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_epochs)
        start = 0
        if resume_state is not None and resume_state["phase"] == "train":
            model.load_state_dict(resume_state["model_state"])
            optimizer.load_state_dict(resume_state["optimizer_state"])
            scheduler.load_state_dict(resume_state["scheduler_state"])
            start = resume_state["epoch"] + 1
        timed_out = run_epochs(total_epochs, optimizer, scheduler, "train", start_epoch=start)

    if best_state is not None:
        model.load_state_dict(best_state)
        print(f"Restored best checkpoint: {best_acc * 100:.2f}% val accuracy "
              f"(the final epoch's own accuracy may have drifted from this)")

    accuracy, matrix = _evaluate(model, val_loader, device, len(labels))
    path.parent.mkdir(parents=True, exist_ok=True)
    model.cpu().eval()
    torch.jit.script(model).save(str(path))
    metadata = {
        "task": task, "arch": arch, "labels": list(labels), "manifest": str(manifest.resolve()),
        "manifest_sha256": manifest_sha256, "model_sha256": _hash_file(path),
        "validation_accuracy": accuracy, "confusion_matrix": matrix,
        "train_rows": len(train_rows), "val_rows": len(val_rows),
        "optimizer": optimizer_name, "weight_decay": weight_decay, "dropout": dropout_used,
        "epochs": total_epochs, "max_minutes": max_minutes,
        "stopped_early_on_time_budget": timed_out,
    }
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(metadata, indent=2) + "\n",
                                                        encoding="utf-8")
    # Training finished cleanly -- the checkpoint's only job was crash
    # recovery mid-run. Leaving it next to a successfully exported model
    # invites a future --resume into a run that already finished.
    ckpt_path.unlink(missing_ok=True)
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
    parser.add_argument("--optimizer", choices=("adamw", "sgd"), default="adamw",
                        help="adamw = adaptive with decoupled weight decay (original default); "
                             "sgd = classic momentum SGD, which often generalises better than "
                             "Adam-family optimizers when fine-tuning a CNN on a small dataset")
    parser.add_argument("--weight-decay", type=float,
                        help="L2 penalty; defaults to config.classifier_training.weight_decay")
    parser.add_argument("--dropout", type=float,
                        help="classifier-head dropout probability; defaults to the arch's "
                             "config value (dropout_scratch / dropout_pretrained)")
    parser.add_argument("--resume", action="store_true",
                        help="continue from the checkpoint next to --output (saved after every "
                             "epoch automatically) instead of starting fresh; refuses if no "
                             "checkpoint exists or its settings don't match this invocation")
    parser.add_argument("--max-minutes", type=float,
                        help="stop after at most this many minutes (checked once per epoch) and "
                             "export the best checkpoint seen so far, rather than run to --epochs "
                             "regardless of how long that actually takes")
    args = parser.parse_args()
    try:
        train(args.task, args.manifest.resolve(), args.output, args.epochs, args.batch_size,
              args.arch, args.optimizer, args.weight_decay, args.dropout, args.resume,
              args.max_minutes)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"offline classifier training refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
