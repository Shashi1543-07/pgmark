"""Downloads external datasets and model weights this project names, into
data/raw/ (datasets, evaluation-only) or data/weights/ (production model
weights edge/ actually loads at runtime).

    python -m tools.fetch_data --set cct20         # Caltech CT benchmark subset (blank detection)
    python -m tools.fetch_data --set atrw           # ATRW re-ID + pose (identification)
    python -m tools.fetch_data --set megadetector    # Stage B detector weights (MIT-licensed)
    python -m tools.fetch_data --set all

Idempotent: a file already present at the expected size is not re-fetched.
Nothing here trains anything -- this only gets bytes onto disk. data/raw/
and data/weights/ are both gitignored. See docs/DATA.md and
docs/MODEL_CHOICES.md for what each source is (and is not) for, and its
licence.
"""
from __future__ import annotations

import argparse
import sys
import tarfile
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
WEIGHTS = Path(__file__).resolve().parents[1] / "data" / "weights"

# (url, expected filename, expected size in bytes or None if unknown/small)
CCT20 = [
    ("https://storage.googleapis.com/public-datasets-lila/caltechcameratraps/"
     "eccv_18_annotations.tar.gz", "eccv_18_annotations.tar.gz", 3_100_000),
    ("https://storage.googleapis.com/public-datasets-lila/caltechcameratraps/"
     "eccv_18_all_images_sm.tar.gz", "eccv_18_all_images_sm.tar.gz", 6_492_615_601),
]

ATRW = [
    ("https://storage.googleapis.com/public-datasets-lila/cvwc2019/train/"
     "atrw_reid_train.tar.gz", "atrw_reid_train.tar.gz", 138_885_317),
    ("https://storage.googleapis.com/public-datasets-lila/cvwc2019/train/"
     "atrw_anno_reid_train.tar.gz", "atrw_anno_reid_train.tar.gz", 98_982),
    ("https://storage.googleapis.com/public-datasets-lila/cvwc2019/test/"
     "atrw_reid_test.tar.gz", "atrw_reid_test.tar.gz", 96_857_922),
    ("https://storage.googleapis.com/public-datasets-lila/cvwc2019/test/"
     "atrw_anno_reid_test.tar.gz", "atrw_anno_reid_test.tar.gz", 89_138),
    ("https://storage.googleapis.com/public-datasets-lila/cvwc2019/train/"
     "atrw_anno_pose_train.tar.gz", "atrw_anno_pose_train.tar.gz", 568_505),
]

# MDV6-mit-yolov9-c: MIT-licensed, compact (9.7M params) MegaDetector V6
# variant. NOT the YOLOv10-compact build (2.3M params, AGPL-3.0) -- see
# docs/MODEL_CHOICES.md for why the license constraint won out over the
# smaller size. No ultralytics/AGPL code involved in running these files;
# see edge/pipeline/vendor/yolo_mit/NOTICE.md.
MEGADETECTOR = [
    ("https://zenodo.org/records/15398270/files/MDV6-mit-yolov9-c.ckpt?download=1",
     "MDV6-mit-yolov9-c.ckpt", None),
    ("https://zenodo.org/records/15178680/files/config_v9s.yaml?download=1",
     "config_v9s.yaml", None),
]


def _fetch(url: str, dest: Path, expect_bytes: int | None) -> None:
    if dest.exists() and (expect_bytes is None or abs(dest.stat().st_size - expect_bytes) < 1_000_000):
        print(f"  have {dest.name} ({dest.stat().st_size:,} bytes)")
        return
    print(f"  fetching {dest.name} from {url}")

    def _progress(count, block_size, total_size):
        done = count * block_size
        if total_size > 0 and count % 200 == 0:
            print(f"    {done / 1e6:8.1f} / {total_size / 1e6:.1f} MB", end="\r")

    urllib.request.urlretrieve(url, dest, reporthook=_progress)
    print(f"    done: {dest.stat().st_size:,} bytes" + " " * 20)


def _extract(dest: Path, into: Path) -> None:
    """One marker per archive, not per destination directory -- several
    archives share the same `into` (e.g. every ATRW tarball extracts into
    data/raw/atrw/), so a shared marker would make extracting the first
    one look like the rest were already done too."""
    into.mkdir(parents=True, exist_ok=True)
    marker = into / f".extracted_{dest.stem}"
    if marker.exists():
        print(f"  {dest.name} already extracted")
        return
    print(f"  extracting {dest.name} -> {into}/")
    with tarfile.open(dest) as tf:
        tf.extractall(into)
    marker.write_text("ok")


def fetch_cct20() -> None:
    root = RAW / "cct20"
    root.mkdir(parents=True, exist_ok=True)
    for url, name, size in CCT20:
        dest = root / name
        _fetch(url, dest, size)
        _extract(dest, root)


def fetch_atrw() -> None:
    root = RAW / "atrw"
    root.mkdir(parents=True, exist_ok=True)
    for url, name, size in ATRW:
        dest = root / name
        _fetch(url, dest, size)
        _extract(dest, root)


def fetch_megadetector() -> None:
    root = WEIGHTS / "megadetector"
    root.mkdir(parents=True, exist_ok=True)
    for url, name, size in MEGADETECTOR:
        _fetch(url, root / name, size)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["cct20", "atrw", "megadetector", "all"], required=True)
    args = ap.parse_args()
    if args.set in ("cct20", "all"):
        print("Caltech Camera Traps (CCT20 benchmark subset) -- blank detection")
        fetch_cct20()
    if args.set in ("atrw", "all"):
        print("ATRW -- re-identification")
        fetch_atrw()
    if args.set in ("megadetector", "all"):
        print("MegaDetector V6 (MDV6-mit-yolov9-c) -- Stage B detector")
        fetch_megadetector()
    return 0


if __name__ == "__main__":
    sys.exit(main())
