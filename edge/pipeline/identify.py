"""Stage 3 -- flank rectification, quality gate, embedding, matching.
See blueprint §7, docs/DATA.md §§1-3, docs/MODEL_CHOICES.md,
AUDIT_AND_REVISED_PLAN.md Prompt 8.

Six steps, each independently testable:

    infer_side()     -- which flank is showing, from keypoint visibility
    quality_gate()    -- refuses rather than guesses (CLAUDE.md rule 8)
    rectify_flank()   -- aligns the near-side shoulder-hip axis to a
                         canonical rectangle (NOT nose-to-tail -- see
                         docs/DATA.md §3 for why that axis is the wrong
                         one; and NOT a true 4-point quadrilateral either
                         -- see the function's own docstring for why
                         that turned out not to match real data)
    embed()           -- TriHard triplet-loss ResNet-50 at 256x128,
                         width/height swapped from pedestrian re-ID
                         because tiger boxes are horizontal-major
    match()           -- cosine similarity against a side-specific
                         catalogue (repo.catalogue_for_side() is the
                         only legal source of that catalogue -- see
                         Rule 6)
    decide()          -- auto-assign / human review / provisional
                         enrol, never a silent guess in between

**Where the four keypoints this module needs come from is explicitly
not this module's problem.** Evaluated against ATRW, they are the
dataset's own ground-truth annotations. On a real, unlabelled Pench
crop, nothing supplies them: training a pose model (HRNet, never
OpenPose -- docs/DATA.md §3) is a separate, larger project than the
embedder itself and is NOT attempted in this build. That is a stated,
honest gap, the same shape as Stage B not existing -- not a geometric
guess dressed up as the real thing.

**The rectification geometry itself is not what docs/DATA.md's prose
suggested going in.** "Warp the shoulder-hip quadrilateral" reads as
four measured corners. Checked against 905 side-resolved ATRW training
crops, the far side's shoulder+hip are unlabelled in every single one
-- a profile photograph cannot show them, they are on the opposite side
of the animal's body. rectify_flank() uses the near side's two points
plus a stated proportional estimate for the perpendicular extent
(Identify.rect_body_depth_ratio); see its docstring and
docs/MODEL_CHOICES.md.

No SQL lives here (repo.py owns all of it). No dataset-specific
parsing lives here either -- ATRW's flat keypoint arrays are adapted
into the named dict this module expects by tools/atrw_dataset.py, not
here, so this module has no idea ATRW exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision

from edge import config

EMBED_MODEL_VERSION = "trihard-resnet50@1.0.0"
EMBED_DIM = 256

WEIGHTS_PATH = Path(__file__).resolve().parents[2] / "data" / "weights" / "identify_embedder.pt"

# docs/DATA.md §1: 256 wide x 128 tall -- swapped from pedestrian re-ID's
# 128 wide x 256 tall, because tiger boxes are horizontal-major.
RECT_WIDTH, RECT_HEIGHT = 256, 128

TRUNK_POINTS = ("right_shoulder", "left_shoulder", "right_hip", "left_hip")
"""docs/DATA.md §3, ATRW Table 4: these four are the most reliably
annotated keypoints (sigma^2 4.1-9.1 x 10^-4). Nose and tail-root -- the
geometrically obvious body axis -- are the two noisiest (69.0, 46.7)."""

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

Keypoints = dict           # {name: (x, y, visibility)}, visibility 0/1/2 (COCO convention)


# ── side inference and the quality gate ────────────────────────────────

def infer_side(keypoints: Keypoints) -> str | None:
    """A profile photograph only shows one flank's shoulder+hip pair --
    the opposite side's trunk points are on the far side of the animal.
    Returns None, not a guess, when both pairs or neither pair are
    labelled: an entity IS a side (CLAUDE.md rule 6), so a crop this
    module cannot assign a side to is not a smaller-confidence version
    of a match, it is unresolvable."""
    def visible(name: str) -> bool:
        kp = keypoints.get(name)
        return kp is not None and kp[2] > 0

    right = visible("right_shoulder") and visible("right_hip")
    left = visible("left_shoulder") and visible("left_hip")
    if right and not left:
        return "R"
    if left and not right:
        return "L"
    return None     # both visible (non-profile pose) or neither (occluded/missing)


@dataclass
class QualityResult:
    ok: bool
    side: str | None
    quality: float
    reason: str


def quality_gate(keypoints: Keypoints, crop_shape: tuple[int, int], cfg: config.Identify
                  ) -> QualityResult:
    """Refuses rather than guesses (CLAUDE.md rule 8). Two independent
    failure modes, reported separately because they mean different
    things to a reviewer: an unresolvable side (structural -- no amount
    of confidence tuning fixes it) versus a resolvable side whose
    keypoints are too uncertain to trust a warp built from them
    (tunable via Identify.min_quality)."""
    side = infer_side(keypoints)
    if side is None:
        return QualityResult(False, None, 0.0, "side is not determinable from keypoint "
                              "visibility -- both or neither shoulder+hip pair is labelled")

    h, w = crop_shape[:2]
    if h * w < cfg.min_crop_pixels:
        return QualityResult(False, side, 0.0,
                              f"crop is {w}x{h}={h*w}px, below the {cfg.min_crop_pixels}px floor")

    # Confidence-weighted quality over the NEAR side's two points only --
    # rectify_flank() below never uses the far side (checked against real
    # data: it is unlabelled in every side-resolved ATRW crop), so scoring
    # it here would silently cap every quality at 0.5 regardless of how
    # good the side actually used is. COCO visibility 2 (labelled,
    # visible) counts fully; 1 (labelled, occluded -- an estimated
    # position) counts half.
    near = ("right_shoulder", "right_hip") if side == "R" else ("left_shoulder", "left_hip")
    weights = {0: 0.0, 1: 0.5, 2: 1.0}
    scores = [weights.get(keypoints[name][2], 0.0) for name in near if name in keypoints]
    quality = round(sum(scores) / len(near), 3) if scores else 0.0

    if quality < cfg.min_quality:
        return QualityResult(False, side, quality,
                              f"quality {quality} below Identify.min_quality {cfg.min_quality}")
    return QualityResult(True, side, quality, "ok")


# ── rectification ───────────────────────────────────────────────────────

def rectify_flank(image: np.ndarray, keypoints: Keypoints, side: str, cfg: config.Identify
                   ) -> np.ndarray | None:
    """Aligns the near side's shoulder-hip axis to the rectangle's long
    edge -- NOT a 4-point perspective warp of a true quadrilateral.

    Checked against 905 side-resolved ATRW training crops: the far
    shoulder+hip are unlabelled in every single one. A profile
    photograph cannot show them -- they are on the opposite side of the
    animal's body. So the source quad's other two corners are
    constructed from the near-side pair alone: the shoulder-hip line
    sets the body axis and length, and the perpendicular (dorsal-ventral)
    extent is Identify.rect_body_depth_ratio of that length, extended
    Identify.rect_margin_ratio past each anchor point along the axis.
    Both ratios are stated approximations (docs/MODEL_CHOICES.md), not
    measurements -- there is no third point to derive them from.

    Returns None if the shoulder-hip distance is degenerate (near-zero)
    rather than warping garbage."""
    near_shoulder = "right_shoulder" if side == "R" else "left_shoulder"
    near_hip = "right_hip" if side == "R" else "left_hip"
    s, h = keypoints.get(near_shoulder), keypoints.get(near_hip)
    if s is None or h is None or s[2] <= 0 or h[2] <= 0:
        return None
    sx, sy, hx, hy = float(s[0]), float(s[1]), float(h[0]), float(h[1])

    body_len = float(np.hypot(hx - sx, hy - sy))
    if body_len < 1.0:
        return None
    depth = body_len * cfg.rect_body_depth_ratio
    margin = body_len * cfg.rect_margin_ratio

    ux, uy = (hx - sx) / body_len, (hy - sy) / body_len   # unit vector: shoulder -> hip
    px, py = -uy, ux                                      # perpendicular unit vector

    def corner(along: float, across: float) -> tuple[float, float]:
        return sx + ux * along + px * across, sy + uy * along + py * across

    src = np.array([
        corner(-margin, -depth / 2), corner(body_len + margin, -depth / 2),
        corner(-margin, depth / 2), corner(body_len + margin, depth / 2),
    ], dtype=np.float32)
    dst = np.array([[0, 0], [RECT_WIDTH - 1, 0],
                     [0, RECT_HEIGHT - 1], [RECT_WIDTH - 1, RECT_HEIGHT - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(image, matrix, (RECT_WIDTH, RECT_HEIGHT))


# ── the embedder ─────────────────────────────────────────────────────────

class TripletEmbedder(nn.Module):
    """ImageNet-pretrained ResNet-50 backbone, TriHard triplet-loss
    baseline (docs/DATA.md §2 -- 47.2 cross-camera mAP in the paper,
    against PPbM's 51.7 for a seven-part pose model that is not a
    24-hour trade). Final FC replaced with an L2-normalised embedding
    head so matching is a plain dot product."""

    def __init__(self, embed_dim: int = EMBED_DIM, pretrained: bool = True):
        super().__init__()
        weights = torchvision.models.ResNet50_Weights.IMAGENET1K_V2 if pretrained else None
        backbone = torchvision.models.resnet50(weights=weights)
        self.backbone = nn.Sequential(*list(backbone.children())[:-1])   # drop backbone's own FC
        self.fc = nn.Linear(2048, embed_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        feat = self.backbone(x).flatten(1)
        return F.normalize(self.fc(feat), p=2, dim=1)


def preprocess(rect_bgr: np.ndarray) -> torch.Tensor:
    """BGR (OpenCV's native order) uint8 HxWx3 -> normalised CHW float
    tensor, ImageNet statistics (the backbone was pretrained on them)."""
    rgb = cv2.cvtColor(rect_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - IMAGENET_MEAN) / IMAGENET_STD
    return torch.from_numpy(rgb.transpose(2, 0, 1)).float()


def embed(model: TripletEmbedder, rect_bgr: np.ndarray) -> np.ndarray:
    model.eval()
    with torch.no_grad():
        x = preprocess(rect_bgr).unsqueeze(0)
        out = model(x)
    return out.squeeze(0).numpy()


def load_embedder(weights_path, embed_dim: int = EMBED_DIM) -> TripletEmbedder:
    """Loads trained weights for inference. Never falls back to
    ImageNet-only weights silently -- an untrained embedder's distances
    are meaningless, and a caller that thinks it has a real embedder
    when it does not will corrupt the catalogue with confident nonsense
    matches. Raise, do not degrade quietly (CLAUDE.md rule 8)."""
    model = TripletEmbedder(embed_dim=embed_dim, pretrained=False)
    state = torch.load(weights_path, map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    model.eval()
    return model


def serialize_embedding(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def deserialize_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


# ── matching and the three-way decision ─────────────────────────────────

def match(query_embedding: np.ndarray, catalogue: list[dict]) -> list[dict]:
    """catalogue: [{entity_id, ind_id, embedding: np.ndarray, ...}, ...],
    already restricted to one side by the caller (repo.catalogue_for_side()
    -- see the module docstring; this function trusts its input and does
    not re-check side, the same way cell_score() trusts its caller
    grouped by station). Both query and catalogue embeddings are
    L2-normalised, so cosine similarity is a plain dot product. Ranked
    highest-first."""
    scored = [{**c, "score": float(np.dot(query_embedding, c["embedding"]))}
              for c in catalogue]
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def decide(best_score: float | None, cfg: config.Identify) -> tuple[str, str]:
    """auto-assign / human review / provisional enrol -- blueprint §7.4's
    three-way split, never a silent guess in the gap between t_low and
    t_high. No existing entity of this side at all is not a low score,
    it is nothing to compare against, so it goes straight to enrol."""
    if best_score is None:
        return "enroll", "no existing entity of this side to compare against"
    if best_score >= cfg.t_high:
        return "auto", f"score {best_score:.3f} >= t_high {cfg.t_high}"
    if best_score >= cfg.t_low:
        return "review", f"score {best_score:.3f} in [t_low {cfg.t_low}, t_high {cfg.t_high})"
    return "enroll", f"score {best_score:.3f} < t_low {cfg.t_low}"


# ── orchestration ───────────────────────────────────────────────────────

def identify_crop(image: np.ndarray, keypoints: Keypoints, catalogue: list[dict],
                   model: TripletEmbedder, cfg: config.Identify | None = None) -> dict:
    """The full pipeline for one detection crop. `catalogue` must already
    be restricted to a single side by the caller -- this function infers
    which side the crop itself shows, and it is the caller's job (via
    repo.catalogue_for_side(reserve_id, side)) to have fetched the
    matching catalogue before calling this. Returns a dict describing
    what happened at every stage, even on refusal, so a caller never has
    to guess why nothing came back."""
    cfg = cfg or config.CONFIG.identify
    quality = quality_gate(keypoints, image.shape, cfg)
    if not quality.ok:
        return {"decision": "refuse", "side": quality.side, "quality": quality.quality,
                "reason": quality.reason, "embedding": None, "best_match": None,
                "candidates": [], "rect": None}

    rect = rectify_flank(image, keypoints, quality.side, cfg)
    if rect is None:
        return {"decision": "refuse", "side": quality.side, "quality": quality.quality,
                "reason": "rectification failed: degenerate shoulder-hip distance",
                "embedding": None, "best_match": None, "candidates": [], "rect": None}

    embedding = embed(model, rect)
    ranked = match(embedding, catalogue)
    best = ranked[0] if ranked else None
    decision, why = decide(best["score"] if best else None, cfg)
    return {"decision": decision, "side": quality.side, "quality": quality.quality,
            "reason": why, "embedding": embedding, "best_match": best,
            "candidates": ranked[:cfg.top_k_candidates], "rect": rect}
