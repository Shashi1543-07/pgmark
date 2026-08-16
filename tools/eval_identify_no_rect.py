"""Evaluates the no-rectification fallback embedder (docs/MODEL_CHOICES.md
"The no-rectification fallback, measured") -- same held-out-identity
split, same statistics (tools/eval_identify.py's evaluate(), Wilson/
bootstrap CIs), as the rectified embedder, so the comparison in
docs/RESULTS.md is direct. No augmentation at eval time (only training
uses AUGMENT) -- a plain resize, matching how tools/eval_identify.py
itself evaluates without training-time augmentation.

    python -m tools.eval_identify_no_rect
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2                              # noqa: E402
import torch                            # noqa: E402

from edge.pipeline.identify import TripletEmbedder                # noqa: E402
from tools.atrw_dataset import held_out_identity_split, load_labelled  # noqa: E402
from tools.eval_identify import evaluate                          # noqa: E402
from tools.train_identify_no_rect import WEIGHTS_PATH              # noqa: E402


def embed_all_no_rect(rows: list[dict], model) -> list[dict]:
    from edge.pipeline.identify import infer_side

    model.eval()
    out = []
    for row in rows:
        side = infer_side(row["keypoints"])
        if side is None:
            continue
        tensor = load_no_rect_eval(row)
        if tensor is None:
            continue
        with torch.no_grad():
            emb = model(tensor.unsqueeze(0)).squeeze(0).numpy()
        frame_id = int(row["file_name"].split(".")[0])
        out.append({"entity_id": f"{row['ind_id']}_{side}", "ind_id": row["ind_id"],
                     "side": side, "frame_id": frame_id, "embedding": emb})
    return out


def load_no_rect_eval(row: dict):
    """Plain resize, no augmentation -- evaluation should measure the
    model, not the training-time augmentation pipeline."""
    import numpy as np
    from edge.pipeline.identify import IMAGENET_MEAN, IMAGENET_STD, RECT_HEIGHT, RECT_WIDTH
    img = cv2.imread(row["orig_path"])
    if img is None:
        return None
    resized = cv2.resize(img, (RECT_WIDTH, RECT_HEIGHT))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - IMAGENET_MEAN) / IMAGENET_STD
    return torch.from_numpy(rgb.transpose(2, 0, 1)).float()


def main() -> int:
    if not WEIGHTS_PATH.exists():
        print(f"missing {WEIGHTS_PATH} -- run `python -m tools.train_identify_no_rect` first")
        return 1

    model = TripletEmbedder()
    model.load_state_dict(torch.load(WEIGHTS_PATH, map_location="cpu", weights_only=True))
    model.eval()

    rows = load_labelled("train")
    _, held_rows = held_out_identity_split(rows)
    print(f"held-out identities: {len({r['ind_id'] for r in held_rows})}, {len(held_rows)} images")

    embedded = embed_all_no_rect(held_rows, model)
    print(f"{len(embedded)} of {len(held_rows)} held-out images embedded")

    per_side = evaluate(embedded)
    print("\nNo-rectification fallback, held out by identity, per side:")
    print(f"{'side':6} {'entities':9} {'images':7} {'queries':8} "
          f"{'top-1 (95% CI)':22} {'top-5 (95% CI)':22} {'mAP (95% CI)':22}")
    for side in sorted(per_side):
        r = per_side[side]
        print(f"{side:6} {r['n_entities']:9} {r['n_images']:7} {r['n_queries']:8} "
              f"{str(r['top1']) + ' ' + str(r['top1_ci']):22} "
              f"{str(r['top5']) + ' ' + str(r['top5_ci']):22} "
              f"{str(r['mAP']) + ' ' + str(r['mAP_ci']):22}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
