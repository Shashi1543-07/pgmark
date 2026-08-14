"""Stage 4 -- occupancy. See blueprint §8.

Minimum convex polygon (MCP) home range: the standard first-line estimate
in the camera-trap literature. Computed over the stations an individual
used this cycle, weighted by how often -- never over raw images, since a
3-shot burst is one visit, not three (that grouping happens at ingest,
Stage 1; by the time a station_id/event_count pair reaches this module
the counting is already done).

The trap this module exists to avoid (blueprint R3): shoelace on raw
lat/lon degrees gives a confidently wrong area, because a degree of
longitude is not a fixed distance -- it shrinks with cos(latitude), and
degrees of latitude and longitude are not even the same length to begin
with. Every polygon here is projected into the reserve's own UTM zone
(reserves.utm_epsg) before its area is measured. The zone is read from
that column, never hardcoded -- a national deployment spans several.

No SQL lives here -- this module takes plain (lat, lon, weight) triples
and a UTM EPSG code, and returns numbers. It does not know what a
"reserve" or a "run" is.
"""
from __future__ import annotations

import math

Point = tuple[float, float]           # (lat, lon), degrees
StationPoint = tuple[float, float, int]  # (lat, lon, event_count)


def project_utm(lat: float, lon: float, epsg: int) -> tuple[float, float]:
    """Forward Transverse Mercator, WGS84 ellipsoid, zone and hemisphere
    read from the EPSG code itself: 326xx = zone xx north, 327xx = zone xx
    south (the standard WGS84/UTM EPSG numbering). Returns (easting,
    northing) in metres. Standard Snyder/USGS series expansion -- the
    same algorithm behind most "utm" conversion libraries."""
    zone = epsg % 100
    northern = (epsg // 100) == 326

    a = 6378137.0                       # WGS84 semi-major axis, metres
    f = 1 / 298.257223563               # WGS84 flattening
    e2 = f * (2 - f)                    # eccentricity squared
    ep2 = e2 / (1 - e2)                 # second eccentricity squared
    k0 = 0.9996                         # UTM scale factor

    lon0 = math.radians(zone * 6 - 183)  # central meridian of this zone
    lat_r, lon_r = math.radians(lat), math.radians(lon)

    N = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    T = math.tan(lat_r) ** 2
    C = ep2 * math.cos(lat_r) ** 2
    A = math.cos(lat_r) * (lon_r - lon0)
    M = a * (
        (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * lat_r
        - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*lat_r)
        + (15*e2**2/256 + 45*e2**3/1024) * math.sin(4*lat_r)
        - (35*e2**3/3072) * math.sin(6*lat_r)
    )

    easting = k0 * N * (
        A + (1 - T + C) * A**3 / 6
        + (5 - 18*T + T**2 + 72*C - 58*ep2) * A**5 / 120
    ) + 500_000.0
    northing = k0 * (
        M + N * math.tan(lat_r) * (
            A**2 / 2 + (5 - T + 9*C + 4*C**2) * A**4 / 24
            + (61 - 58*T + T**2 + 600*C - 330*ep2) * A**6 / 720
        )
    )
    if not northern:
        northing += 10_000_000.0
    return easting, northing


def polygon_area_m2(points: list[tuple[float, float]]) -> float:
    """Shoelace formula on already-projected (x, y) metres. Points need
    not be pre-ordered into a ring -- callers pass a hull in winding
    order, but the formula is order-direction-agnostic (abs())."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def convex_hull(points: list[Point]) -> list[Point]:
    """Andrew's monotone chain. Runs directly on (lat, lon) pairs -- fine
    for sorting and left/right turn tests, which only need consistent
    ordering, not metric distance. The metric geometry (area) happens
    after projection, in mcp_home_range()."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def mcp_home_range(points: list[Point], epsg: int) -> dict | None:
    """The hull (as lat/lon, for display) and its true projected area.
    None if fewer than 3 distinct, non-collinear points survive the hull
    -- an MCP is undefined below that, and a degenerate polygon must
    never be emitted silently (blueprint §8)."""
    hull = convex_hull(points)
    if len(hull) < 3:
        return None
    projected = [project_utm(lat, lon, epsg) for lat, lon in hull]
    area_km2 = polygon_area_m2(projected) / 1_000_000.0
    if area_km2 <= 0:
        return None   # collinear: a real hull, but zero-width, not a range
    return {"hull": hull, "area_km2": round(area_km2, 2)}


def compute(station_points: list[StationPoint], epsg: int, min_stations: int) -> dict:
    """The occupancy row for one individual, one run. station_points is
    one (lat, lon, event_count) triple per distinct station used --
    already de-duplicated and burst-grouped upstream. event_count is only
    used to weight the centroid; the hull only needs the distinct points."""
    if not station_points:
        return {"hull_wkt": None, "centroid_lat": None, "centroid_lon": None,
                "area_km2": None, "insufficient_reason": "no captures this cycle"}

    total = sum(n for _, _, n in station_points)
    clat = round(sum(lat * n for lat, lon, n in station_points) / total, 5)
    clon = round(sum(lon * n for lat, lon, n in station_points) / total, 5)

    if len(station_points) < min_stations:
        return {"hull_wkt": None, "centroid_lat": clat, "centroid_lon": clon,
                "area_km2": None,
                "insufficient_reason": f"fewer than {min_stations} stations"}

    result = mcp_home_range([(lat, lon) for lat, lon, _ in station_points], epsg)
    if result is None:
        return {"hull_wkt": None, "centroid_lat": clat, "centroid_lon": clon,
                "area_km2": None,
                "insufficient_reason": "capture points are collinear; no valid polygon"}

    ring = result["hull"] + [result["hull"][0]]
    wkt = "POLYGON((" + ", ".join(f"{lon} {lat}" for lat, lon in ring) + "))"
    return {"hull_wkt": wkt, "centroid_lat": clat, "centroid_lon": clon,
            "area_km2": result["area_km2"], "insufficient_reason": None}
