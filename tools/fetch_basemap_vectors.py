"""Build the offline WORLD basemap: Natural Earth vectors, trimmed.

A BUILD-TIME tool, like tools/fetch_basemap_tiles.py, and the only other
thing in this repository allowed to touch the network. It runs once on a
machine with internet and writes plain GeoJSON into `edge/ui/geo/`, which
ships with the app.

WHY VECTORS AND NOT MORE TILES
The satellite pyramid covers the reserve and nothing else, so zooming out
fell off the edge of the world -- there was no India to see, let alone a
planet. Covering the globe with raster tiles is not a real option: z0-z6
alone is ~5,500 tiles, the bulk-download terms of every free raster basemap
forbid exactly this, and the result would still be a fixed picture that
cannot be recoloured.

Natural Earth is public domain (CC0), a few hundred KB once trimmed, draws
at ANY zoom because it is geometry rather than pixels, and -- the part that
matters for this UI -- its colours are decided at draw time, so the whole
world can turn green for the dark theme and white for the light one from
the same data.

    python -m tools.fetch_basemap_vectors

Precision: coordinates are rounded to 4 decimal places, about 11 m at the
equator. That is far finer than a country outline needs at any zoom where
country outlines are the subject, and it roughly halves the payload.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

GEO_DIR = Path(__file__).resolve().parents[1] / "edge" / "ui" / "geo"
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"

# source file -> (output name, properties worth keeping)
LAYERS = {
    "ne_110m_ocean.geojson":                  ("ocean.geojson", ()),
    "ne_50m_admin_0_countries.geojson":       ("countries.geojson", ("NAME", "ADMIN", "ISO_A2")),
    "ne_50m_admin_1_states_provinces.geojson": ("states.geojson", ("name", "admin")),
    "ne_50m_lakes.geojson":                   ("lakes.geojson", ("name",)),
}
ATTRIBUTION = "Boundaries: Natural Earth (public domain)"
PRECISION = 4


def _round_coords(node):
    """Walk a GeoJSON coordinate tree, rounding every number in place."""
    if isinstance(node, (int, float)):
        return round(float(node), PRECISION)
    if isinstance(node, list):
        return [_round_coords(v) for v in node]
    return node


def _fetch(name: str) -> dict:
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "pugmark-build/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download files already present")
    args = ap.parse_args()

    GEO_DIR.mkdir(parents=True, exist_ok=True)
    total_in = total_out = 0

    for src, (out_name, keep) in LAYERS.items():
        dest = GEO_DIR / out_name
        if dest.exists() and not args.force:
            print(f"  skip     {out_name} (already present; --force to refresh)")
            total_out += dest.stat().st_size
            continue
        try:
            gj = _fetch(src)
        except Exception as e:
            print(f"  FAILED   {src}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

        raw = len(json.dumps(gj))
        total_in += raw

        features = []
        for f in gj.get("features", []):
            geom = f.get("geometry")
            if not geom or not geom.get("coordinates"):
                continue
            geom["coordinates"] = _round_coords(geom["coordinates"])
            props = {k: f.get("properties", {}).get(k) for k in keep
                     if f.get("properties", {}).get(k)}
            features.append({"type": "Feature", "properties": props, "geometry": geom})

        trimmed = {"type": "FeatureCollection", "features": features}
        # separators matter: the default ", " / ": " adds ~15% to a file that
        # is mostly punctuation
        blob = json.dumps(trimmed, separators=(",", ":"))
        dest.write_text(blob, encoding="utf-8")
        total_out += len(blob)
        print(f"  wrote    {out_name:<18} {len(features):>4} features  "
              f"{raw/1024/1024:5.2f} MB -> {len(blob)/1024/1024:5.2f} MB")

    manifest = GEO_DIR / "index.json"
    manifest.write_text(json.dumps({
        "source": "Natural Earth 1:50m / 1:110m",
        "attribution": ATTRIBUTION,
        "licence": "public domain (CC0)",
        "precision_dp": PRECISION,
        "layers": {v[0].replace(".geojson", ""): f"/ui/geo/{v[0]}" for v in LAYERS.values()},
    }, indent=2), encoding="utf-8")

    print(f"\nworld basemap on disk: {total_out/1024/1024:.2f} MB in {GEO_DIR}")
    print(f"wrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
