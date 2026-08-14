"""Builds a small, real-photo folder for manually testing the software by
hand -- not a fixture the automated suites depend on. Pulls from data
already on disk (fetched by tools/fetch_data.py):

  TIGER_PHOTOS   -- real tigers, from ATRW's train split (reid_list_train.csv
                    gives real individual-ID ground truth, so pairs of
                    photos of the SAME tiger can be told apart from six
                    photos of six different tigers -- useful for testing
                    whether a second photo of a tiger already in the
                    catalogue gets recognised, not just whether any photo
                    gets enrolled).
  BLANK_FRAMES   -- real camera-trap frames with nothing in them, from
                    Caltech Camera Traps (CCT20)'s "empty" category --
                    genuine night-IR and daylight blanks, not synthetic
                    noise.
  OTHER_ANIMALS  -- real camera-trap frames of non-tiger species (CCT20
                    again). Not something the brief asked for, but a
                    meaningful edge case: Stage B's detector recognises
                    "animal" generically, not species, so these should
                    reach the flank/embedding stage and then be refused
                    there, not silently treated as a tiger.

Nothing here touches edge/db/repo.py or the running database -- it only
copies files. See docs/DATA.md for what ATRW and CCT20 each are and are
not licensed for; this folder is for local manual testing, not for
redistribution.

    python -m tools.build_manual_test_set
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
OUT = ROOT / "manual_test_photos"

# (individual_id, how many of their photos to take) -- picked to give both
# a repeat-visitor story (same tiger, multiple photos) and a one-off story
# (a tiger the catalogue has never seen).
TIGER_PICKS = [("237", 3), ("249", 3), ("153", 3), ("136", 1), ("265", 1), ("247", 1)]
N_BLANK = 12
OTHER_SPECIES = ["coyote", "bobcat", "deer", "raccoon"]


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
    tiger_dir, blank_dir, other_dir = (OUT / "TIGER_PHOTOS", OUT / "BLANK_FRAMES",
                                        OUT / "OTHER_ANIMALS")
    for d in (tiger_dir, blank_dir, other_dir):
        d.mkdir(parents=True)

    # ── tigers, with real identity ground truth ─────────────────────────
    by_id = _atrw_by_id(ATRW / "reid_list_train.csv")
    manifest_lines = []
    letters = iter("ABCDEF")
    for iid, n in TIGER_PICKS:
        label = next(letters)
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

    # ── non-tiger animals, real species, none of them big cats ──────────
    by_cat: dict[int, list[int]] = defaultdict(list)
    for ann in d["annotations"]:
        by_cat[ann["category_id"]].append(ann["image_id"])
    for species in OTHER_SPECIES:
        cat_id = next((c["id"] for c in d["categories"] if c["name"] == species), None)
        if cat_id is None:
            continue
        for image_id in by_cat[cat_id]:
            fn = img_by_id[image_id]["file_name"]
            src = CCT20 / "eccv_18_all_images_sm" / fn
            if src.exists():
                shutil.copy(src, other_dir / f"{species}.jpg")
                break

    print(f"built {OUT}")
    print(f"  TIGER_PHOTOS/  {len(list(tiger_dir.iterdir()))} files")
    for line in manifest_lines:
        print(line)
    print(f"  BLANK_FRAMES/  {len(list(blank_dir.iterdir()))} files")
    print(f"  OTHER_ANIMALS/ {len(list(other_dir.iterdir()))} files")


if __name__ == "__main__":
    main()
