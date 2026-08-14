"""Unit tests: pure logic, no models, no I/O. See blueprint §13 Layer 1.

    python -m tests.unit.test_identify

Covers edge/pipeline/identify.py's non-model logic: side inference,
the quality gate, rectification geometry, and the three-way decision.
Real embedding/matching machinery is proven against real ATRW data in
tools/eval_identify.py and the live suite instead -- this file is only
the parts a pure function can check without a trained model.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from edge import config                       # noqa: E402
from edge.pipeline import identify            # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def test_infer_side() -> None:
    right = {"right_shoulder": (10, 10, 2), "right_hip": (10, 50, 2),
             "left_shoulder": (0, 0, 0), "left_hip": (0, 0, 0)}
    left = {"left_shoulder": (90, 10, 2), "left_hip": (90, 50, 2),
            "right_shoulder": (0, 0, 0), "right_hip": (0, 0, 0)}
    both = {"right_shoulder": (10, 10, 2), "right_hip": (10, 50, 2),
            "left_shoulder": (90, 10, 2), "left_hip": (90, 50, 2)}

    check("a clean right-flank profile resolves to R", identify.infer_side(right) == "R")
    check("a clean left-flank profile resolves to L", identify.infer_side(left) == "L")
    check("both trunk pairs visible (non-profile pose) refuses rather than guesses",
          identify.infer_side(both) is None)
    check("no keypoints at all refuses rather than guesses", identify.infer_side({}) is None)
    check("one-sided but incomplete (shoulder without hip) refuses",
          identify.infer_side({"right_shoulder": (10, 10, 2)}) is None)


def test_quality_gate() -> None:
    cfg = config.CONFIG.identify
    clean_right = {"right_shoulder": (10, 10, 2), "right_hip": (10, 50, 2)}
    occluded_right = {"right_shoulder": (10, 10, 1), "right_hip": (10, 50, 1)}
    ambiguous = {"right_shoulder": (10, 10, 2), "right_hip": (10, 50, 2),
                 "left_shoulder": (90, 10, 2), "left_hip": (90, 50, 2)}

    q = identify.quality_gate(clean_right, (200, 200, 3), cfg)
    check("fully visible trunk points score full quality", q.ok and q.quality == 1.0, str(q))

    q2 = identify.quality_gate(occluded_right, (200, 200, 3), cfg)
    check("occluded-but-labelled trunk points score half quality", q2.quality == 0.5, str(q2))

    q3 = identify.quality_gate(clean_right, (10, 10, 3), cfg)
    check("a crop below min_crop_pixels is refused even with clean keypoints",
          not q3.ok and "px floor" in q3.reason, str(q3))

    q4 = identify.quality_gate(ambiguous, (200, 200, 3), cfg)
    check("an ambiguous side is refused before quality is even scored",
          not q4.ok and q4.side is None, str(q4))

    low_cfg = config.Identify(min_quality=0.9)
    q5 = identify.quality_gate(occluded_right, (200, 200, 3), low_cfg)
    check("quality below Identify.min_quality is refused",
          not q5.ok and "min_quality" in q5.reason, str(q5))


def test_rectify_flank() -> None:
    cfg = config.CONFIG.identify
    img = np.zeros((300, 300, 3), dtype=np.uint8)
    kp = {"right_shoulder": (50, 50, 2), "right_hip": (150, 60, 2)}

    rect = identify.rectify_flank(img, kp, "R", cfg)
    check("a valid shoulder-hip pair rectifies to the canonical size",
          rect is not None and rect.shape == (identify.RECT_HEIGHT, identify.RECT_WIDTH, 3),
          None if rect is None else str(rect.shape))

    degenerate = {"right_shoulder": (50, 50, 2), "right_hip": (50, 50, 2)}
    rect2 = identify.rectify_flank(img, degenerate, "R", cfg)
    check("a zero-length shoulder-hip distance refuses rather than warping garbage",
          rect2 is None)

    missing = {"right_shoulder": (50, 50, 2)}
    rect3 = identify.rectify_flank(img, missing, "R", cfg)
    check("a missing hip point refuses", rect3 is None)

    wrong_side = {"left_shoulder": (50, 50, 2), "left_hip": (150, 60, 2)}
    rect4 = identify.rectify_flank(img, wrong_side, "R", cfg)
    check("asking for side R with only left-side points present refuses "
          "rather than rectifying the wrong side", rect4 is None)


def test_decide() -> None:
    cfg = config.CONFIG.identify
    d1 = identify.decide(min(1.0, cfg.t_high + 0.05), cfg)
    check("a high score auto-assigns", d1[0] == "auto", str(d1))
    d2 = identify.decide(0.6, cfg)
    check("a mid score goes to human review", d2[0] == "review", str(d2))
    d3 = identify.decide(0.2, cfg)
    check("a low score enrols provisionally", d3[0] == "enroll", str(d3))
    d4 = identify.decide(None, cfg)
    check("no catalogue to compare against enrols, not a refusal and not a guess",
          d4[0] == "enroll", str(d4))
    # boundary values are inclusive (>=), matching edge/config.py's own comments
    d5 = identify.decide(cfg.t_high, cfg)
    check("a score exactly at t_high auto-assigns (>=, not >)", d5[0] == "auto", str(d5))
    d6 = identify.decide(cfg.t_low, cfg)
    check("a score exactly at t_low goes to review, not enrol (>=, not >)",
          d6[0] == "review", str(d6))


def test_match_ranks_by_similarity() -> None:
    rng = np.random.default_rng(20260813)
    query = rng.normal(size=identify.EMBED_DIM).astype(np.float32)
    query /= np.linalg.norm(query)
    same = query.copy()
    far = -query.copy()
    catalogue = [
        {"entity_id": "e_far", "ind_id": "X", "embedding": far},
        {"entity_id": "e_same", "ind_id": "Y", "embedding": same},
    ]
    ranked = identify.match(query, catalogue)
    check("the identical embedding ranks first, not by catalogue order",
          ranked[0]["entity_id"] == "e_same", str([r["entity_id"] for r in ranked]))
    check("an opposite embedding scores near -1", ranked[-1]["score"] < -0.99,
          str(ranked[-1]["score"]))


def test_embedding_serialization_round_trips() -> None:
    rng = np.random.default_rng(1)
    emb = rng.normal(size=identify.EMBED_DIM).astype(np.float32)
    blob = identify.serialize_embedding(emb)
    back = identify.deserialize_embedding(blob)
    check("an embedding survives a serialize/deserialize round trip exactly",
          np.array_equal(emb, back))


def main() -> int:
    test_infer_side()
    test_quality_gate()
    test_rectify_flank()
    test_decide()
    test_match_ranks_by_similarity()
    test_embedding_serialization_round_trips()
    print("\n".join(f"  ok   {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {f}" for f in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
