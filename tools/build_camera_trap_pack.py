"""Builds a single ready-to-test camera trap SD card pack for folder ingest testing.

Contains matched station subfolders corresponding to Pench Tiger Reserve stations:
  - PN-B-001/ : Real Tigers (Left & Right flanks of repeat individuals), Deer, and Blanks
  - PN-B-002/ : Real Tigers (Novel tigers + repeat encounters), Raccoons, and Blanks
  - PN-C-005/ : Non-target wildlife (Bobcats, Coyotes, Cattle), Human patrol, and Night Blanks
  - UNASSIGNED_TRAIL_09/ : Unmatched station folder to test dropdown station assignment in preflight

Run:
    python -m tools.build_camera_trap_pack
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
OUT = ROOT / "camera_trap_pack"


def _atrw_by_id(csv_path: Path) -> dict[str, list[str]]:
    by_id: dict[str, list[str]] = defaultdict(list)
    if not csv_path.exists():
        return by_id
    with open(csv_path, newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                by_id[row[0].strip()].append(row[1].strip())
    return by_id


def main() -> None:
    print("=" * 68)
    print("  BUILDING CAMERA TRAP SD CARD TEST PACK")
    print("=" * 68)

    if not ATRW.exists() or not CCT20.exists():
        sys.exit("Error: Datasets not found in data/raw/")

    if OUT.exists():
        shutil.rmtree(OUT)

    # Station folders matching Pench station IDs in repo
    stations = {
        "PN-B-001": OUT / "PN-B-001",
        "PN-B-002": OUT / "PN-B-002",
        "PN-C-005": OUT / "PN-C-005",
        "UNASSIGNED_TRAIL_09": OUT / "UNASSIGNED_TRAIL_09",
    }
    for d in stations.values():
        d.mkdir(parents=True, exist_ok=True)

    # Load ATRW tigers
    train_by_id = _atrw_by_id(ATRW / "reid_list_train.csv")
    tiger_ids = [iid for iid, fns in train_by_id.items() if len(fns) >= 3]

    # Load CCT20 images
    ann_file = CCT20 / "eccv_18_annotation_files" / "cis_val_annotations.json"
    cct_data = json.loads(ann_file.read_text(encoding="utf-8")) if ann_file.exists() else {}
    cat_names = {c["id"]: c["name"].lower() for c in cct_data.get("categories", [])}
    img_map = {im["id"]: im["file_name"] for im in cct_data.get("images", [])}
    by_cat = defaultdict(list)
    for ann in cct_data.get("annotations", []):
        cat = cat_names.get(ann.get("category_id"), "empty")
        fn = img_map.get(ann.get("image_id"))
        if fn and (CCT20 / "eccv_18_all_images_sm" / fn).exists():
            by_cat[cat].append(fn)

    img_dir = CCT20 / "eccv_18_all_images_sm"

    # 1. Fill Station PN-B-001 (Repeat Tigers + Deer + Blanks)
    print("  [+] Packing PN-B-001 (Tigers + Deer + Blanks)...")
    # Tiger 1 (Left & Right Flanks)
    t1_files = train_by_id[tiger_ids[0]][:4] if tiger_ids else []
    for i, fn in enumerate(t1_files, 1):
        src = (ATRW / "train" / fn) if (ATRW / "train" / fn).exists() else (ATRW / "test" / fn)
        if src.exists():
            shutil.copy(src, stations["PN-B-001"] / f"DSCF_010{i}_TIGER_L_R.jpg")

    # Deer
    for i, fn in enumerate(by_cat.get("deer", [])[:4], 1):
        shutil.copy(img_dir / fn, stations["PN-B-001"] / f"DSCF_011{i}_DEER.jpg")

    # Blanks
    for i, fn in enumerate(by_cat.get("empty", [])[:6], 1):
        shutil.copy(img_dir / fn, stations["PN-B-001"] / f"DSCF_012{i}_BLANK.jpg")

    # 2. Fill Station PN-B-002 (Novel Tiger + Repeat Tigers + Blanks)
    print("  [+] Packing PN-B-002 (Novel Tiger + Repeat encounters + Blanks)...")
    if len(tiger_ids) >= 3:
        for i, fn in enumerate(train_by_id[tiger_ids[1]][:3], 1):
            src = ATRW / "train" / fn
            if src.exists():
                shutil.copy(src, stations["PN-B-002"] / f"DSCF_020{i}_TIGER_REPEAT.jpg")
        for i, fn in enumerate(train_by_id[tiger_ids[2]][:2], 1):
            src = ATRW / "train" / fn
            if src.exists():
                shutil.copy(src, stations["PN-B-002"] / f"DSCF_021{i}_TIGER_NOVEL.jpg")

    for i, fn in enumerate(by_cat.get("raccoon", [])[:3] + by_cat.get("coyote", [])[:2], 1):
        shutil.copy(img_dir / fn, stations["PN-B-002"] / f"DSCF_022{i}_WILDLIFE.jpg")

    for i, fn in enumerate(by_cat.get("empty", [])[10:16], 1):
        shutil.copy(img_dir / fn, stations["PN-B-002"] / f"DSCF_023{i}_BLANK.jpg")

    # 3. Fill Station PN-C-005 (Bobcats, Cattle, Human Patrol, Blanks)
    print("  [+] Packing PN-C-005 (Bobcats, Human Patrol, Blanks)...")
    for i, fn in enumerate(by_cat.get("bobcat", [])[:4] + by_cat.get("cattle", [])[:3], 1):
        shutil.copy(img_dir / fn, stations["PN-C-005"] / f"DSCF_030{i}_ANIMALS.jpg")

    for i, fn in enumerate(by_cat.get("human", [])[:3] + by_cat.get("vehicle", [])[:2], 1):
        shutil.copy(img_dir / fn, stations["PN-C-005"] / f"DSCF_031{i}_PRIVACY_PATROL.jpg")

    for i, fn in enumerate(by_cat.get("empty", [])[20:26], 1):
        shutil.copy(img_dir / fn, stations["PN-C-005"] / f"DSCF_032{i}_BLANK.jpg")

    # 4. Fill UNASSIGNED_TRAIL_09
    print("  [+] Packing UNASSIGNED_TRAIL_09 (Unassigned trail)...")
    if len(tiger_ids) >= 4:
        for i, fn in enumerate(train_by_id[tiger_ids[3]][:2], 1):
            src = ATRW / "train" / fn
            if src.exists():
                shutil.copy(src, stations["UNASSIGNED_TRAIL_09"] / f"DSCF_090{i}_TIGER.jpg")
    for i, fn in enumerate(by_cat.get("empty", [])[30:35], 1):
        shutil.copy(img_dir / fn, stations["UNASSIGNED_TRAIL_09"] / f"DSCF_091{i}_BLANK.jpg")

    total_files = sum(len(list(d.glob("*.jpg"))) for d in stations.values())
    print("=" * 68)
    print(f"  READY! {total_files} images packed into:")
    print(f"  -> {OUT.resolve()}")
    print("=" * 68)


if __name__ == "__main__":
    main()
