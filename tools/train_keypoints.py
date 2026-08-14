"""Trains the near-side shoulder+hip regressor (AUDIT_AND_REVISED_PLAN.md
Task 4) with Ultralytics YOLO11-pose, deliberately AGPL-3.0 --
docs/MODEL_CHOICES.md records why. Not OpenPose: the ATRW authors report
it failed to converge on the tiger skeleton entirely (docs/DATA.md §3).

    python -m tools.train_keypoints [--epochs N] [--smoke-test]

Requires data/raw/atrw_pose_yolo/ (python -m tools.keypoint_dataset).
Writes to data/weights/keypoints/ (gitignored).

yolo11n-pose.pt (the nano variant) -- CPU-only training target, per the
same "no GPU on the deployment machine, and none confirmed available for
training either" constraint as everything else in this build
(edge/config.py's own comments). --smoke-test runs 2 epochs first so
actual per-epoch wall-clock time on this hardware is a measurement
before a long run is committed to, not a guess -- see docs/RESULTS.md
for what that measurement found.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ultralytics import YOLO   # noqa: E402

DATA_YAML = Path(__file__).resolve().parents[1] / "data" / "raw" / "atrw_pose_yolo" / "data.yaml"
WEIGHTS_DIR = Path(__file__).resolve().parents[1] / "data" / "weights" / "keypoints"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--smoke-test", action="store_true",
                     help="2 epochs only, to measure real per-epoch CPU time before "
                          "committing to a long run")
    args = ap.parse_args()
    epochs = 2 if args.smoke_test else args.epochs

    if not DATA_YAML.exists():
        print(f"missing {DATA_YAML} -- run `python -m tools.keypoint_dataset` first")
        return 1

    # Training only -- production inference (edge/pipeline/keypoints.py) stays
    # CPU-only on purpose, matching the range-office laptop deployment target
    # (CLAUDE.md). This script runs offline, on whatever hardware is building
    # the release, so it is free to use a GPU when one is actually there.
    import torch
    device = "0" if torch.cuda.is_available() else "cpu"
    print(f"training device: {'cuda:0 (' + torch.cuda.get_device_name(0) + ')' if device == '0' else 'cpu'}")

    model = YOLO("yolo11n-pose.pt")   # nano: smallest pose variant
    t0 = time.time()
    model.train(
        data=str(DATA_YAML), epochs=epochs, imgsz=256, batch=8, device=device,
        project=str(WEIGHTS_DIR), name="run", exist_ok=True,
        patience=0,   # no early stopping -- a fixed, measurable epoch count
        verbose=True,
    )
    elapsed = time.time() - t0
    print(f"\n{epochs} epochs in {elapsed:.1f}s -- {elapsed/epochs:.1f}s/epoch")

    best = WEIGHTS_DIR / "run" / "weights" / "best.pt"
    print(f"best weights: {best} ({'exists' if best.exists() else 'MISSING'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
