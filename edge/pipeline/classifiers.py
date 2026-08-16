"""Strict, local-only species and flank-side classification.

The field bundle supplies two TorchScript modules below ``edge/models``:

* ``species/species_classifier.ts`` logits: tiger, leopard, dhole, sambar,
  chital, boar, langur, human, vehicle, unknown.
* ``side/flank_side_classifier.ts`` logits: L, R, UNKNOWN.

Both accept normalized ``N x 3 x H x W`` RGB float32 tensors and return an
``N x C`` logits tensor. An unavailable, malformed, or uncertain model is a
safe ``unknown`` result; no downloader or named model fallback exists here.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import torch

from edge import config
from edge.pipeline.device import get_device_manager

SPECIES_LABELS = (
    "tiger", "leopard", "dhole", "sambar", "chital", "boar", "langur",
    "human", "vehicle", "unknown",
)
SIDE_LABELS = ("L", "R", "UNKNOWN")
_MEAN = np.array((0.485, 0.456, 0.406), dtype=np.float32)
_STD = np.array((0.229, 0.224, 0.225), dtype=np.float32)


class LocalClassifierError(RuntimeError):
    """A local artifact is unavailable or violates the release contract."""


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: float | None
    source: str
    model_version: str | None
    detail: str | None = None


_models: dict[Path, torch.jit.ScriptModule] = {}
_versions: dict[Path, str] = {}


def _model_version(path: Path, kind: str) -> str:
    if path not in _versions:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()[:16]
        _versions[path] = f"{kind}-torchscript@sha256:{digest}"
    return _versions[path]


def _load_local(path: Path) -> torch.jit.ScriptModule:
    path = path.resolve()
    try:
        path.relative_to(config.MODELS_DIR)
    except ValueError as exc:
        raise LocalClassifierError(f"classifier path escapes edge/models: {path}") from exc
    if not path.is_file():
        raise LocalClassifierError(f"local classifier missing: {path}")
    model = _models.get(path)
    if model is None:
        try:
            model = torch.jit.load(str(path), map_location="cpu")
            model.eval()
        except Exception as exc:  # noqa: BLE001 - safely converted below
            raise LocalClassifierError(f"cannot load local classifier {path}: {exc}") from exc
        _models[path] = model
    return model


def reset_local_model_cache() -> None:
    """Test/package helper; production inference deliberately remains warm."""
    _models.clear()
    _versions.clear()


def crop_detection(image: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray | None:
    """Return a bounded pixel crop for normalized detector coordinates."""
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        return None
    x, y, w, h = box
    if not all(np.isfinite((x, y, w, h))) or w <= 0 or h <= 0:
        return None
    height, width = image.shape[:2]
    x0, y0 = max(0, int(np.floor(x * width))), max(0, int(np.floor(y * height)))
    x1, y1 = min(width, int(np.ceil((x + w) * width))), min(height, int(np.ceil((y + h) * height)))
    if x1 <= x0 or y1 <= y0:
        return None
    return image[y0:y1, x0:x1].copy()


def save_raw_crop(image: np.ndarray, box: tuple[float, float, float, float],
                  crop_id: str) -> str | None:
    """Save a plain, un-rectified pixel crop at the detection box and return its path.

    Used whenever a detection is refused before rectify_flank() ever runs
    (species or physical side could not be confirmed) -- flank_crops.path
    would otherwise be NULL and the review queue would have nothing real to
    show a human reviewer for that item. This is deliberately NOT a
    substitute for the geometry-aligned crop rectify_flank() produces once a
    side is known -- it is the simplest thing that shows real pixels, so a
    human can sanity-check the AI's species/side call instead of reviewing
    a decorative placeholder blind.
    """
    crop = crop_detection(image, box)
    if crop is None:
        return None
    config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CROPS_DIR / f"{crop_id}.jpg"
    cv2.imwrite(str(path), crop)
    return str(path)


def _tensor(crop: np.ndarray, cfg: config.Classifiers) -> torch.Tensor:
    if crop is None or crop.size == 0:
        raise LocalClassifierError("detector crop is empty")
    resized = cv2.resize(crop, (cfg.input_width, cfg.input_height), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    normalized = (rgb - _MEAN) / _STD
    return torch.from_numpy(np.ascontiguousarray(normalized.transpose(2, 0, 1))).unsqueeze(0)


def _activate_model(model, device: torch.device) -> None:
    """TorchScript modules can move between CUDA and CPU after an OOM."""
    move = getattr(model, "to", None)
    if callable(move):
        move(device)
    evaluate = getattr(model, "eval", None)
    if callable(evaluate):
        evaluate()


def _predict_many(crops: list[np.ndarray], path: Path, labels: tuple[str, ...], threshold: float,
                  unknown_label: str, kind: str, cfg: config.Classifiers) -> list[Classification]:
    if not crops:
        return []
    try:
        model = _load_local(path)
        version = _model_version(path, kind)

        def predict_batch(batch: list[np.ndarray], device: torch.device) -> list[Classification]:
            _activate_model(model, device)
            tensor = torch.cat([_tensor(crop, cfg) for crop in batch]).to(device)
            with torch.inference_mode():
                logits = model(tensor)
            if not isinstance(logits, torch.Tensor):
                raise LocalClassifierError("TorchScript classifier must return a logits tensor")
            if logits.ndim != 2 or tuple(logits.shape) != (len(batch), len(labels)):
                raise LocalClassifierError(
                    f"classifier returned {tuple(logits.shape)} logits; expected "
                    f"({len(batch)}, {len(labels)})")
            if not torch.isfinite(logits).all():
                raise LocalClassifierError("classifier returned non-finite logits")
            probabilities = torch.softmax(logits.float().cpu(), dim=1)
            out = []
            for row in probabilities:
                index = int(torch.argmax(row).item())
                confidence = float(row[index].item())
                label = labels[index] if confidence >= threshold else unknown_label
                out.append(Classification(label, confidence, "classifier", version))
            return out

        return get_device_manager().run_batches(crops, predict_batch)
    except Exception as exc:  # noqa: BLE001 - every classifier fault fails closed
        return [Classification(unknown_label, None, "unavailable", None, str(exc))
                for _ in crops]


def _predict(crop: np.ndarray, path: Path, labels: tuple[str, ...], threshold: float,
             unknown_label: str, kind: str, cfg: config.Classifiers) -> Classification:
    """Single-crop compatibility wrapper around the real batched path."""
    return _predict_many([crop], path, labels, threshold, unknown_label, kind, cfg)[0]


def classify_species(image: np.ndarray, box: tuple[float, float, float, float],
                     cfg: config.Classifiers | None = None) -> Classification:
    return classify_species_many(image, [box], cfg)[0]


def classify_species_many(image: np.ndarray, boxes: list[tuple[float, float, float, float]],
                          cfg: config.Classifiers | None = None) -> list[Classification]:
    cfg = cfg or config.CONFIG.classifiers
    return _classify_many(image, boxes, config.SPECIES_MODEL_PATH, SPECIES_LABELS,
                          cfg.species_min_confidence, "unknown", "species", cfg)


def classify_side(image: np.ndarray, box: tuple[float, float, float, float],
                  cfg: config.Classifiers | None = None) -> Classification:
    return classify_side_many(image, [box], cfg)[0]


def classify_side_many(image: np.ndarray, boxes: list[tuple[float, float, float, float]],
                       cfg: config.Classifiers | None = None) -> list[Classification]:
    cfg = cfg or config.CONFIG.classifiers
    return _classify_many(image, boxes, config.SIDE_MODEL_PATH, SIDE_LABELS,
                          cfg.side_min_confidence, "UNKNOWN", "flank-side", cfg)


def _classify_many(image: np.ndarray, boxes: list[tuple[float, float, float, float]],
                   path: Path, labels: tuple[str, ...], threshold: float,
                   unknown_label: str, kind: str,
                   cfg: config.Classifiers) -> list[Classification]:
    out: list[Classification | None] = [None] * len(boxes)
    indices, crops = [], []
    for index, box in enumerate(boxes):
        crop = crop_detection(image, box)
        if crop is None:
            out[index] = Classification(unknown_label, None, "unavailable", None,
                                         "invalid detector crop")
        else:
            indices.append(index)
            crops.append(crop)
    for index, result in zip(indices, _predict_many(
            crops, path, labels, threshold, unknown_label, kind, cfg)):
        out[index] = result
    return [result for result in out if result is not None]
