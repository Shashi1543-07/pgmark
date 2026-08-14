"""Builds a second, larger manual-testing folder -- 100 real photos, 20 of
them tigers, 80 blank. Same sources and same reasoning as
tools/build_manual_test_set.py (see that module's docstring for the full
rationale); this one just scales the mix differently, as its own folder
so it doesn't overwrite the first.

  TIGER_PHOTOS  -- 20 real tigers from ATRW's train split, spread across
                   16 individuals (reid_list_train.csv gives real
                   identity ground truth). Four of them get 2 photos each
                   so a repeat visit is still testable at this size, not
                   just one-off enrolment.
  BLANK_FRAMES  -- 80 real empty camera-trap frames, Caltech Camera
                   Traps (CCT20)'s "empty" category.

Nothing here touches edge/db/repo.py or the running database -- it only
copies files. See docs/DATA.md for what ATRW and CCT20 each are and are
not licensed for; this folder is for local manual testing, not for
redistribution.

    python -m tools.build_manual_test_100
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATRW = ROOT / "data" / "raw" / "atrw"
CCT20 = ROOT / "data" / "raw" / "cct20"
OUT = ROOT / "manual_test_100"

# 4 individuals x 2 photos (repeat-visitor story) + 12 individuals x 1
# photo (one-off story) = 20, picked from ATRW's own best-represented
# individuals so there's no risk of running short.
TIGER_PICKS = [
    ("153", 2), ("160", 2), ("154", 2), ("246", 2),
    ("243", 1), ("136", 1), ("265", 1), ("237", 1), ("249", 1), ("247", 1),
    ("172", 1), ("168", 1), ("261", 1), ("268", 1), ("264", 1), ("244", 1),
]
N_BLANK = 80


def _atrw_by_id(csv_path: Path) -> dict[str, list[str]]:
    by_id: dict[str, list[str]] = defaultdict(list)
    with open(csv_path, newline="") as f:
        for iid, fn in csv.reader(f):
            by_id[iid].append(fn)
    return by_id


def _cct20_annotations() -> dict:
    ann_path = CCT20 / "eccv_18_annotation_files" / "cis_val_annotations.json"
    return json.loads(ann_path.read_text())


def main() -> None:
    if not ATRW.exists() or not CCT20.exists():
        sys.exit(f"expected data at {ATRW} and {CCT20} -- run tools/fetch_data.py first")

    if OUT.exists():
        shutil.rmtree(OUT)
    tiger_dir, blank_dir = OUT / "TIGER_PHOTOS", OUT / "BLANK_FRAMES"
    for d in (tiger_dir, blank_dir):
        d.mkdir(parents=True)

    # ── tigers, with real identity ground truth ─────────────────────────
    by_id = _atrw_by_id(ATRW / "reid_list_train.csv")
    manifest_lines = []
    letters = "ABCDEFGHIJKLMNOP"
    for (iid, n), label in zip(TIGER_PICKS, letters):
        files = by_id[iid][:n]
        for i, fn in enumerate(files, 1):
            dest_name = f"tiger_{label}_{i}.jpg" if n > 1 else f"tiger_{label}.jpg"
            shutil.copy(ATRW / "train" / fn, tiger_dir / dest_name)
        manifest_lines.append(
            f"  tiger_{label}_*  -- {n} real photo(s) of the same individual (ATRW id {iid})")

    # ── blanks, real camera-trap empties ─────────────────────────────────
    d = _cct20_annotations()
    empty_cat = next(c["id"] for c in d["categories"] if c["name"] == "empty")
    img_by_id = {im["id"]: im for im in d["images"]}
    n = 0
    for ann in d["annotations"]:
        if ann["category_id"] != empty_cat:
            continue
        fn = img_by_id[ann["image_id"]]["file_name"]
        src = CCT20 / "eccv_18_all_images_sm" / fn
        if not src.exists():
            continue
        n += 1
        shutil.copy(src, blank_dir / f"blank_{n:02d}.jpg")
        if n >= N_BLANK:
            break

    print(f"built {OUT}")
    print(f"  TIGER_PHOTOS/  {len(list(tiger_dir.iterdir()))} files")
    for line in manifest_lines:
        print(line)
    print(f"  BLANK_FRAMES/ {len(list(blank_dir.iterdir()))} files")
    print(f"  total: {len(list(tiger_dir.iterdir())) + len(list(blank_dir.iterdir()))} files")


if __name__ == "__main__":
    main()
