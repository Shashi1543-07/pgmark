"""Build a strictly local L/R/UNKNOWN flank-side training manifest from ATRW.

ATRW's full 15-point skeleton (not just shoulder+hip) makes it useful for
the side classifier's ground truth. No file is downloaded or copied: the
generated JSONL only points at already-local ATRW images.

Emits all three classes -- L, R, and UNKNOWN -- not just L/R. An earlier
version of this script dropped anything infer_ground_truth_side() could
not call, on the reasoning that ATRW "cannot supply UNKNOWN examples."
That reasoning does not hold: ATRW's own oblique, near-frontal, and
occluded-pose images ARE genuine UNKNOWN examples, and a side classifier
trained on L/R alone has no way to ever say "not a clean profile" -- it
would be structurally forced to guess between the two even when neither
is visible. tools.train_classifiers._require_all_labels() already
requires all three classes to be present in both splits for exactly this
reason; the old dropped-UNKNOWN output could never satisfy that on its
own, which is why the shipped manifest turned out to have been built some
other, undocumented way. This is now the one correct, reproducible path
docs/STAGE2_MODEL_WORKFLOW.md describes.

    python -m tools.build_atrw_side_manifest

The resulting ``data/raw/atrw_side/manifest.jsonl`` is input to
``python -m tools.train_classifiers --task side --manifest ...``.
"""
from __future__ import annotations

import json
import argparse
from pathlib import Path

from tools.atrw_dataset import held_out_identity_split, infer_ground_truth_side, load_labelled

OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "atrw_side" / "manifest.jsonl"


def rows_for_split(rows: list[dict], split: str) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not row["keypoints"]:
            continue
        side = infer_ground_truth_side(row["keypoints"])
        tag = "profile" if side in ("L", "R") else "non_profile_or_ambiguous"
        out.append({"path": str(Path(row["orig_path"]).resolve()), "side": side,
                    "split": split, "source": "ATRW", "ind_id": row["ind_id"],
                    "challenge_tags": [tag]})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT,
                        help="local JSONL output path (default: data/raw/atrw_side/manifest.jsonl)")
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        rows = load_labelled("train")
    except FileNotFoundError as exc:
        print(f"missing local ATRW re-id dataset: {exc}")
        return 1
    train_rows, val_rows = held_out_identity_split(rows, held_out_fraction=0.15)
    payload = rows_for_split(train_rows, "train") + rows_for_split(val_rows, "val")
    if not payload:
        print("no keypoint-labelled ATRW rows found; stage the local ATRW re-id dataset first")
        return 1
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in payload),
                          encoding="utf-8")
    except OSError as exc:
        print(f"cannot write local side manifest {output}: {exc}")
        return 1
    counts = {}
    for r in payload:
        counts[(r["split"], r["side"])] = counts.get((r["split"], r["side"]), 0) + 1
    print(f"wrote {output}")
    for split in ("train", "val"):
        print(f"  {split}: " + ", ".join(f"{side}={counts.get((split, side), 0)}"
                                          for side in ("L", "R", "UNKNOWN")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
