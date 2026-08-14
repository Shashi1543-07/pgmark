"""GeoJSON export. See blueprint §8: "The map must never be the only way
to get data out" -- GeoJSON loads directly in QGIS, which forest
departments actually use.

No SQL lives here -- callers pass rows already read via repo.py.
"""
from __future__ import annotations


def _parse_polygon_wkt(wkt: str) -> list[list[float]]:
    """'POLYGON((lon lat, lon lat, ...))' -> [[lon, lat], ...]. This module
    only ever receives WKT written by edge/pipeline/occupancy.py, which
    always closes the ring (first point repeated last) -- exactly what
    GeoJSON also requires, so no extra closing step is needed here."""
    inner = wkt.strip()[len("POLYGON(("):-2]
    ring = []
    for pair in inner.split(","):
        lon_s, lat_s = pair.strip().split(" ")
        ring.append([float(lon_s), float(lat_s)])
    return ring


def occupancy_feature_collection(rows: list[dict], include_geometry: bool = True) -> dict:
    """One feature per individual with a valid hull this cycle. Individuals
    without enough captures for a hull are omitted here, not misrepresented
    as a point or an empty shape -- csv.occupancy_csv() is where they still
    show up, with their insufficient_reason intact.

    include_geometry=False drops the polygon and centroid -- the caller's
    own role gate, same as everywhere else precise coordinates are served
    (blueprint §10). Area alone does not locate anything."""
    features = []
    for r in rows:
        if not r.get("hull_wkt"):
            continue
        props = {
            "ind_id": r["ind_id"], "run_id": r["run_id"],
            "area_km2": r["area_km2"], "event_count": r["event_count"],
            "effort_days": r["effort_days"],
            "provisional": bool(r.get("provisional")),
        }
        geometry = None
        if include_geometry:
            geometry = {"type": "Polygon", "coordinates": [_parse_polygon_wkt(r["hull_wkt"])]}
            props["centroid_lat"] = r["centroid_lat"]
            props["centroid_lon"] = r["centroid_lon"]
        features.append({"type": "Feature", "geometry": geometry, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def stations_feature_collection(rows: list[dict]) -> dict:
    """One point feature per camera station. Stations are infrastructure,
    not an individual tiger's location, so this carries no role gate of
    its own -- callers already generalise station coordinates the same
    way as everywhere else if the requesting role calls for it."""
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {"station_id": r["station_id"], "name": r["name"],
                           "zone": r["zone"], "village_dist_km": r["village_dist_km"]},
        } for r in rows],
    }
