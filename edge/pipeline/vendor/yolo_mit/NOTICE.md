# Vendored code notice

The `yolo/` package in this directory is copied, unmodified except for its
location on disk, from:

    microsoft/CameraTraps (PyTorchWildlife)
    PytorchWildlife/models/detection/yolo_mit/yolo/
    https://github.com/microsoft/CameraTraps
    Copyright (c) Microsoft Corporation.
    Licensed under the MIT License.

## Why vendored instead of `pip install PytorchWildlife`

The MDV6-mit-yolov9-c checkpoint this build uses is itself MIT-licensed
(see `docs/MODEL_CHOICES.md`), but the `PytorchWildlife` PyPI package's own
declared dependencies unconditionally include `ultralytics` and `yolov5`
(both AGPL-3.0) regardless of which model variant is actually used —
installing the package would pull AGPL-licensed code into this deployment
even though this build never calls any ultralytics-based code path.

The `yolo_mit/yolo/` subtree specifically does not import `ultralytics`
anywhere (checked directly against every file before vendoring, not
assumed) — it is model architecture, config, and post-processing code with
no dependency on the AGPL-licensed parts of the wider package. Vendoring
just this subtree keeps the MIT-licensed code and drops the unrelated
AGPL-licensed dependency the full package would otherwise force in.

`edge/pipeline/detector.py` is new code written for this project — a thin
wrapper around `yolo.model.yolo.create_model`, `create_converter`, and
`PostProcess` — replacing PyTorchWildlife's own `YOLOMITBase` wrapper
class, which was not vendored because it depends on `supervision`, `wget`,
`lightning`, and PyTorchWildlife's own dataset-loading module. None of
those are needed for single-image inference against a pre-downloaded
checkpoint, and skipping them avoids the extra dependency surface.

## Provenance

Copied 2026-08-14 from the `main` branch of
https://github.com/microsoft/CameraTraps, commit at time of copy visible
via that repository's history. See `LICENSE` in this directory for the
full MIT license text, copied from the same repository's root `LICENSE`
file.
