"""Offline-only replacement for upstream YOLO asset preparation.

The upstream MIT helper downloaded training datasets and missing checkpoints.
PUGMARK uses this vendored package for local inference only, so retaining a
callable downloader would violate the field deployment invariant even if the
normal inference path never reached it. Missing assets are a release defect,
not a trigger for recovery through a network.
"""
from __future__ import annotations

from pathlib import Path


class OfflineAssetError(RuntimeError):
    """Raised when an upstream training/download path is reached at the edge."""


def prepare_dataset(dataset_cfg, task: str) -> None:
    raise OfflineAssetError(
        "YOLO dataset preparation is unavailable on an air-gapped edge node; "
        "stage datasets during the release build")


def prepare_weight(download_link: str | None = None, weight_path: Path = Path("model.ckpt")) -> None:
    raise FileNotFoundError(
        f"model weights are required locally at {weight_path}; automatic downloads are disabled")
