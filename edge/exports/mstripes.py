"""M-STrIPES adapter. See blueprint §11.2.

M-STrIPES (NTCA + WII, live since 2010) already carries geotagged field
observations, patrol tracks and reserve structure at Pench and other
reserves. The right integration reads its station and patrol data instead
of asking staff to re-enter it, and emits observations in a form it can
ingest back -- "we don't ask a reserve to replace anything."

This is deliberately NOT built. Blueprint is explicit about why: "you
cannot verify the schema before the 17th... claiming an integration you
have not built is the one thing that will cost you the room." Nothing in
this codebase has ever seen a real M-STrIPES export, so nothing here can
honestly claim to parse one. What follows is the interface a real adapter
would fill in; every function raises exactly that fact rather than a
fabricated "success" against a guessed schema. Producing a plausible-
looking result from an unverified field-name guess would be worse than
refusing -- it would silently corrupt whichever side trusts it.

Implementing this for real needs, at minimum: a sample M-STrIPES export
(or API access) to confirm field names, units and coordinate reference
system. None of that has been available while building this.
"""
from __future__ import annotations


class SchemaNotAvailable(NotImplementedError):
    """Raised by every function in this module. Not a bug -- there is no
    verified M-STrIPES schema to code against yet. See the module
    docstring for what would need to change before this can be filled in."""


def import_stations(mstripes_export_path: str) -> list[dict]:
    """Would read M-STrIPES' deployment/station records and return rows
    shaped for edge.db.repo's `stations` table (station_id, reserve_id,
    name, lat, lon, zone, village_dist_km, grid_cell, folder_hint)."""
    raise SchemaNotAvailable(
        "M-STrIPES station schema has not been verified against a real "
        "export or API response. Get one, confirm field names and CRS, "
        "then implement this against what was actually confirmed -- not "
        "a guess.")


def import_patrol_tracks(mstripes_export_path: str) -> list[dict]:
    """Would read M-STrIPES' patrol GPS tracks, for cross-referencing
    effort with camera-trap coverage. Same caveat as import_stations()."""
    raise SchemaNotAvailable(
        "M-STrIPES patrol-track schema has not been verified. See "
        "import_stations() for why this raises instead of guessing.")


def export_observations(detection_rows: list[dict]) -> list[dict]:
    """Would shape Pugmark's detections into whatever observation form
    M-STrIPES accepts back, so a reserve running both systems sees one
    picture rather than two. Requires the same verified schema as the
    import side."""
    raise SchemaNotAvailable(
        "M-STrIPES observation-intake schema has not been verified. See "
        "import_stations() for why this raises instead of guessing.")
