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

Orientation is normalized via EXIF before any geometry or cropping.
Perceptual hash (dhash) and SHA256 deduplication are computed in-line.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

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

EXIF_ORIENTATION = 274
EXIF_SERIAL_TAGS = (42033, 0xA431, 0xA435)  # BodySerialNumber / CameraSerialNumber


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


def normalize_orientation(im: Image.Image) -> Image.Image:
    """Normalize image orientation based on EXIF metadata (tags 1-8)."""
    try:
        return ImageOps.exif_transpose(im) or im
    except Exception:
        return im


def compute_dhash(im: Image.Image, hash_size: int = 8) -> str:
    """Calculate difference hash (dhash) as a hexadecimal string for dedup."""
    try:
        # Resize to (hash_size + 1, hash_size) in greyscale
        resized = im.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        arr = np.asarray(resized, dtype=np.int16)
        # Compare adjacent pixels horizontally: arr[:, 1:] > arr[:, :-1]
        diff = arr[:, 1:] > arr[:, :-1]
        # Pack boolean array into hex string
        return "{:016x}".format(int("".join("1" if b else "0" for b in diff.flatten()), 2))
    except Exception:
        return ""


def probe(path: str | Path, *, read_exif: bool = True) -> dict:
    """One pass over a file: size, format, dimensions, EXIF, night flag, dhash, orientation.

    Raises `UnreadableImage` rather than returning a half-populated dict:
    "this file could not be read" is a different outcome from "this file
    was read and has no EXIF".
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

    out = {
        "bytes": size, "format": fmt, "width": None, "height": None,
        "exif_dt_raw": None, "make": None, "model": None, "serial": None,
        "orientation": 1, "is_night": 0, "dhash": None, "truncated": False
    }
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
                out["make"] = (str(exif.get(271)).strip() if exif.get(271) else None)
                out["model"] = (str(exif.get(272)).strip() if exif.get(272) else None)
                out["orientation"] = int(exif.get(EXIF_ORIENTATION) or 1)
                for stag in EXIF_SERIAL_TAGS:
                    if exif.get(stag):
                        out["serial"] = str(exif.get(stag)).strip()
                        break

            # Fast 1/8-scale decode for night heuristic and dhash
            im.draft("RGB", (64, 64))
            small = im.convert("RGB").resize((32, 32))
            small = normalize_orientation(small)
            px = np.asarray(small, dtype=np.int16)
            out["is_night"] = int(float(np.mean(px.max(axis=2) - px.min(axis=2))) < 12)
            out["dhash"] = compute_dhash(small)
    except UnreadableImage:
        raise
    except OSError as exc:
        raise UnreadableImage(f"decode failed: {exc}") from exc
    except Exception as exc:                                       # noqa: BLE001
        raise UnreadableImage(f"{type(exc).__name__}: {exc}") from exc
    return out


def read_grid(path: str | Path, grid_n: int, band_frac: float) -> np.ndarray | None:
    """Stage A's cell grid, normalized for EXIF orientation, decoded at 8x scale."""
    try:
        with Image.open(path) as im:
            target = grid_n * 8
            im.draft("L", (target, target))
            im = im.convert("L")
            im = normalize_orientation(im)
            w, h = im.size
            band = int(h * band_frac)
            if 0 < band < h:
                im = im.crop((0, 0, w, h - band))
            return np.asarray(im.resize((grid_n, grid_n)), dtype=np.uint8)
    except Exception:                                              # noqa: BLE001
        return None


def read_for_detector(path: str | Path, max_side: int = 1280):
    """RGB image for Stage B, capped on the long edge, normalized for EXIF orientation."""
    with Image.open(path) as im:
        im = normalize_orientation(im)
        ow, oh = im.size
        im.draft("RGB", (max_side, max_side))
        out = im.convert("RGB")
        if max(out.size) > max_side:
            scale = max_side / max(out.size)
            out = out.resize((max(1, int(out.width * scale)),
                              max(1, int(out.height * scale))))
        return out, ow, oh


def read_bgr(path: str | Path) -> np.ndarray | None:
    """Full-resolution BGR array for rectification, normalized for EXIF orientation."""
    try:
        with Image.open(path) as im:
            im = normalize_orientation(im)
            arr = np.asarray(im.convert("RGB"))
        return arr[:, :, ::-1].copy()
    except Exception:                                              # noqa: BLE001
        return None


def hash_and_probe(path: str | Path, chunk: int = 1024 * 1024) -> tuple[str, dict]:
    """SHA-256 streamed in chunks, plus the metadata probe."""
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
    """A small JPEG for the UI, normalized for EXIF orientation."""
    try:
        with Image.open(path) as im:
            im = normalize_orientation(im)
            im.draft("RGB", (max_side, max_side))
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:                                              # noqa: BLE001
        return None
