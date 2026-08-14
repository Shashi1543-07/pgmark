"""A controlled corpus for the motion prefilter: a static background
repeated, with two frames carrying an obvious injected subject, so "blank
detection" is a checkable claim against known ground truth rather than
asserted against noise. See blueprint §6.

    from tests.fixtures.triage_corpus import build
    build(root, station_folder="...", station_activity_start=...)
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

# index -> has a subject. Index 0 exercises "no background yet"; the rest
# exercise blank-vs-subject once a running background exists.
FRAMES = [False, False, False, True, False, False, True, False, False, False]


def _frame(bg_color: tuple, subject: bool, exif_dt: datetime) -> bytes:
    img = Image.new("RGB", (128, 96), bg_color)
    if subject:
        ImageDraw.Draw(img).ellipse((30, 20, 90, 70), fill=(200, 40, 40))
    exif = Image.Exif()
    exif[36867] = exif_dt.strftime("%Y:%m:%d %H:%M:%S")   # DateTimeOriginal -- and
    # incidentally the reason every "blank" frame isn't byte-identical to
    # every other one: without this, content-addressed dedupe (working
    # exactly as ingest intends) collapses all of them into a single row,
    # and there is nothing left for a running background to compare against.
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, exif=exif)
    return buf.getvalue()


def build(root: Path, station_folder: str, station_activity_start: datetime) -> dict:
    folder = root / station_folder
    folder.mkdir(parents=True, exist_ok=True)
    bg = (90, 110, 90)
    base = station_activity_start + timedelta(days=3)
    for i, has_subject in enumerate(FRAMES):
        when = base + timedelta(minutes=i * 5)
        (folder / f"IMG_{i:02d}.jpg").write_bytes(_frame(bg, has_subject, when))
    return {"folder": station_folder, "frames": len(FRAMES),
            "subject_indices": [i for i, s in enumerate(FRAMES) if s],
            "blank_indices": [i for i, s in enumerate(FRAMES) if not s]}
