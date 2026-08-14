"""Stage B -- the animal/person/vehicle detector. See blueprint §6.2,
docs/MODEL_CHOICES.md, AUDIT_AND_REVISED_PLAN.md Prompt 3.

MegaDetector V6, MDV6-mit-yolov9-c: MIT-licensed, 9.7M parameters,
compact enough for a laptop CPU (docs/MODEL_CHOICES.md explains why this
variant and not the smaller, AGPL-3.0-licensed YOLOv10-compact build).

The architecture/inference code in edge/pipeline/vendor/yolo_mit/ is
vendored from microsoft/CameraTraps (MIT licensed), NOT installed via
`pip install PytorchWildlife` -- that package's own declared dependencies
unconditionally pull in `ultralytics` (AGPL-3.0), regardless of which
model variant is actually used. See
edge/pipeline/vendor/yolo_mit/NOTICE.md for the full explanation.

Lazy-loaded: the model is not constructed until the first call to
get_detector(), and cached after that -- a laptop that never runs Stage B
in a given process never pays the load cost, and every call after the
first is free.

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
import yaml
from omegaconf import OmegaConf
from PIL import Image

from edge import config

_VENDOR_ROOT = Path(__file__).resolve().parent / "vendor" / "yolo_mit"
if str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))

from yolo.model.yolo import create_model                       # noqa: E402
from yolo.tools.data_loader import AugmentationComposer         # noqa: E402
from yolo.utils.bounding_box_utils import create_converter       # noqa: E402
from yolo.utils.model_utils import PostProcess                   # noqa: E402

MODEL_VERSION = "megadetector-v6-mit-yolov9-c@1.0.0"

ANIMAL_LABEL = "animal"
PERSON_LABEL = "person"
VEHICLE_LABEL = "vehicle"
"""The single source of truth for these three strings. triage.py's
privacy routing (a person frame must never reach the tiger pipeline)
compares Detection.label against PERSON_LABEL, imported from here, not
against a second, independently-typed "person" literal -- see
tests/unit/test_vendor_licensing.py's sibling check in
tests/live/test_routes.py for why that specific class of bug (the
model's own id-to-name table drifting out of step with the string the
routing code checks against) is worth a permanent, not a one-time, test."""

CLASS_NAMES = {0: ANIMAL_LABEL, 1: PERSON_LABEL, 2: VEHICLE_LABEL}
"""Matches images.status/detections.label's existing 'animal'|'person'|
'vehicle' vocabulary (edge/db/migrations/0001_init.sql) exactly -- no
translation layer needed between the model's classes and the schema's."""

CHECKPOINT_PATH = config.WEIGHTS_DIR / "megadetector" / "MDV6-mit-yolov9-c.ckpt"
CONFIG_PATH = config.WEIGHTS_DIR / "megadetector" / "config_v9s.yaml"


@dataclass
class Detection:
    label: str          # animal | person | vehicle
    conf: float
    x: float             # normalised [0, 1], top-left
    y: float
    w: float
    h: float


class Detector:
    """Wraps the vendored YOLO-MIT model for single-image inference.
    Not PyTorchWildlife's own YOLOMITBase -- that class depends on
    `supervision`, `wget`, `lightning`, and PyTorchWildlife's own
    dataset-loading module, none of which single-image inference against
    a pre-downloaded checkpoint needs (see vendor/yolo_mit/NOTICE.md)."""

    def __init__(self, checkpoint_path: Path, config_path: Path, device: str = "cpu"):
        if not checkpoint_path.exists() or not config_path.exists():
            raise FileNotFoundError(
                f"MegaDetector weights not found at {checkpoint_path} / {config_path} -- "
                f"run `python -m tools.fetch_data --set megadetector` first")
        cfg_dict = yaml.safe_load(config_path.read_text())
        self.cfg = OmegaConf.create(cfg_dict)
        OmegaConf.set_struct(self.cfg.model, False)
        self.device = device
        self.image_size = list(self.cfg.image_size)

        self.model = create_model(self.cfg.model, weight_path=str(checkpoint_path),
                                   class_num=len(CLASS_NAMES)).to(device)
        self.model.eval()
        self.converter = create_converter(self.cfg.model.name, self.model,
                                           self.cfg.model.anchor, self.image_size, device)
        self.transform = AugmentationComposer([], self.image_size, self.image_size[0])

    def detect(self, image_path: str, conf_threshold: float, iou_threshold: float = 0.5,
               max_boxes: int = 300) -> list[Detection]:
        """Runs detection on one image. Returns normalised [0,1] boxes --
        deliberately independent of the source image's pixel dimensions,
        matching detections.x/y/w/h's own convention."""
        post_process = PostProcess(
            self.converter,
            OmegaConf.create({"min_confidence": conf_threshold, "min_iou": iou_threshold,
                               "max_bbox": max_boxes}))

        with Image.open(image_path) as im:
            im = im.convert("RGB")
            width, height = im.size
            tensor, _, rev_tensor = self.transform(im)

        tensor = tensor.to(self.device)[None]
        rev_tensor = rev_tensor.to(self.device)[None]

        with torch.no_grad():
            predict = self.model(tensor)
            results = post_process(predict, rev_tensor)   # [cls, x1, y1, x2, y2, conf] per box

        boxes = results[0].cpu().numpy() if len(results) else np.zeros((0, 6))
        out = []
        for cls_id, x1, y1, x2, y2, conf in boxes:
            label = CLASS_NAMES.get(int(cls_id))
            if label is None:
                continue
            out.append(Detection(
                label=label, conf=float(conf),
                x=max(0.0, min(1.0, x1 / width)), y=max(0.0, min(1.0, y1 / height)),
                w=max(0.0, min(1.0, (x2 - x1) / width)), h=max(0.0, min(1.0, (y2 - y1) / height))))
        return out


_detector: Detector | None = None


def get_detector() -> Detector:
    """Lazy singleton -- constructed on first use, cached after that."""
    global _detector
    if _detector is None:
        _detector = Detector(CHECKPOINT_PATH, CONFIG_PATH)
    return _detector
