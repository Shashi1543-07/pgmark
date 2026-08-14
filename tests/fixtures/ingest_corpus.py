"""A deterministic messy-input corpus for ingest tests. See blueprint §13
Layer 2: "Commit fixtures containing, deliberately..." -- this commits the
*generator* rather than binary JPEGs, so the corpus is reviewable as code
and reproduced byte-for-byte on every run instead of living as opaque
blobs in git.

    from tests.fixtures.ingest_corpus import build
    build(root, station_folder="...", station_activity_start=...)
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image


def _jpeg_bytes(color: tuple, exif_dt: datetime | None = None,
                make: str | None = None, model: str | None = None) -> bytes:
    img = Image.new("RGB", (64, 48), color)
    exif = Image.Exif()
    if exif_dt is not None:
        exif[36867] = exif_dt.strftime("%Y:%m:%d %H:%M:%S")   # DateTimeOriginal
    if make:
        exif[271] = make        # Make
    if model:
        exif[272] = model       # Model
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def build(root: Path, station_folder: str, station_activity_start: datetime) -> dict:
    """Writes three folders under `root` and returns a manifest of what
    should be true about each, for the test to assert against:

      UNSORTED_CARD  -- zero-byte file, truncated JPEG, a stray .txt, and
                         a valid image under a unicode filename. Matches no
                         station -- also exercises the unmatched-folder path.
      MIXED_BODIES   -- two valid images, two different camera bodies.
                         Matches no station either.
      <station_folder> -- a real burst (3 frames, 2s apart -> one event), a
                         second burst 20 minutes later (-> a second event),
                         a frame with an implausible EXIF year (drift
                         correction anchored to station_activity_start), a
                         frame with no EXIF but a filename timestamp (tier
                         3), a frame with neither (tier 4, interpolated),
                         and a byte-identical duplicate of the first frame.
    """
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"unsorted": "UNSORTED_CARD", "mixed": "MIXED_BODIES", "good": station_folder}

    unsorted = root / "UNSORTED_CARD"
    unsorted.mkdir()
    (unsorted / "zero_byte.jpg").write_bytes(b"")
    (unsorted / "truncated.jpg").write_bytes(b"\xff\xd8\xff\xe0not really a jpeg")
    (unsorted / "notes.txt").write_text("field notes, not an image", encoding="utf-8")
    (unsorted / "कैमरे.jpg").write_bytes(_jpeg_bytes((10, 10, 10)))

    mixed = root / "MIXED_BODIES"
    mixed.mkdir()
    mixed_at = station_activity_start + timedelta(days=5)
    (mixed / "a.jpg").write_bytes(
        _jpeg_bytes((50, 60, 70), exif_dt=mixed_at, make="Reconyx", model="HC600"))
    (mixed / "b.jpg").write_bytes(
        _jpeg_bytes((50, 60, 70), exif_dt=mixed_at + timedelta(hours=1),
                    make="Bushnell", model="Core"))

    good = root / station_folder
    good.mkdir()
    base = station_activity_start + timedelta(days=10)
    for i in range(3):                          # burst 1: one event
        (good / f"IMG_{i:04d}.jpg").write_bytes(_jpeg_bytes(
            (80, 80, 80), exif_dt=base + timedelta(seconds=i * 2),
            make="Reconyx", model="HC600"))
    for i in range(2):                           # burst 2, 20 min later: a second event
        (good / f"IMG_01{i:02d}.jpg").write_bytes(_jpeg_bytes(
            (80, 80, 80), exif_dt=base + timedelta(minutes=20, seconds=i * 2),
            make="Reconyx", model="HC600"))
    (good / "IMG_0200.jpg").write_bytes(_jpeg_bytes(                 # reset clock
        (80, 80, 80), exif_dt=datetime(2002, 1, 1, tzinfo=timezone.utc),
        make="Reconyx", model="HC600"))
    fn_ts = base + timedelta(hours=1)
    (good / f"{fn_ts:%Y%m%d_%H%M%S}.jpg").write_bytes(_jpeg_bytes((80, 80, 80)))  # tier 3
    (good / "IMG_0050.jpg").write_bytes(_jpeg_bytes((79, 79, 79)))    # tier 4: interpolated
    # (a distinct colour from the tier-3 frame above -- both carry no EXIF at
    # all, and identical pixels would make them byte-identical duplicates,
    # collapsing one of the two tiers this fixture exists to exercise)
    (good / "IMG_0000_copy.jpg").write_bytes(_jpeg_bytes(             # duplicate of frame 0
        (80, 80, 80), exif_dt=base, make="Reconyx", model="HC600"))

    return manifest
