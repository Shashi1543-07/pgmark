"""The "wild" evaluation (AUDIT_AND_REVISED_PLAN.md Task 5): closed-set
identification using PREDICTED boxes (Stage B) and PREDICTED keypoints
(the trained regressor, or its stub fallback), not ATRW's own
ground-truth annotations -- the ATRW paper's own distinction between a
"plain" track (manual boxes/keypoints) and a "wild" track (fully
automatic). tools/eval_identify.py measures "plain"; this measures
"wild", against the SAME held-out-identity split, so the two numbers in
docs/RESULTS.md are directly comparable.

    python -m tools.eval_identify_wild

Requires data/weights/megadetector/ and data/weights/identify_embedder.pt
(the same requirements as the live suite's Stage B / identify checks),
plus data/raw/atrw/train/. Uses whatever edge/pipeline/keypoints.py
resolves to at the time it runs -- the trained regressor if its weights
exist, the fixed-fraction stub otherwise -- so re-running this script
after tools.train_keypoints finishes is what turns the number from
"stub-plain" into "trained-wild" without any code change here.

Reuses tools/eval_identify.py's statistics (Wilson CI, bootstrap CI,
mAP, the GALLERY_ID_EXCLUSION_GAP same-clip proxy, the side-separated
evaluate()) rather than reimplementing them -- the only thing that
changes between "plain" and "wild" is where the keypoints and box come
from, not how accuracy is measured from them.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge import config                                          # noqa: E402
from edge.pipeline import detector as detector_pipeline           # noqa: E402
from edge.pipeline import keypoints as keypoints_pipeline          # noqa: E402
from edge.pipeline.identify import (                              # noqa: E402
    WEIGHTS_PATH, embed, infer_side, load_embedder, rectify_flank)
from tools.atrw_dataset import held_out_identity_split, load_labelled  # noqa: E402
from tools.eval_identify import bootstrap_ci, evaluate, wilson_ci  # noqa: E402


def embed_all_wild(rows: list[dict], embed_model, det_model, cfg) -> tuple[list[dict], dict]:
    """Same output shape as tools/eval_identify.py::embed_all(), but
    every input comes from running the real pipeline stages instead of
    reading ATRW's own annotations: Stage B for the box, then whichever
    keypoint source edge/pipeline/keypoints.py resolves to. Returns
    (embedded_rows, refusal_reason_counts) -- the counts matter here in
    a way they didn't for the "plain" evaluation, since "wild" has more
    ways to genuinely fail before ever reaching quality_gate()."""
    import cv2

    out = []
    reasons: dict[str, int] = {}

    def bump(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for row in rows:
        detections = det_model.detect(row["orig_path"],
                                       conf_threshold=cfg.triage.detector_conf_threshold)
        animal = [d for d in detections if d.label == detector_pipeline.ANIMAL_LABEL]
        if not animal:
            bump("stage_b_no_animal")
            continue
        best = max(animal, key=lambda d: d.conf)

        kp = keypoints_pipeline.estimate_keypoints((best.x, best.y, best.w, best.h),
                                                     row["orig_path"])
        side = infer_side(kp)
        if side is None:
            bump("side_not_determinable")
            continue

        image = cv2.imread(row["orig_path"])
        rect = rectify_flank(image, kp, side, cfg.identify)
        if rect is None:
            bump("rectification_failed")
            continue

        embedding = embed(embed_model, rect)
        frame_id = int(row["file_name"].split(".")[0])
        out.append({"entity_id": f"{row['ind_id']}_{side}", "ind_id": row["ind_id"],
                     "side": side, "frame_id": frame_id, "embedding": embedding})
    return out, reasons


def main() -> int:
    if not WEIGHTS_PATH.exists():
        print(f"missing {WEIGHTS_PATH} -- run `python -m tools.train_identify` first")
        return 1
    if not (detector_pipeline.CHECKPOINT_PATH.exists() and detector_pipeline.CONFIG_PATH.exists()):
        print("missing megadetector weights -- run "
              "`python -m tools.fetch_data --set megadetector` first")
        return 1

    used_trained_keypoints = keypoints_pipeline.WEIGHTS_PATH.exists()
    print(f"keypoint source: {'trained regressor' if used_trained_keypoints else 'stub (fallback)'}"
          f" -- {keypoints_pipeline.WEIGHTS_PATH}")

    cfg = config.CONFIG
    embed_model = load_embedder(WEIGHTS_PATH)
    det_model = detector_pipeline.get_detector()

    rows = load_labelled("train")
    _, held_rows = held_out_identity_split(rows)
    print(f"held-out identities: {len({r['ind_id'] for r in held_rows})}, {len(held_rows)} images")

    embedded, refusal_reasons = embed_all_wild(held_rows, embed_model, det_model, cfg)
    print(f"{len(embedded)} of {len(held_rows)} held-out images reached an embedding")
    for reason, n in sorted(refusal_reasons.items(), key=lambda kv: -kv[1]):
        print(f"  refused before embedding, {reason}: {n}")

    per_side = evaluate(embedded)
    print(f"\n\"WILD\" closed-set accuracy -- predicted boxes (Stage B) and predicted "
          f"keypoints ({'trained regressor' if used_trained_keypoints else 'stub'}), "
          f"not ATRW's ground truth:")
    print(f"{'side':6} {'entities':9} {'images':7} {'queries':8} "
          f"{'top-1 (95% CI)':22} {'top-5 (95% CI)':22} {'mAP (95% CI)':22}")
    for side in sorted(per_side):
        r = per_side[side]
        print(f"{side:6} {r['n_entities']:9} {r['n_images']:7} {r['n_queries']:8} "
              f"{str(r['top1']) + ' ' + str(r['top1_ci']):22} "
              f"{str(r['top5']) + ' ' + str(r['top5_ci']):22} "
              f"{str(r['mAP']) + ' ' + str(r['mAP_ci']):22}")

    print(f"\nCompare against the \"plain\" (ground-truth keypoints) numbers in "
          f"docs/RESULTS.md -- the gap between the two is what predicted boxes and "
          f"predicted keypoints actually cost, measured, not estimated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
