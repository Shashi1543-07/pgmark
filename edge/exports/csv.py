"""CSV export. See blueprint §8: the map must never be the only way to
get data out.

No SQL lives here -- callers pass rows already read via repo.py.
"""
from __future__ import annotations

import csv
import io

_FIELDS = ["ind_id", "run_id", "provisional", "area_km2", "event_count",
           "effort_days", "centroid_lat", "centroid_lon", "station_set",
           "insufficient_reason"]


def occupancy_csv(rows: list[dict], include_locations: bool = True) -> str:
    """include_locations=False blanks centroid columns and the station
    list -- the same role gate applied to every other export and read of
    precise coordinates (blueprint §10). area_km2 stays: it does not
    locate anything by itself."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        row = dict(r)
        station_set = row.get("station_set")
        row["station_set"] = "|".join(station_set) if isinstance(station_set, list) else ""
        if not include_locations:
            row["centroid_lat"] = row["centroid_lon"] = row["station_set"] = ""
        w.writerow(row)
    return buf.getvalue()
