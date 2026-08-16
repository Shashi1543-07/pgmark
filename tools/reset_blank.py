"""Wipes the database down to an empty reserve + station skeleton -- no
runs, no individuals, no alerts, no audit history. For hands-on manual
testing of the real pipeline against real photos (see
tools/build_manual_test_set.py for a folder to point Import Photos at),
where tools/seed_demo.py's eight planted scenarios would otherwise sit in
the way of seeing your own results.

Keeps the reserve and its stations, because the UI needs a reserve to
select and Stage 1 ingest needs stations to assign scanned folders to --
without either, nothing in the software can be exercised at all. Reuses
tools/seed_demo.py's own station geometry and activity windows so the
map/occupancy screens still have somewhere real to draw and effort
coverage has a real camera-days denominator, even with zero captures.

    python -m tools.reset_blank
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import config                                            # noqa: E402
from edge.db import repo                                           # noqa: E402
from tools.seed_demo import (                                      # noqa: E402
    RESERVE, RESERVE_UTM_EPSG, build_activity, build_stations, _capture_existing_accounts)


def main() -> None:
    config.ensure_dirs()
    existing_users = []
    existing_sessions = []
    if config.DB_PATH.exists():
        # Same guarantee as tools/seed_demo.py's --reset: never silently
        # discard real accounts because this read raced with something else
        # holding the database open. See _capture_existing_accounts()'s own
        # docstring for the incident that made this non-optional.
        existing_users, existing_sessions = _capture_existing_accounts()
        repo.close_all()
        try:
            config.DB_PATH.unlink()
            for suffix in ("-wal", "-shm"):
                p = Path(str(config.DB_PATH) + suffix)
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
        except OSError:
            conn = repo.connect()
            conn.execute("PRAGMA foreign_keys = OFF")
            tables = [r["name"] for r in repo._rows(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))]
            for t in tables:
                if t != "schema_migrations":
                    try:
                        conn.execute(f"DELETE FROM {t}")
                    except Exception:
                        pass
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()
    repo.migrate()

    boundary = {
        "type": "Polygon",
        "coordinates": [[[79.20, 21.55], [79.40, 21.55],
                         [79.40, 21.76], [79.20, 21.76], [79.20, 21.55]]],
    }
    repo.insert("reserves", {
        "reserve_id": RESERVE, "name": "Pench Tiger Reserve", "state": "Maharashtra",
        "utm_epsg": RESERVE_UTM_EPSG, "boundary_geojson": json.dumps(boundary),
        "created_at": repo.now(),
    })
    stations = build_stations()
    repo.insert_many("stations", stations)
    repo.insert_many("station_activity", build_activity(stations, set(), datetime.now(timezone.utc)))
    if existing_users:
        for u in existing_users:
            repo.insert("users", dict(u))
        for s in existing_sessions:
            repo.insert("sessions", dict(s))
    else:
        adm = repo.ensure_admin()
        if adm["created"]:
            print(f"admin account created — temp password: {adm['temp_password']} · recovery code: {adm['recovery_code']}")

    print(f"blank reserve ready: {RESERVE} -- {len(stations)} stations, "
          f"0 runs, 0 individuals, 0 alerts")
    print("point Import Photos at manual_test_photos/ "
          "(python -m tools.build_manual_test_set if it doesn't exist yet)")


if __name__ == "__main__":
    main()
