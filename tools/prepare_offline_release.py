"""Build-time manifest generation and local cache warming for a field bundle.

All model files must already be present under edge/models/. This utility has
no URL, downloader, or fallback path. Its job is to hash exactly those local
files and load each one once while the release is being assembled.

    python -m tools.prepare_offline_release --write-manifest --prewarm
    python -m tools.verify_offline_release
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from edge import config
from tools.verify_offline_release import sha256, verify


def write_manifest() -> None:
    missing = [f"{name}: {path}" for name, path in config.runtime_model_paths().items()
               if not path.is_file()]
    if missing:
        raise FileNotFoundError("cannot make an offline manifest; missing\n  " + "\n  ".join(missing))
    files = {
        path.relative_to(config.MODELS_DIR).as_posix(): {
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in config.runtime_model_paths().values()
    }
    config.MODEL_MANIFEST_PATH.write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"wrote {config.MODEL_MANIFEST_PATH}")


def prewarm() -> None:
    # Imports happen after edge.config has forced every offline environment
    # switch. Each constructor receives an explicit absolute local path.
    from edge.pipeline import classifiers, detector, identify, keypoints

    detector.get_detector()
    if keypoints._get_model() is None:  # noqa: SLF001 - intentional packaging probe
        raise RuntimeError(f"could not load local keypoint model {keypoints.WEIGHTS_PATH}")
    identify.load_embedder(identify.WEIGHTS_PATH)
    # A synthetic crop only warms the locally loaded TorchScript modules; it
    # is not a model-quality check and cannot hide a missing production asset.
    import numpy as np
    sample = np.zeros((224, 224, 3), dtype=np.uint8)
    box = (0.0, 0.0, 1.0, 1.0)
    for classify in (classifiers.classify_species, classifiers.classify_side):
        result = classify(sample, box)
        if result.source != "classifier":
            raise RuntimeError(f"could not load local classifier: {result.detail}")
    print("pre-warmed torch, detector, Ultralytics, embedder, species, and side local model caches")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--prewarm", action="store_true")
    args = parser.parse_args()
    if not args.write_manifest and not args.prewarm:
        parser.error("choose --write-manifest, --prewarm, or both")
    if args.write_manifest:
        write_manifest()
    defects = verify()
    if defects:
        raise SystemExit("offline bundle is incomplete:\n  " + "\n  ".join(defects))
    if args.prewarm:
        prewarm()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
