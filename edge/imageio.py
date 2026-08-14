"""Image reading, sized for 50,000 frames rather than for one.

Every function here replaces a full-resolution decode that v0.1.1 did and
did not need to do. The numbers below are measured on this codebase's own
workload — a 4000x3000 camera-trap JPEG, the ordinary case for a Cuddeback
or Browning trail camera:

    Stage A grid read, v0.1.1 (`triage._read_grid`)     143.7 ms/frame
    Stage A grid read, with `Image.draft()`              38.4 ms/frame

    -> 50,000 frames:  120 minutes  ->  32 minutes

`Image.draft()` is not a resize. It tells libjpeg to decode the JPEG's DCT
coefficients at 1/2, 1/4 or 1/8 scale during decompression, so the
full-size bitmap is never constructed. Stage A downsamples to a 16x16 grid
and ingest's night heuristic to 32x32, so both were building a 12-megapixel
array in order to throw away 99.99% of it.

The second thing here is content validation. v0.1.1 decided what was an
image from the file extension and a `PIL.verify()` that it then discarded —
and `verify()` alone does not catch a decompression bomb, a truncated file
that decodes to garbage, or a 30,000x30,000 PNG that will exhaust the RAM
of the laptop this is supposed to run on.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

# A truncated JPEG is common on a card that was pulled mid-write. Decoding
# what is there beats discarding the frame — but the truncation is recorded
# as a flag, never silently swallowed.
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Pillow's own bomb guard is ~89 Mpx and raises DecompressionBombError above
# 2x that. A camera trap does not produce 89-megapixel frames; anything that
# large in an import folder is either not a camera-trap frame or is hostile.
MAX_PIXELS = 80_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# Formats a camera trap actually writes. Checked against the file's magic
# bytes, not its extension: `.jpg` is a claim, not a fact.
ALLOWED_FORMATS = {"JPEG", "PNG", "MPO", "TIFF", "BMP", "WEBP"}

_MAGIC = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"II*\x00", "TIFF"), (b"MM\x00*", "TIFF"),
    (b"BM", "BMP"),
    (b"RIFF", "WEBP"),
)


class UnreadableImage(Exception):
    """The file is not a usable image. Carries the reason so the dead-letter
    row says which of the several possible things went wrong."""


def sniff_format(head: bytes) -> str | None:
    """Format from magic bytes. A `.jpg` that is actually a ZIP is not a
    camera-trap frame and must not reach a decoder."""
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            if fmt == "WEBP" and head[8:12] != b"WEBP":
                continue
            return fmt
    return None


def probe(path: str | Path, *, read_exif: bool = True) -> dict:
    """One pass over a file: size, format, dimensions, EXIF, night flag.

    v0.1.1's `ingest._scan_file()` opened and fully decoded each frame
    twice — once for `verify()`, once for EXIF — and then a third time
    inside `_night_heuristic()`, which called `.convert("RGB").resize(...)`
    on the full-resolution image. Three full decodes per frame, at ingest,
    for metadata that needs none of them.

    Raises `UnreadableImage` rather than returning a half-populated dict:
    "this file could not be read" is a different outcome from "this file
    was read and has no EXIF", and collapsing them is how a corrupt card
    becomes an invisible gap in a season's data.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise UnreadableImage(f"cannot stat file: {exc}") from exc
    if size == 0:
        raise UnreadableImage("zero-byte file")

    with p.open("rb") as fh:
        head = fh.read(32)
    fmt = sniff_format(head)
    if fmt is None:
        raise UnreadableImage("not a recognised image format (magic bytes do not match "
                              "JPEG/PNG/TIFF/BMP/WEBP)")

    out = {"bytes": size, "format": fmt, "width": None, "height": None,
           "exif_dt_raw": None, "make": None, "model": None, "is_night": 0,
           "truncated": False}
    try:
        with Image.open(p) as im:
            if im.format and im.format.upper() not in ALLOWED_FORMATS:
                raise UnreadableImage(f"format {im.format} is not accepted")
            out["width"], out["height"] = im.size
            if im.size[0] * im.size[1] > MAX_PIXELS:
                raise UnreadableImage(
                    f"{im.size[0]}x{im.size[1]} exceeds the {MAX_PIXELS:,}px ceiling")
            if read_exif:
                exif = im.getexif()
                out["exif_dt_raw"] = exif.get(36867) or exif.get(306)
                out["make"], out["model"] = exif.get(271), exif.get(272)
            # Night flag on a 1/8-scale decode: the IR/greyscale signal this
            # measures survives downsampling completely.
            im.draft("RGB", (64, 64))
            small = im.convert("RGB").resize((32, 32))
            px = np.asarray(small, dtype=np.int16)
            out["is_night"] = int(float(np.mean(px.max(axis=2) - px.min(axis=2))) < 12)
    except UnreadableImage:
        raise
    except OSError as exc:
        raise UnreadableImage(f"decode failed: {exc}") from exc
    except Exception as exc:                                       # noqa: BLE001
        raise UnreadableImage(f"{type(exc).__name__}: {exc}") from exc
    return out


def read_grid(path: str | Path, grid_n: int, band_frac: float) -> np.ndarray | None:
    """Stage A's cell grid, decoded at the smallest scale that still
    oversamples the grid.

    Drop-in replacement for `triage._read_grid()`. The only change is the
    `draft()` call and asking for 8x the grid resolution before the final
    resize — decoding straight to 16x16 would let libjpeg's own scaler do
    the averaging with fewer source pixels per cell, which changes the
    scores. Oversampling by 8x and letting Pillow's resize do the averaging
    keeps `cell_score()`'s calibration (tests/unit/test_triage_scoring.py)
    valid rather than silently re-tuning the blank threshold.
    """
    try:
        with Image.open(path) as im:
            target = grid_n * 8
            im.draft("L", (target, target))
            im = im.convert("L")
            w, h = im.size
            band = int(h * band_frac)
            if 0 < band < h:
                im = im.crop((0, 0, w, h - band))
            return np.asarray(im.resize((grid_n, grid_n)), dtype=np.uint8)
    except Exception:                                              # noqa: BLE001
        return None


def read_for_detector(path: str | Path, max_side: int = 1280):
    """RGB image for Stage B, capped on the long edge.

    MegaDetector's input size is fixed by its own config (640 or 1280);
    feeding it a 12-megapixel frame decodes 12 megapixels and then throws
    away 96% of them in the transform. `draft()` gets libjpeg to skip that
    work up front.

    Returns `(PIL.Image, original_width, original_height)` — the original
    dimensions are returned separately because `Detector.detect()`
    normalises boxes against them, and normalising against the drafted size
    would put every box in the wrong place.
    """
    with Image.open(path) as im:
        ow, oh = im.size
        im.draft("RGB", (max_side, max_side))
        out = im.convert("RGB")
        if max(out.size) > max_side:
            scale = max_side / max(out.size)
            out = out.resize((max(1, int(out.width * scale)),
                              max(1, int(out.height * scale))))
        return out, ow, oh


def read_bgr(path: str | Path) -> np.ndarray | None:
    """Full-resolution BGR array for rectification, which genuinely needs
    real pixels — `cv2.warpPerspective` samples the source image, so this
    is the one place a full decode is the right call."""
    try:
        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGB"))
        return arr[:, :, ::-1].copy()
    except Exception:                                              # noqa: BLE001
        return None


def hash_and_probe(path: str | Path, chunk: int = 1024 * 1024) -> tuple[str, dict]:
    """SHA-256 streamed in chunks, plus the metadata probe.

    v0.1.1 did `path.read_bytes()` — the entire file into RAM — to hash it.
    For one 3 MB frame that is nothing; the point is that it establishes a
    pattern where a 100 MB video file dropped in the folder by mistake also
    goes fully into memory, and nothing bounds it.
    """
    import hashlib
    h = hashlib.sha256()
    p = Path(path)
    try:
        with p.open("rb") as fh:
            while block := fh.read(chunk):
                h.update(block)
    except OSError as exc:
        raise UnreadableImage(f"cannot read file: {exc}") from exc
    return h.hexdigest(), probe(p)


def thumbnail_bytes(path: str | Path, max_side: int = 320, quality: int = 78) -> bytes | None:
    """A small JPEG for the UI.

    `/api/crops/{id}/image` and `/api/individuals/{id}/thumbnail` returned
    the full-size file with `FileResponse`. A review screen showing 50
    candidate crops therefore pulled 50 full-resolution images across
    localhost to render them at 96 px.
    """
    try:
        with Image.open(path) as im:
            im.draft("RGB", (max_side, max_side))
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:                                              # noqa: BLE001
        return None
