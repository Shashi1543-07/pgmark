"""Unit tests for local-only species and physical-flank gates.

Run with ``python -m tests.unit.test_classifiers``. These tests use a fake
in-memory Torch module; they neither require nor attempt to obtain model
weights.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from edge import config  # noqa: E402
from edge.pipeline import classifiers, keypoints  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' -- ' + detail if detail else ''}")


class FakeModel:
    def __init__(self, logits: list[float]):
        self.logits = torch.tensor([logits], dtype=torch.float32)

    def __call__(self, _tensor):
        return self.logits


def _fake_prediction(logits: list[float], labels: tuple[str, ...], threshold: float,
                     unknown: str):
    original_load = classifiers._load_local
    original_version = classifiers._model_version
    try:
        classifiers._load_local = lambda _path: FakeModel(logits)
        classifiers._model_version = lambda _path, kind: f"{kind}@test"
        return classifiers._predict(
            np.zeros((20, 30, 3), dtype=np.uint8), Path("test.ts"), labels,
            threshold, unknown, "test", config.CONFIG.classifiers)
    finally:
        classifiers._load_local = original_load
        classifiers._model_version = original_version


def test_contract_and_confidence() -> None:
    species = _fake_prediction([8.0] + [0.0] * 9, classifiers.SPECIES_LABELS, 0.8, "unknown")
    check("high-confidence local logits produce tiger", species.label == "tiger"
          and species.source == "classifier" and species.model_version == "test@test")

    side = _fake_prediction([0.1, 0.0, 0.0], classifiers.SIDE_LABELS, 0.85, "UNKNOWN")
    check("low-confidence side logits become UNKNOWN rather than a guessed flank",
          side.label == "UNKNOWN" and side.confidence is not None and side.confidence < 0.85,
          str(side))

    unknown = _fake_prediction([0.0] * 9 + [8.0], classifiers.SPECIES_LABELS, 0.8, "unknown")
    check("the explicit species unknown class stays unknown", unknown.label == "unknown")


def test_crop_and_missing_model_refusal() -> None:
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = classifiers.crop_detection(image, (0.25, 0.2, 0.5, 0.5))
    check("normalized detector crop maps to bounded image pixels",
          crop is not None and crop.shape == (50, 100, 3), str(None if crop is None else crop.shape))
    check("invalid detector crop refuses", classifiers.crop_detection(image, (0, 0, 0, 1)) is None)

    original_path = config.SPECIES_MODEL_PATH
    try:
        config.SPECIES_MODEL_PATH = Path("definitely-missing-local-species-model.ts")
        missing = classifiers.classify_species(image, (0, 0, 1, 1))
    finally:
        config.SPECIES_MODEL_PATH = original_path
    check("missing local species asset returns safe unknown without downloading",
          missing.label == "unknown" and missing.source == "unavailable", str(missing))


def test_pose_relabel_uses_classifier_side() -> None:
    convention = {"right_shoulder": (10, 20, 2), "right_hip": (90, 40, 2)}
    left = keypoints.apply_physical_side(convention, "L")
    right = keypoints.apply_physical_side(convention, "R")
    check("a side L decision relabels convention-only pose anchors to L",
          left is not None and "left_shoulder" in left and "right_shoulder" not in left)
    check("a side R decision retains anchors in the R catalogue namespace",
          right is not None and "right_shoulder" in right and "left_shoulder" not in right)
    check("missing pose pair refuses instead of inventing rectification anchors",
          keypoints.apply_physical_side({"right_shoulder": (1, 1, 2)}, "L") is None)


def main() -> int:
    test_contract_and_confidence()
    test_crop_and_missing_model_refusal()
    test_pose_relabel_uses_classifier_side()
    print("\n".join(f"  ok   {item}" for item in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {item}" for item in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
