"""Stage 3's missing input: where are the shoulder and hip? See
edge/pipeline/identify.py's module docstring for why this was, until
now, an explicit and honest gap rather than a geometric guess dressed up
as the real thing -- AUDIT_AND_REVISED_PLAN.md Task 5/Task 4 built this
module in two stages:

    Task 5: estimate_keypoints_stub() -- a fixed geometric guess from
        the detection box alone (shoulder 25% along the box, hip 75%),
        so the full crop -> rectify -> embed -> match -> decide chain
        could be wired and proven end to end before the real regressor
        existed. Still used as the fallback when the trained weights
        are not present on a given machine (CLAUDE.md rule 8 -- degrade
        to something documented, not crash).

    Task 4: a trained 2-keypoint YOLO11-pose regressor (near-side
        shoulder + hip only, not the full 15-point skeleton),
        deliberately AGPL-3.0-derived (Ultralytics) -- see
        docs/MODEL_CHOICES.md for that trade-off, recorded rather than
        inherited silently. estimate_keypoints() now tries this model
        first and only falls back to the stub if the weights are
        missing.

**A known, unfixed limitation carried by BOTH paths: neither one
determines which physical flank (true left or true right) is showing.**
The model, like the stub, always labels its two points "right_shoulder"
and "right_hip" -- a fixed naming convention downstream code needs, not
a claim about which side of the tiger's body they actually are.
identify.infer_side() therefore always resolves to 'R' for every real,
un-labelled Pench upload, regardless of which flank the photo actually
shows: on a real deployment where roughly half of captures show the
left flank, those crops are compared against the RIGHT-side catalogue,
silently. Task 4 was scoped as "near-side shoulder and hip only," which
is exactly what was built; a genuine fix needs the model to also
classify which side is visible (e.g. two classes instead of one,
labelled from the same ATRW ground truth this build already has side
labels for) -- not attempted here, flagged here instead of discovered
later. See docs/RESULTS.md's "wild" evaluation section.

Both paths return the same shape identify.py expects: {name: (x, y, v)}
in absolute PIXEL coordinates (not the [0,1]-normalised frame
Detector.detect() itself returns boxes in), COCO visibility convention.
rectify_flank() uses these points to index real pixels via
cv2.warpPerspective and to threshold a real body length in pixels
(Identify.rect_body_depth_ratio, the >=1px degeneracy guard) -- feeding
it normalised [0,1] coordinates was tried and produces a body length
under 1 in every real case, refusing every crop as "degenerate" before
it ever reaches rectification. Caught by running this against a real
photo, not by inspection.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from edge import config

Keypoints = dict

WEIGHTS_PATH = config.WEIGHTS_DIR / "keypoints" / "run" / "weights" / "best.pt"
CONF_THRESHOLD = 0.5
"""Below this per-keypoint confidence, YOLO11-pose's own output is
treated as v=0 (not visible) rather than v=2 -- quality_gate() then
refuses the crop on its own terms (CLAUDE.md rule 8) instead of
rectifying from a point the model itself was not confident about."""


def estimate_keypoints_stub(box: tuple[float, float, float, float],
                             image_size: tuple[int, int]) -> Keypoints:
    """box: (x, y, w, h), normalised [0,1] (Detector.detect()'s own
    convention). image_size: (width, height) in pixels -- required to
    convert the box to the pixel coordinates rectify_flank() actually
    needs; see the module docstring for why normalised coordinates
    silently produced only refusals.

    Fixed fractions along the box's long (width) axis, vertically
    centred: shoulder at 25%, hip at 75%. Always the RIGHT side --
    there is no signal in a bounding box alone to tell which flank is
    facing the camera, and alternating or guessing randomly would just
    make failures nondeterministic instead of consistent and
    diagnosable."""
    x, y, w, h = box
    img_w, img_h = image_size
    px_x, px_y, px_w, px_h = x * img_w, y * img_h, w * img_w, h * img_h
    return {
        "right_shoulder": (px_x + 0.25 * px_w, px_y + 0.5 * px_h, 2),
        "right_hip": (px_x + 0.75 * px_w, px_y + 0.5 * px_h, 2),
    }


_model = None
_model_load_attempted = False


def _get_model():
    """Lazy singleton, matching edge/pipeline/detector.py's pattern.
    Loads at most once per process; a missing weights file is cached as
    "no model" rather than retried on every call."""
    global _model, _model_load_attempted
    if _model is None and not _model_load_attempted:
        _model_load_attempted = True
        if WEIGHTS_PATH.exists():
            from ultralytics import YOLO
            _model = YOLO(str(WEIGHTS_PATH))
    return _model


def estimate_keypoints_trained(box: tuple[float, float, float, float],
                                image_path: str) -> Keypoints | None:
    """Crops to the (padded) detection box and runs the trained
    regressor on that crop -- training data was itself near-full-frame
    tiger crops (ATRW's re-ID images), so inference matches that
    distribution rather than running on a full camera-trap scene the
    model never saw the shape of. Returns None (not a guess) if the
    model is not loaded or produced no detection in the crop."""
    model = _get_model()
    if model is None:
        return None

    with Image.open(image_path) as img:
        img = img.convert("RGB")
        img_w, img_h = img.size
        x, y, w, h = box
        # A small margin around the detection box -- the training crops
        # were not perfectly tight either (docs/MODEL_CHOICES.md), and a
        # tiny bit of context reduces the chance the shoulder/hip fall
        # just outside a too-tight crop.
        margin = 0.1
        px0 = max(0, int((x - margin * w) * img_w))
        py0 = max(0, int((y - margin * h) * img_h))
        px1 = min(img_w, int((x + w + margin * w) * img_w))
        py1 = min(img_h, int((y + h + margin * h) * img_h))
        if px1 <= px0 or py1 <= py0:
            return None
        crop = img.crop((px0, py0, px1, py1))

    results = model.predict(np.asarray(crop), verbose=False)
    if not results or results[0].keypoints is None or len(results[0].keypoints.xy) == 0:
        return None

    kp_xy = results[0].keypoints.xy[0].cpu().numpy()      # (2, 2): shoulder, hip -- crop-relative pixels
    kp_conf = results[0].keypoints.conf[0].cpu().numpy() if results[0].keypoints.conf is not None \
        else np.ones(2)
    if kp_xy.shape[0] < 2:
        return None

    out = {}
    for name, (cx, cy), conf in zip(("right_shoulder", "right_hip"), kp_xy, kp_conf):
        # crop-relative -> full-image pixel coordinates
        out[name] = (px0 + float(cx), py0 + float(cy), 2 if conf >= CONF_THRESHOLD else 0)
    return out


def estimate_keypoints(box: tuple[float, float, float, float], image_path: str) -> Keypoints:
    """Tries the trained regressor first, falls back to the fixed
    geometric stub if the weights are not present on this machine or
    the model found nothing in the crop -- CLAUDE.md rule 8: a documented
    fallback, not a crash, and identify.py's own quality_gate() is what
    actually decides whether either path's output is trustworthy enough
    to rectify from."""
    trained = estimate_keypoints_trained(box, image_path)
    if trained is not None:
        return trained
    with Image.open(image_path) as img:
        image_size = img.size
    return estimate_keypoints_stub(box, image_size)
