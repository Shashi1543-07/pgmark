"""ATRW-specific data adapter. Converts the raw CSV/keypoint files under
data/raw/atrw/ into the named-keypoint format edge/pipeline/identify.py
expects, and builds the held-out-by-identity split used for training
and evaluation. See docs/DATA.md §§1-2, docs/MODEL_CHOICES.md.

edge/pipeline/identify.py has no idea this file, or ATRW, exists --
that separation is deliberate (see identify.py's module docstring).

**The official ATRW/CVWC2019 test split ships no identity labels.**
reid_list_test.csv is a bare filename list -- the ground truth was
withheld by the competition organizers for the leaderboard and was
never publicly released (confirmed against the CVWC2019 challenge
site). docs/DATA.md says to use the published splits so numbers are
"comparable to published work"; that is not achievable here, because
the comparison point does not exist outside the organizers' own
infrastructure. What IS achievable, and what this module builds
instead, is a held-out-by-identity split of the 1887 labelled images
in reid_list_train.csv -- the only portion of ATRW with real ground
truth. Report evaluation numbers as "held out by identity from ATRW's
labelled training data," never as "the ATRW test set."

**ATRW's keypoint order (0-indexed) maps 1:1 onto docs/DATA.md's
Table 4, plus one:**
    0 left_ear, 1 right_ear, 2 nose, 3 right_shoulder, 4 right_front_paw,
    5 left_shoulder, 6 left_front_paw, 7 right_hip, 8 right_knee,
    9 right_back_paw, 10 left_hip, 11 left_knee, 12 left_back_paw,
    13 tail, 14 center
Each keypoint is (x, y, v) in the flat 45-value array, COCO visibility
convention (0 unlabelled, 1 labelled-occluded, 2 labelled-visible).
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "raw" / "atrw"

KEYPOINT_NAMES = [
    "left_ear", "right_ear", "nose", "right_shoulder", "right_front_paw",
    "left_shoulder", "left_front_paw", "right_hip", "right_knee",
    "right_back_paw", "left_hip", "left_knee", "left_back_paw", "tail", "center",
]


def _to_named_keypoints(flat: list[float]) -> dict:
    return {name: (flat[3 * i], flat[3 * i + 1], flat[3 * i + 2])
            for i, name in enumerate(KEYPOINT_NAMES)}


def load_labelled(split: str = "train") -> list[dict]:
    """One row per labelled image: ind_id, file_name, orig_path,
    keypoints (named dict). `split` is 'train' -- the only ATRW portion
    with real identity labels; see the module docstring."""
    rows = list(csv.reader((ROOT / f"reid_list_{split}.csv").open()))
    kp_by_file = json.loads((ROOT / f"reid_keypoints_{split}.json").read_text())
    out = []
    for ind_id, file_name in rows:
        flat = kp_by_file.get(file_name)
        out.append(dict(
            ind_id=ind_id, file_name=file_name,
            orig_path=str(ROOT / split / file_name),
            keypoints=_to_named_keypoints(flat) if flat else {}))
    return out


def held_out_identity_split(rows: list[dict], held_out_fraction: float = 0.2,
                             seed: int = 20260813) -> tuple[list[dict], list[dict]]:
    """Splits by IDENTITY, not by image -- an embedder tested on
    identities it trained on measures memorisation, the exact mistake
    docs/DATA.md §4 warns against for Stage A and that applies just as
    much here. Returns (train_rows, held_out_rows); held-out identities
    never appear in train_rows at all."""
    ids = sorted({r["ind_id"] for r in rows})
    rng = random.Random(seed)
    rng.shuffle(ids)
    n_held = max(1, round(len(ids) * held_out_fraction))
    held_ids = set(ids[:n_held])
    train_rows = [r for r in rows if r["ind_id"] not in held_ids]
    held_rows = [r for r in rows if r["ind_id"] in held_ids]
    return train_rows, held_rows
