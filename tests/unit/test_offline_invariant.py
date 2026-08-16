"""Static and import-level checks for the air-gapped field runtime.

These do not merely inspect the UI: a CDN-free page is irrelevant if a model
loader starts a download on first inference. Run with:

    python -m tests.unit.test_offline_invariant
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from edge import config  # noqa: E402
from edge.pipeline import classifiers, detector, identify, keypoints  # noqa: E402

PASS, FAIL = [], []
REPO_ROOT = Path(__file__).resolve().parents[2]
EDGE_ROOT = REPO_ROOT / "edge"
OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "YOLO_OFFLINE")
NETWORK_MODULES = {"requests", "httpx", "urllib", "socket", "huggingface_hub"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def _network_violations() -> list[str]:
    violations: list[str] = []
    for source in EDGE_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                aliases.add(node.module.split(".")[0])
        banned_imports = aliases & NETWORK_MODULES
        if banned_imports:
            violations.append(f"{source.relative_to(REPO_ROOT)} imports {sorted(banned_imports)}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "from_pretrained":
                violations.append(f"{source.relative_to(REPO_ROOT)}:{node.lineno} calls from_pretrained")
            if (isinstance(node, ast.Attribute) and node.attr == "hub"
                    and isinstance(node.value, ast.Name) and node.value.id == "torch"):
                violations.append(f"{source.relative_to(REPO_ROOT)}:{node.lineno} reaches torch.hub")
    return violations


def test_offline_environment_is_forced() -> None:
    check("all model-library offline switches are forced before model imports",
          all(os.environ.get(name) == "1" for name in OFFLINE_ENV),
          str({name: os.environ.get(name) for name in OFFLINE_ENV}))


def test_runtime_models_are_absolute_and_bundled() -> None:
    paths = config.runtime_model_paths()
    check("the runtime declares detector, keypoint, identity, species, and side assets",
          len(paths) == 6, str(paths))
    check("edge/models is an absolute local root", config.MODELS_DIR.is_absolute(), str(config.MODELS_DIR))
    outside = [f"{name}: {path}" for name, path in paths.items()
               if not path.is_absolute() or not path.is_relative_to(config.MODELS_DIR)]
    check("every runtime model path stays under edge/models", not outside, "; ".join(outside))
    check("all runtime model paths are shared with their loaders",
          detector.CHECKPOINT_PATH == config.DETECTOR_CHECKPOINT_PATH
          and detector.CONFIG_PATH == config.DETECTOR_CONFIG_PATH
          and keypoints.WEIGHTS_PATH == config.KEYPOINTS_MODEL_PATH
          and identify.WEIGHTS_PATH == config.EMBEDDER_MODEL_PATH
          and config.SPECIES_MODEL_PATH.is_relative_to(config.MODELS_DIR)
          and config.SIDE_MODEL_PATH.is_relative_to(config.MODELS_DIR))
    check("species and side classifiers have fixed reviewed label contracts",
          classifiers.SPECIES_LABELS == ("tiger", "leopard", "dhole", "sambar", "chital",
                                         "boar", "langur", "human", "vehicle", "unknown")
          and classifiers.SIDE_LABELS == ("L", "R", "UNKNOWN"))
    check("no model checkpoint remains at the repository root",
          not list(REPO_ROOT.glob("*.pt")), str(list(REPO_ROOT.glob("*.pt"))))


def test_runtime_has_no_network_or_automatic_weight_fetch() -> None:
    violations = _network_violations()
    check("edge runtime imports no networking client and calls no automatic model loader",
          not violations, "; ".join(violations))
    source = inspect.getsource(identify.TripletEmbedder)
    check("the embedder constructs torchvision without named downloadable weights",
          "weights=None" in source and "ResNet50_Weights" not in source)
    check("the embedder default cannot request pretrained network weights",
          "pretrained" not in inspect.signature(identify.TripletEmbedder).parameters)
    trainer_source = (REPO_ROOT / "tools" / "train_keypoints.py").read_text(encoding="utf-8")
    check("the keypoint trainer passes Ultralytics an explicit local path, not a model name",
          "YOLO(str(BASE_MODEL_PATH))" in trainer_source)


def main() -> int:
    test_offline_environment_is_forced()
    test_runtime_models_are_absolute_and_bundled()
    test_runtime_has_no_network_or_automatic_weight_fetch()
    print("\n".join(f"  ok   {item}" for item in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {item}" for item in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
