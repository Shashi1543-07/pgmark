"""Build the offline basemap tile pyramid.

This is a BUILD-TIME tool and the only thing in this repository that is
allowed to touch the network. It runs on a developer machine with internet,
downloads satellite imagery for the Pench bounding box once, and writes it
into `edge/ui/tiles/{z}/{x}/{y}.jpg` -- an ordinary XYZ layout that Leaflet
reads straight off the local disk.

The deployed edge node never runs this. It ships the tiles that came out of
it, which is the entire point: the range office laptop has no internet, and
a grey map at the demo is a lost hackathon (CLAUDE.md rule 3, BLUEPRINT.md
sec 8 "the offline map trap").

    python -m tools.fetch_basemap_tiles              # z10-z14, the shipped set
    python -m tools.fetch_basemap_tiles --max-zoom 15   # sharper, +51 MB

Why z10-z14: z14 is ~9.5 m/pixel at this latitude, enough to read rivers,
roads and forest texture around a camera station. z15 alone would quadruple
the payload to ~69 MB for detail nobody needs at reserve scale, so Leaflet
is configured to over-zoom past z14 from the cached tiles instead
(`maxNativeZoom`), which stays sharp enough and costs nothing.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1] / "edge" / "ui"
TILE_DIR = UI_DIR / "tiles"
MANIFEST = UI_DIR / "img" / "basemap-pench.json"

# Esri World Imagery. Note the {z}/{y}/{x} order -- it is NOT the {z}/{x}/{y}
# that the on-disk layout and Leaflet's URL template both use.
SOURCE = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
          "World_Imagery/MapServer/tile/{z}/{y}/{x}")
ATTRIBUTION = "Imagery © Esri, Maxar, Earthstar Geographics"

# a margin around the reserve so panning does not immediately hit a void
PAD_DEG = 0.06


def deg2tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def _fetch(url: str, dest: Path, retries: int = 3) -> str:
    """Returns 'ok', 'cached' or 'fail'. Never raises: one dead tile must not
    take down a 1100-tile build."""
    if dest.exists() and dest.stat().st_size > 0:
        return "cached"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pugmark-basemap-build/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status != 200:
                    raise urllib.error.HTTPError(url, r.status, "bad status", None, None)
                blob = r.read()
            if not blob:
                raise ValueError("empty body")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(blob)
            tmp.replace(dest)
            return "ok"
        except Exception:
            if attempt == retries - 1:
                return "fail"
            time.sleep(0.6 * (attempt + 1))
    return "fail"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-zoom", type=int, default=10)
    ap.add_argument("--max-zoom", type=int, default=14)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"missing {MANIFEST} -- it defines the bounding box to fetch", file=sys.stderr)
        return 1
    meta = json.loads(MANIFEST.read_text(encoding="utf-8"))

    north = meta["north"] + PAD_DEG
    south = meta["south"] - PAD_DEG
    west = meta["west"] - PAD_DEG
    east = meta["east"] + PAD_DEG

    jobs: list[tuple[str, Path]] = []
    for z in range(args.min_zoom, args.max_zoom + 1):
        x0, y0 = deg2tile(north, west, z)
        x1, y1 = deg2tile(south, east, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                jobs.append((SOURCE.format(z=z, x=x, y=y), TILE_DIR / str(z) / str(x) / f"{y}.jpg"))

    print(f"bbox  {south:.4f}..{north:.4f} lat  {west:.4f}..{east:.4f} lon")
    print(f"zooms z{args.min_zoom}-z{args.max_zoom}  ->  {len(jobs)} tiles into {TILE_DIR}")

    counts = {"ok": 0, "cached": 0, "fail": 0}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_fetch, url, dest): url for url, dest in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            counts[res] += 1
            if res == "fail":
                failures.append(futures[fut])
            if i % 100 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}  ok={counts['ok']} cached={counts['cached']} "
                      f"fail={counts['fail']}", flush=True)

    total_bytes = sum(f.stat().st_size for f in TILE_DIR.rglob("*.jpg"))
    print(f"\n{counts['ok']} downloaded, {counts['cached']} already present, "
          f"{counts['fail']} failed")
    print(f"tile pyramid on disk: {total_bytes / 1_048_576:.1f} MB "
          f"in {sum(1 for _ in TILE_DIR.rglob('*.jpg'))} files")

    # Record what was fetched so the UI can configure Leaflet from it rather
    # than from numbers duplicated in JavaScript.
    meta.update({
        "tiles": "/ui/tiles/{z}/{x}/{y}.jpg",
        "min_zoom": args.min_zoom,
        "max_native_zoom": args.max_zoom,
        "pad_deg": PAD_DEG,
        "source": "Esri World Imagery",
        "attribution": ATTRIBUTION,
    })
    MANIFEST.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"updated {MANIFEST}")

    if failures:
        print(f"\n{len(failures)} tiles failed, first few:", file=sys.stderr)
        for u in failures[:5]:
            print(f"  {u}", file=sys.stderr)
        print("re-run to retry only the missing ones (existing tiles are skipped)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
