"""Database access. Every SQL statement in PUGMARK lives in this module.

Rule: no other file writes SQL. When a query is wrong there is exactly one
place to look, and when the schema changes there is exactly one place to fix.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from edge import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_local = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ── connection ──────────────────────────────────────────────────────────

def connect(path: Path | None = None) -> sqlite3.Connection:
    """One connection per thread. SQLite objects are not thread-safe and
    FastAPI runs handlers in a thread pool."""
    path = path or config.DB_PATH
    key = f"conn:{path}"
    conn = getattr(_local, key, None)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        setattr(_local, key, conn)
    return conn


def close() -> None:
    for key in list(vars(_local)):
        if key.startswith("conn:"):
            getattr(_local, key).close()
            delattr(_local, key)


# ── migrations ──────────────────────────────────────────────────────────

def migrate(path: Path | None = None) -> int:
    """Apply pending migrations in filename order. Idempotent: safe to call
    on every startup, which is exactly when it is called."""
    conn = connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, filename TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    count = 0
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?,?,?)",
            (version, f.name, now()),
        )
        conn.commit()
        count += 1
    return count


def schema_version(path: Path | None = None) -> int:
    conn = connect(path)
    row = conn.execute("SELECT MAX(version) v FROM schema_migrations").fetchone()
    return (row["v"] or 0) if row else 0


# ── helpers ─────────────────────────────────────────────────────────────

def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _one(cur: sqlite3.Cursor) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def insert(table: str, values: dict, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    conn.commit()


def insert_many(table: str, rows: Iterable[dict], conn: sqlite3.Connection | None = None) -> int:
    rows = list(rows)
    if not rows:
        return 0
    conn = conn or connect()
    cols = list(rows[0])
    marks = ", ".join("?" for _ in cols)
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({marks})",
        [tuple(r[c] for c in cols) for r in rows],
    )
    conn.commit()
    return len(rows)


# ── sync ────────────────────────────────────────────────────────────────
# Every syncable table (runs, stations, images, individuals) carries
# origin_node/lamport/synced_at/row_hash from the frozen schema onward.
# What lives here is this node's own identity and clock (migration 0002)
# and the generic machinery a bundle needs: which rows are new since last
# sync, and how to apply an incoming row without duplicating it or losing
# a genuine edit. See edge/sync/bundle.py for the bundle format itself.

SYNCABLE_TABLES = {
    "runs": "run_id", "stations": "station_id",
    "images": "image_id", "individuals": "ind_id",
}
_SYNC_BOOKKEEPING = ("origin_node", "lamport", "synced_at", "row_hash")


def node_id(conn: sqlite3.Connection | None = None) -> str:
    """This node's own persistent identity. Generated once, on first use,
    and never regenerated -- a node's identity changing mid-deployment
    would make every row it ever wrote look like it came from somewhere
    else."""
    conn = conn or connect()
    row = conn.execute("SELECT node_id FROM node_identity LIMIT 1").fetchone()
    if row:
        return row["node_id"]
    new = uuid.uuid4().hex
    conn.execute("INSERT INTO node_identity(node_id, lamport_counter) VALUES (?, 0)", (new,))
    conn.commit()
    return new


def next_lamport(conn: sqlite3.Connection | None = None) -> int:
    """Atomically advances this node's own clock. Every syncable row gets
    the value in force when it was written, so two bundles can always be
    merged in a single, deterministic (lamport, origin_node) order no
    matter which node applies which first."""
    conn = conn or connect()
    node_id(conn)  # ensures a row exists to update
    conn.execute("UPDATE node_identity SET lamport_counter = lamport_counter + 1")
    row = conn.execute("SELECT lamport_counter FROM node_identity LIMIT 1").fetchone()
    conn.commit()
    return row["lamport_counter"]


def compute_row_hash(values: dict) -> str:
    """Content hash of a row's business fields, excluding the sync
    bookkeeping columns themselves -- hashing row_hash into row_hash is
    circular, and origin_node/lamport/synced_at describe the row's
    history, not its content. The same content always hashes the same,
    which is what makes bundle application idempotent: re-applying an
    already-seen row is detectable without comparing every column."""
    business = {k: v for k, v in values.items() if k not in _SYNC_BOOKKEEPING}
    canonical = json.dumps(business, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def rows_since(table: str, since_lamport: int,
               conn: sqlite3.Connection | None = None) -> list[dict]:
    """Everything this node has written to `table` with a higher Lamport
    value than the caller has already seen. Rows with no lamport at all
    (never stamped -- true of this build's seed/demo data) are not part
    of the sync-tracked set and are correctly excluded, not silently
    swept in as 'new'."""
    if table not in SYNCABLE_TABLES:
        raise ValueError(f"{table!r} is not a syncable table")
    conn = conn or connect()
    return _rows(conn.execute(
        f"SELECT * FROM {table} WHERE lamport IS NOT NULL AND lamport > ?"
        " ORDER BY lamport", (since_lamport,)))


def mark_synced(table: str, ids: list, conn: sqlite3.Connection | None = None) -> None:
    """synced_at is NULL until a row has been included in a bundle this
    node sent out (blueprint §4's own description of the column) -- this
    is what keeps a 'how much is left to sync' count honest over time
    instead of re-counting the same already-sent rows forever."""
    if not ids or table not in SYNCABLE_TABLES:
        return
    pk_col = SYNCABLE_TABLES[table]
    conn = conn or connect()
    marks = ", ".join("?" for _ in ids)
    conn.execute(f"UPDATE {table} SET synced_at=? WHERE {pk_col} IN ({marks})",
                (now(), *ids))
    conn.commit()


def pending_sync_count(reserve_id: str, conn: sqlite3.Connection | None = None) -> int:
    """Rows ever stamped with a Lamport value that have not yet gone out
    in a bundle. Zero for this build's seed/demo data by construction --
    nothing in tools/seed_demo.py stamps these columns, so none of it is
    sync-tracked, which is the correct state for data that was never
    really produced by a node in the first place."""
    conn = conn or connect()
    total = 0
    for table in SYNCABLE_TABLES:
        row = _one(conn.execute(
            f"SELECT COUNT(*) c FROM {table}"
            f" WHERE reserve_id=? AND lamport IS NOT NULL AND synced_at IS NULL",
            (reserve_id,)))
        total += row["c"] if row else 0
    return total


def advance_lamport_watermark(value: int, conn: sqlite3.Connection | None = None) -> None:
    """After applying a bundle, this node's own clock must never fall
    behind what it just learned -- a Lamport clock's entire purpose is
    ordering causally-related events, which breaks the moment a node's
    own counter can lag behind values it has already seen from
    elsewhere. Called once per apply_bundle(), not per row."""
    conn = conn or connect()
    node_id(conn)
    conn.execute("UPDATE node_identity SET lamport_counter = MAX(lamport_counter, ?)", (value,))
    conn.commit()


def apply_synced_row(table: str, row: dict,
                     conn: sqlite3.Connection | None = None) -> str:
    """Idempotent, conflict-aware insert of one incoming row. Returns
    'inserted', 'unchanged' (already had this exact content -- the
    ordinary case when a bundle arrives twice), or 'conflict_resolved'
    (same primary key, different content on each side -- resolved by
    keeping whichever side has the higher Lamport value, tie-broken by
    origin_node so the outcome is the same regardless of which node
    applies the merge)."""
    if table not in SYNCABLE_TABLES:
        raise ValueError(f"{table!r} is not a syncable table")
    pk_col = SYNCABLE_TABLES[table]
    conn = conn or connect()
    existing = _one(conn.execute(f"SELECT * FROM {table} WHERE {pk_col}=?", (row[pk_col],)))
    if not existing:
        cols = list(row)
        marks = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                    tuple(row[c] for c in cols))
        conn.commit()
        return "inserted"

    if existing.get("row_hash") == row.get("row_hash"):
        return "unchanged"

    incoming_wins = (row.get("lamport") or 0, row.get("origin_node") or "") > \
        (existing.get("lamport") or 0, existing.get("origin_node") or "")
    if incoming_wins:
        cols = [c for c in row if c != pk_col]
        set_clause = ", ".join(f"{c}=?" for c in cols)
        conn.execute(f"UPDATE {table} SET {set_clause} WHERE {pk_col}=?",
                    (*[row[c] for c in cols], row[pk_col]))
        conn.commit()
    return "conflict_resolved"


# ── audit ───────────────────────────────────────────────────────────────

def audit(action: str, *, actor: str = "system", entity_type: str | None = None,
          entity_id: str | None = None, before: Any = None, after: Any = None,
          model_version: str | None = None, threshold: float | None = None,
          note: str | None = None) -> None:
    """Append to the audit log. The table has triggers blocking UPDATE and
    DELETE, so this is the only way anything gets in and nothing gets out."""
    connect().execute(
        "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id,"
        " before, after, model_version, threshold, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now(), actor, action, entity_type, entity_id,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None,
         model_version, threshold, note),
    )
    connect().commit()


def audit_tail(limit: int = 200, q: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_log"
    args: list = []
    if q:
        sql += " WHERE action LIKE ? OR entity_id LIKE ? OR note LIKE ?"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY log_id DESC LIMIT ?"
    args.append(limit)
    return _rows(connect().execute(sql, args))


# ── reserves and stations ───────────────────────────────────────────────

def reserves() -> list[dict]:
    return _rows(connect().execute("SELECT * FROM reserves ORDER BY name"))


def reserve(reserve_id: str) -> dict | None:
    return _one(connect().execute(
        "SELECT * FROM reserves WHERE reserve_id=?", (reserve_id,)))


def stations(reserve_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT s.*,"
        " (SELECT COUNT(*) FROM images i WHERE i.station_id=s.station_id) AS image_count"
        " FROM stations s WHERE s.reserve_id=? ORDER BY s.station_id", (reserve_id,)))


def station_effort_days(station_id: str, start: str, end: str) -> float:
    """Camera-days a station was active inside a window. This number is what
    makes the difference between 'the tiger is gone' and 'we weren't looking'."""
    rows = _rows(connect().execute(
        "SELECT start_date, end_date FROM station_activity WHERE station_id=?",
        (station_id,)))
    total = 0.0
    w0, w1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
    for r in rows:
        a = datetime.fromisoformat(r["start_date"])
        b = datetime.fromisoformat(r["end_date"]) if r["end_date"] else w1
        lo, hi = max(a, w0), min(b, w1)
        if hi > lo:
            total += (hi - lo).total_seconds() / 86400.0
    return round(total, 2)


def station_activity_for_reserve(reserve_id: str) -> list[dict]:
    """Every deployment interval for every station in a reserve -- the
    Camtrap DP 'Deployments' table is this, almost column for column."""
    return _rows(connect().execute(
        "SELECT sa.activity_id, sa.station_id, sa.start_date, sa.end_date, sa.note,"
        " s.name, s.lat, s.lon, s.zone"
        " FROM station_activity sa JOIN stations s ON s.station_id = sa.station_id"
        " WHERE s.reserve_id=? ORDER BY sa.station_id, sa.start_date", (reserve_id,)))


def detections_for_run(run_id: str) -> list[dict]:
    """Every detection in a run, with whatever identity it eventually
    carries -- the confirmed individual if a human or an auto-accepted
    match settled it (superseded_by IS NULL), NULL otherwise. One row per
    detection; a detection with no usable crop simply has no assignment
    to join, not a missing row."""
    return _rows(connect().execute(
        "SELECT d.det_id, d.image_id, d.label, d.species, d.conf,"
        " im.station_id, im.captured_at,"
        " ie.event_id,"
        " a.ind_id, a.confidence assign_confidence, a.method assign_method"
        " FROM detections d"
        " JOIN images im ON im.image_id = d.image_id"
        " LEFT JOIN image_event ie ON ie.image_id = im.image_id"
        " LEFT JOIN flank_crops c ON c.det_id = d.det_id"
        " LEFT JOIN assignments a ON a.crop_id = c.crop_id AND a.superseded_by IS NULL"
        " WHERE im.run_id=?", (run_id,)))


# ── runs ────────────────────────────────────────────────────────────────

def runs(reserve_id: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM runs"
    args: list = []
    if reserve_id:
        sql += " WHERE reserve_id=?"
        args.append(reserve_id)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    return _rows(connect().execute(sql, args))


def run(run_id: str) -> dict | None:
    return _one(connect().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)))


def latest_run(reserve_id: str) -> dict | None:
    return _one(connect().execute(
        "SELECT * FROM runs WHERE reserve_id=? ORDER BY started_at DESC LIMIT 1",
        (reserve_id,)))


def run_counts(run_id: str) -> dict:
    row = _one(connect().execute(
        "SELECT COUNT(*) total,"
        " SUM(status='blank')       blank,"
        " SUM(status='subject')     subject,"
        " SUM(status='person')      person,"
        " SUM(status='vehicle')     vehicle,"
        " SUM(status='corrupt')     corrupt,"
        " SUM(status='quarantined') quarantined,"
        " SUM(triage_stage='A')     stage_a,"
        " SUM(triage_stage='B')     stage_b"
        " FROM images WHERE run_id=?", (run_id,))) or {}
    return {k: (v or 0) for k, v in row.items()}


def timestamp_sources(run_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT captured_at_source src, COUNT(*) n FROM images"
        " WHERE run_id=? GROUP BY src ORDER BY n DESC", (run_id,)))


def run_flags(run_id: str) -> dict:
    """Aggregate ingest warnings so preflight can show them as counts."""
    out: dict[str, int] = {}
    for r in connect().execute("SELECT flags FROM images WHERE run_id=?", (run_id,)):
        try:
            for f in json.loads(r["flags"] or "[]"):
                out[f] = out.get(f, 0) + 1
        except json.JSONDecodeError:
            continue
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def set_run_stage(run_id: str, stage: str) -> None:
    conn = connect()
    conn.execute("UPDATE runs SET stage=? WHERE run_id=?", (stage, run_id))
    conn.commit()


def set_run_image_count(run_id: str, n: int) -> None:
    conn = connect()
    conn.execute("UPDATE runs SET image_count=? WHERE run_id=?", (n, run_id))
    conn.commit()


def images_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or connect()
    return _rows(conn.execute(
        "SELECT * FROM images WHERE run_id=? ORDER BY station_id, captured_at", (run_id,)))


def images_by_status(run_id: str, status: str) -> list[dict]:
    """A run's frames at a given status, station name joined in for
    display -- edge/ui/app.js uses this to list a run's 'subject' frames
    with an Identify action next to each, since Stage 3 (identify_upload.py)
    takes a file path directly and never touches a bulk run's own rows."""
    return _rows(connect().execute(
        "SELECT i.image_id, i.orig_path, i.station_id, s.name station_name,"
        " i.captured_at, i.is_night"
        " FROM images i LEFT JOIN stations s ON s.station_id=i.station_id"
        " WHERE i.run_id=? AND i.status=? ORDER BY i.captured_at", (run_id, status)))


def image(image_id: str) -> dict | None:
    return _one(connect().execute("SELECT * FROM images WHERE image_id=?", (image_id,)))


def update_image_station(image_id: str, station_id: str) -> None:
    """Applied at confirm time when a preflight-unmatched folder gets a
    human-assigned station. Never guessed automatically; see blueprint §5.1."""
    conn = connect()
    conn.execute("UPDATE images SET station_id=? WHERE image_id=?", (station_id, image_id))
    conn.commit()


def set_image_status(image_id: str, status: str, triage_stage: str | None = None,
                     conn: sqlite3.Connection | None = None) -> None:
    """Also re-stamps lamport/row_hash when the row was ever sync-tracked
    (lamport IS NOT NULL) -- a status change is a real content change,
    and a stale hash sitting next to fresh content would silently break
    the idempotency check row_hash exists to provide (found while
    building the sync bundle test, not before)."""
    conn = conn or connect()
    row = _one(conn.execute("SELECT * FROM images WHERE image_id=?", (image_id,)))
    if not row:
        return
    row["status"], row["triage_stage"] = status, triage_stage
    if row.get("lamport") is not None:
        row["lamport"] = next_lamport(conn)
        row["synced_at"] = None
        row["row_hash"] = compute_row_hash(row)
    conn.execute(
        "UPDATE images SET status=?, triage_stage=?, lamport=?, synced_at=?, row_hash=?"
        " WHERE image_id=?",
        (row["status"], row["triage_stage"], row.get("lamport"), row.get("synced_at"),
         row.get("row_hash"), image_id))
    conn.commit()


def delete_run_cascade(run_id: str) -> None:
    """Removes a run and everything under it: images, the events/links
    built from them, quarantine rows, and (since Stage B became real)
    detections and whatever hangs off them -- flank_crops, assignments,
    persons_restricted. Used by the live test suite so its own scratch
    ingest/triage runs never linger in what is meant to stay a clean,
    reseedable demonstration database. audit_log entries referencing
    this run_id are left alone -- the log is append-only by design, even
    for a run that no longer exists."""
    conn = connect()
    image_ids = [r["image_id"] for r in conn.execute(
        "SELECT image_id FROM images WHERE run_id=?", (run_id,)).fetchall()]
    if image_ids:
        marks = ",".join("?" * len(image_ids))
        event_ids = [r["event_id"] for r in conn.execute(
            f"SELECT DISTINCT event_id FROM image_event WHERE image_id IN ({marks})",
            image_ids).fetchall()]
        conn.execute(f"DELETE FROM image_event WHERE image_id IN ({marks})", image_ids)
        if event_ids:
            ev_marks = ",".join("?" * len(event_ids))
            conn.execute(f"DELETE FROM events WHERE event_id IN ({ev_marks})", event_ids)

        det_ids = [r["det_id"] for r in conn.execute(
            f"SELECT det_id FROM detections WHERE image_id IN ({marks})", image_ids).fetchall()]
        if det_ids:
            dmarks = ",".join("?" * len(det_ids))
            crop_ids = [r["crop_id"] for r in conn.execute(
                f"SELECT crop_id FROM flank_crops WHERE det_id IN ({dmarks})", det_ids).fetchall()]
            if crop_ids:
                cmarks = ",".join("?" * len(crop_ids))
                conn.execute(f"DELETE FROM assignments WHERE crop_id IN ({cmarks})", crop_ids)
                conn.execute(f"DELETE FROM flank_crops WHERE crop_id IN ({cmarks})", crop_ids)
            conn.execute(f"DELETE FROM detections WHERE det_id IN ({dmarks})", det_ids)
        conn.execute(f"DELETE FROM persons_restricted WHERE image_id IN ({marks})", image_ids)
    conn.execute("DELETE FROM quarantine WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM images WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    conn.commit()


def delete_reserve_cascade(reserve_id: str) -> None:
    """Removes a reserve and everything under it: runs, stations,
    individuals, and every row that hangs off them (images, detections,
    flank_crops, assignments, occupancy, alerts, station_activity). For
    tests/scenarios/test_alert_scenarios.py, which builds and tears down a
    whole synthetic reserve so it never lingers in what is meant to stay a
    clean, reseedable demonstration database."""
    conn = connect()
    run_ids = [r["run_id"] for r in conn.execute(
        "SELECT run_id FROM runs WHERE reserve_id=?", (reserve_id,)).fetchall()]
    image_ids = [r["image_id"] for r in conn.execute(
        "SELECT image_id FROM images WHERE reserve_id=?", (reserve_id,)).fetchall()]
    if image_ids:
        marks = ",".join("?" * len(image_ids))
        det_ids = [r["det_id"] for r in conn.execute(
            f"SELECT det_id FROM detections WHERE image_id IN ({marks})", image_ids).fetchall()]
        if det_ids:
            dmarks = ",".join("?" * len(det_ids))
            crop_ids = [r["crop_id"] for r in conn.execute(
                f"SELECT crop_id FROM flank_crops WHERE det_id IN ({dmarks})", det_ids).fetchall()]
            if crop_ids:
                cmarks = ",".join("?" * len(crop_ids))
                conn.execute(f"DELETE FROM assignments WHERE crop_id IN ({cmarks})", crop_ids)
                conn.execute(f"DELETE FROM flank_crops WHERE crop_id IN ({cmarks})", crop_ids)
            conn.execute(f"DELETE FROM detections WHERE det_id IN ({dmarks})", det_ids)
        event_ids = [r["event_id"] for r in conn.execute(
            f"SELECT DISTINCT event_id FROM image_event WHERE image_id IN ({marks})",
            image_ids).fetchall()]
        conn.execute(f"DELETE FROM image_event WHERE image_id IN ({marks})", image_ids)
        if event_ids:
            emarks = ",".join("?" * len(event_ids))
            conn.execute(f"DELETE FROM events WHERE event_id IN ({emarks})", event_ids)
        conn.execute(f"DELETE FROM images WHERE image_id IN ({marks})", image_ids)
    if run_ids:
        rmarks = ",".join("?" * len(run_ids))
        conn.execute(f"DELETE FROM alerts WHERE run_id IN ({rmarks})", run_ids)
        conn.execute(f"DELETE FROM occupancy WHERE run_id IN ({rmarks})", run_ids)
    conn.execute("DELETE FROM station_activity WHERE station_id IN "
                 "(SELECT station_id FROM stations WHERE reserve_id=?)", (reserve_id,))
    conn.execute("DELETE FROM stations WHERE reserve_id=?", (reserve_id,))
    conn.execute("DELETE FROM individuals WHERE reserve_id=?", (reserve_id,))
    conn.execute("DELETE FROM runs WHERE reserve_id=?", (reserve_id,))
    conn.execute("DELETE FROM reserves WHERE reserve_id=?", (reserve_id,))
    conn.commit()


def station_first_active(station_id: str) -> str | None:
    """Earliest known start_date for a station, used to anchor a corrected
    timestamp when a camera's clock reset (blueprint §5.3)."""
    row = _one(connect().execute(
        "SELECT MIN(start_date) v FROM station_activity WHERE station_id=?", (station_id,)))
    return row["v"] if row else None


# ── quarantine ──────────────────────────────────────────────────────────

def quarantine_summary(run_id: str) -> dict:
    row = _one(connect().execute(
        "SELECT COUNT(*) n, COALESCE(SUM(bytes),0) bytes,"
        " SUM(restored_at IS NOT NULL) restored"
        " FROM quarantine WHERE run_id=?", (run_id,))) or {}
    n = row.get("n") or 0
    restored = row.get("restored") or 0
    live = n - restored
    secs = live * config.CONFIG.triage.seconds_per_manual_review
    return {
        "quarantined": live,
        "restored": restored,
        "bytes": row.get("bytes") or 0,
        "mb": round((row.get("bytes") or 0) / 1_048_576, 1),
        "person_hours_saved": round(secs / 3600, 1),
        "seconds_per_review_assumed": config.CONFIG.triage.seconds_per_manual_review,
    }


def quarantine_sample(run_id: str, limit: int = 24) -> list[dict]:
    return _rows(connect().execute(
        "SELECT q.q_id, q.image_id, q.conf, q.reason, q.orig_path, i.station_id,"
        " i.is_night FROM quarantine q JOIN images i USING(image_id)"
        " WHERE q.run_id=? AND q.restored_at IS NULL"
        " ORDER BY q.conf ASC LIMIT ?", (run_id, limit)))


def restore_quarantine(run_id: str, actor: str = "system") -> int:
    """Reverse a triage decision. Idempotent, and recoverable from the
    on-disk manifest alone even if this database is lost. Covers both
    Stage A's 'quarantined' images and Stage B's 'blank' ones -- both are
    physically quarantined the same way (edge/pipeline/triage.py's
    _quarantine_move()), just tagged with which stage made the call, so
    both need resetting back to 'pending' here."""
    conn = connect()
    n = conn.execute(
        "SELECT COUNT(*) c FROM quarantine WHERE run_id=? AND restored_at IS NULL",
        (run_id,)).fetchone()["c"]
    if not n:
        return 0
    conn.execute(
        "UPDATE quarantine SET restored_at=? WHERE run_id=? AND restored_at IS NULL",
        (now(), run_id))
    # Sync-tracked rows (a real ingest run) need the same fresh lamport/
    # row_hash stamp any other content change gets -- see set_image_status().
    # Untracked rows (this build's seed/demo data, which never sets lamport
    # at all) take the cheap bulk path since there is no hash invariant to
    # keep for them, and a real deployment's quarantine rarely spans
    # thousands of rows the way the synthetic demo does.
    tracked = _rows(conn.execute(
        "SELECT image_id, triage_stage FROM images"
        " WHERE run_id=? AND status IN ('quarantined','blank') AND lamport IS NOT NULL",
        (run_id,)))
    for r in tracked:
        set_image_status(r["image_id"], "pending", r["triage_stage"], conn)
    conn.execute(
        "UPDATE images SET status='pending'"
        " WHERE run_id=? AND status IN ('quarantined','blank') AND lamport IS NULL",
        (run_id,))
    conn.commit()
    audit("quarantine.restore", actor=actor, entity_type="run", entity_id=run_id,
          after={"restored": n})
    return n


# ── individuals ─────────────────────────────────────────────────────────

def individuals(reserve_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT i.*,"
        " (SELECT COUNT(*) FROM assignments a WHERE a.ind_id=i.ind_id"
        "    AND a.superseded_by IS NULL) AS crop_count,"
        " (SELECT COUNT(DISTINCT im.station_id) FROM assignments a"
        "    JOIN flank_crops c   ON c.crop_id=a.crop_id"
        "    JOIN detections  d   ON d.det_id=c.det_id"
        "    JOIN images      im  ON im.image_id=d.image_id"
        "   WHERE a.ind_id=i.ind_id AND a.superseded_by IS NULL) AS station_count,"
        " (SELECT GROUP_CONCAT(DISTINCT c2.side) FROM assignments a2"
        "    JOIN flank_crops c2 ON c2.crop_id=a2.crop_id"
        "   WHERE a2.ind_id=i.ind_id AND a2.superseded_by IS NULL) AS sides,"
        " (SELECT AVG(a3.confidence) FROM assignments a3"
        "   WHERE a3.ind_id=i.ind_id AND a3.superseded_by IS NULL) AS mean_confidence"
        " FROM individuals i WHERE i.reserve_id=?"
        " ORDER BY i.provisional DESC, i.ind_id", (reserve_id,)))


def individual(ind_id: str) -> dict | None:
    return _one(connect().execute(
        "SELECT * FROM individuals WHERE ind_id=?", (ind_id,)))


def create_provisional_individual(reserve_id: str, actor: str) -> str:
    """A new individual the catalogue has never seen -- provisional until
    a human promotes it (promote_individual()), matching the demo
    catalogue's own PENCH-P-NNN convention. Used by the identify route's
    enrol path: a crop that scored below t_low against every existing
    entity of its side is not a smaller-confidence match, it is nothing
    to compare against (edge/pipeline/identify.py::decide())."""
    ind_id = f"{reserve_id}-P-{new_id()[:8]}"
    ts = now()
    insert("individuals", dict(
        ind_id=ind_id, reserve_id=reserve_id, label=None, provisional=1,
        sex=None, age_class=None, first_seen=ts, last_seen=ts,
        national_id=None, notes="auto-enrolled by identify pipeline"))
    audit("individual.auto_enroll", actor=actor, entity_type="individual",
          entity_id=ind_id, after={"reserve_id": reserve_id})
    return ind_id


def individual_captures(ind_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT im.image_id, im.captured_at, im.is_night, im.station_id,"
        " s.name station_name, s.zone, s.lat, s.lon, c.side, a.confidence, a.decision"
        " FROM assignments a"
        " JOIN flank_crops c  ON c.crop_id=a.crop_id"
        " JOIN detections  d  ON d.det_id=c.det_id"
        " JOIN images      im ON im.image_id=d.image_id"
        " JOIN stations    s  ON s.station_id=im.station_id"
        " WHERE a.ind_id=? AND a.superseded_by IS NULL"
        " ORDER BY im.captured_at DESC", (ind_id,)))


def latest_crop_path(ind_id: str) -> str | None:
    """The most recent real, rectified flank image on disk for this
    individual, if any -- edge/ui/app.js's stripe rail shows this in
    place of the procedurally generated pattern when it exists. No
    station join (unlike individual_captures()): an ad-hoc
    /api/identify/upload crop may have no station_id at all, and a
    thumbnail lookup does not need one."""
    row = _one(connect().execute(
        "SELECT c.path FROM assignments a"
        " JOIN flank_crops c ON c.crop_id=a.crop_id"
        " WHERE a.ind_id=? AND a.superseded_by IS NULL AND c.path IS NOT NULL"
        " ORDER BY a.decided_at DESC LIMIT 1", (ind_id,)))
    return row["path"] if row else None


def promote_individual(ind_id: str, actor: str) -> bool:
    conn = connect()
    before = individual(ind_id)
    if not before or not before["provisional"]:
        return False
    conn.execute("UPDATE individuals SET provisional=0 WHERE ind_id=?", (ind_id,))
    conn.commit()
    audit("individual.promote", actor=actor, entity_type="individual",
          entity_id=ind_id, before={"provisional": 1}, after={"provisional": 0})
    return True


# ── entities: the real unit of re-identification, blueprint §7.3 ─────────
# The ATRW paper (Li et al., ACM MM 2020) establishes that a tiger's left
# and right flank patterns are different and unrelated, and that capturing
# both sides of the same animal is rare in the wild -- so the unit of
# matching is the ENTITY (one side of one tiger), not the individual. See
# docs/DATA.md §1 and migration 0003_entities.sql.

def rebuild_entities(reserve_id: str | None = None) -> int:
    """Derives `entities` from confirmed assignments (superseded_by IS
    NULL) with a known side, and backfills assignments.entity_id. Safe
    to re-run at any time -- every entity write is a deterministic
    upsert keyed on (ind_id, side), never additive garbage. Crops with
    side='unknown' contribute to no entity: an entity IS a side, so
    there is no such thing as an unknown-side entity."""
    conn = connect()
    args: list = []
    reserve_filter = ""
    if reserve_id:
        reserve_filter = " AND i.reserve_id=?"
        args.append(reserve_id)
    rows = _rows(conn.execute(
        "SELECT a.ind_id, c.side, COUNT(*) n, MIN(im.captured_at) first_seen,"
        " MAX(im.captured_at) last_seen"
        " FROM assignments a"
        " JOIN flank_crops c ON c.crop_id = a.crop_id"
        " JOIN detections  d ON d.det_id = c.det_id"
        " JOIN images     im ON im.image_id = d.image_id"
        " JOIN individuals i ON i.ind_id = a.ind_id"
        " WHERE a.superseded_by IS NULL AND c.side IN ('L','R')" + reserve_filter +
        " GROUP BY a.ind_id, c.side", args))
    for r in rows:
        conn.execute(
            "INSERT INTO entities (entity_id, ind_id, side, crop_count, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(entity_id) DO UPDATE SET crop_count=excluded.crop_count,"
            " first_seen=excluded.first_seen, last_seen=excluded.last_seen",
            (f"{r['ind_id']}-{r['side']}", r["ind_id"], r["side"], r["n"],
             r["first_seen"], r["last_seen"]))
    conn.execute(
        "UPDATE assignments"
        "   SET entity_id = assignments.ind_id || '-' ||"
        "       (SELECT c.side FROM flank_crops c WHERE c.crop_id = assignments.crop_id)"
        " WHERE superseded_by IS NULL"
        "   AND crop_id IN (SELECT crop_id FROM flank_crops WHERE side IN ('L','R'))")
    conn.commit()
    return len(rows)


def entities(reserve_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT e.* FROM entities e JOIN individuals i ON i.ind_id = e.ind_id"
        " WHERE i.reserve_id=? ORDER BY e.entity_id", (reserve_id,)))


def single_flank(reserve_id: str) -> list[dict]:
    """Individuals photographed from only one side -- the common case in
    the field, per the paper (docs/DATA.md §1), not the edge case. A crop
    of the other side for one of these is neither a match nor evidence of
    a new tiger; it is unresolvable, and the UI has to be able to ask
    which individuals are in that state."""
    return _rows(connect().execute(
        "SELECT * FROM single_flank_individuals WHERE reserve_id=? ORDER BY ind_id",
        (reserve_id,)))


def catalogue_for_side(reserve_id: str, side: str) -> list[dict]:
    """The side-specific catalogue a crop is matched against. Raises
    rather than silently returning an empty or wrong list -- an L crop
    must never be scored against an R catalogue, and enforcing that by
    making a bad side unrepresentable here (instead of trusting every
    call site to remember) is the whole reason this function exists
    rather than a raw SELECT inline wherever matching happens."""
    if side not in ("L", "R"):
        raise ValueError(f"side must be 'L' or 'R', got {side!r}")
    return _rows(connect().execute(
        "SELECT e.* FROM entities e JOIN individuals i ON i.ind_id = e.ind_id"
        " WHERE i.reserve_id=? AND e.side=? ORDER BY e.entity_id", (reserve_id, side)))


def crop_embeddings_for_side(reserve_id: str, side: str) -> list[dict]:
    """The real matching catalogue for edge/pipeline/identify.py::match()
    -- every confirmed crop's embedding for one side, one row per crop
    (not collapsed to one row per entity: matching compares a query
    against individual gallery crops, not a centroid). Raises on a bad
    side for the same reason catalogue_for_side() does. embedding is the
    raw BLOB; deserialise with identify.deserialize_embedding()."""
    if side not in ("L", "R"):
        raise ValueError(f"side must be 'L' or 'R', got {side!r}")
    return _rows(connect().execute(
        "SELECT a.entity_id, a.ind_id, c.crop_id, c.embedding, c.embed_model_version"
        " FROM assignments a"
        " JOIN flank_crops c ON c.crop_id = a.crop_id"
        " JOIN detections  d ON d.det_id = c.det_id"
        " JOIN images     im ON im.image_id = d.image_id"
        " JOIN individuals i ON i.ind_id = a.ind_id"
        " WHERE i.reserve_id=? AND c.side=? AND a.superseded_by IS NULL"
        " AND c.embedding IS NOT NULL", (reserve_id, side)))


def save_crop_embedding(crop_id: str, embedding: bytes, model_version: str,
                         quality: float, rect_ok: bool) -> None:
    conn = connect()
    conn.execute(
        "UPDATE flank_crops SET embedding=?, embed_model_version=?, quality=?, rect_ok=?"
        " WHERE crop_id=?",
        (embedding, model_version, quality, int(rect_ok), crop_id))
    conn.commit()


def persons_restricted_for_image(image_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT * FROM persons_restricted WHERE image_id=?", (image_id,)))


# ── review queue ────────────────────────────────────────────────────────

def review_open(limit: int = 50) -> list[dict]:
    rows = _rows(connect().execute(
        "SELECT q.*, c.side, c.quality, c.path crop_path, im.station_id,"
        " im.captured_at, im.is_night"
        " FROM review_queue q"
        " JOIN flank_crops c ON c.crop_id=q.crop_id"
        " JOIN detections  d ON d.det_id=c.det_id"
        " JOIN images     im ON im.image_id=d.image_id"
        " WHERE q.state='open' ORDER BY q.priority DESC LIMIT ?", (limit,)))
    for r in rows:
        try:
            r["candidates"] = json.loads(r["candidates"])
        except json.JSONDecodeError:
            r["candidates"] = []
    return rows


def crop_path(crop_id: str) -> str | None:
    row = _one(connect().execute(
        "SELECT path FROM flank_crops WHERE crop_id=?", (crop_id,)))
    return row["path"] if row else None


def review_count() -> int:
    return connect().execute(
        "SELECT COUNT(*) c FROM review_queue WHERE state='open'").fetchone()["c"]


def review_decide(queue_id: str, ind_id: str, actor: str,
                  new_individual: bool = False) -> dict:
    """A human decision. Supersedes rather than overwrites, so the record of
    who thought what, when, and on what evidence is never destroyed."""
    conn = connect()
    q = _one(conn.execute("SELECT * FROM review_queue WHERE queue_id=?", (queue_id,)))
    if not q:
        raise KeyError(queue_id)
    prior = _one(conn.execute(
        "SELECT * FROM assignments WHERE crop_id=? AND superseded_by IS NULL",
        (q["crop_id"],)))
    assign_id = new_id("as_")
    conn.execute(
        "INSERT INTO assignments(assign_id, crop_id, ind_id, score, method,"
        " decision, confidence, decided_at, actor) VALUES (?,?,?,?,?,?,?,?,?)",
        (assign_id, q["crop_id"], ind_id, 1.0, "ensemble",
         "enrolled" if new_individual else "human", 1.0, now(), actor))
    if prior:
        conn.execute("UPDATE assignments SET superseded_by=? WHERE assign_id=?",
                     (assign_id, prior["assign_id"]))
    conn.execute("UPDATE review_queue SET state='done' WHERE queue_id=?", (queue_id,))
    conn.commit()
    audit("review.decide", actor=actor, entity_type="crop", entity_id=q["crop_id"],
          before={"ind_id": prior["ind_id"]} if prior else None,
          after={"ind_id": ind_id, "new_individual": new_individual})
    return {"assign_id": assign_id, "remaining": review_count()}


# ── occupancy ───────────────────────────────────────────────────────────

def occupancy(run_id: str) -> list[dict]:
    rows = _rows(connect().execute(
        "SELECT o.*, i.provisional FROM occupancy o"
        " JOIN individuals i USING(ind_id) WHERE o.run_id=?"
        " ORDER BY o.area_km2 DESC NULLS LAST", (run_id,)))
    for r in rows:
        try:
            r["station_set"] = json.loads(r["station_set"])
        except json.JSONDecodeError:
            r["station_set"] = []
    return rows


def occupancy_history(reserve_id: str) -> dict[str, dict[str, dict]]:
    """Every occupancy row for a reserve, keyed by individual then run_id,
    oldest run first. The alert engine's only view into occupancy -- it
    never recomputes a hull or a centroid, only reads what Stage 4 wrote."""
    rows = _rows(connect().execute(
        "SELECT o.* FROM occupancy o JOIN runs r ON r.run_id = o.run_id"
        " WHERE r.reserve_id=? ORDER BY r.started_at", (reserve_id,)))
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        try:
            r["station_set"] = json.loads(r["station_set"])
        except json.JSONDecodeError:
            r["station_set"] = []
        out.setdefault(r["ind_id"], {})[r["run_id"]] = r
    return out


def mean_assignment_confidence(run_id: str, ind_id: str) -> float | None:
    """The identity confidence behind an individual's captures in a run.
    An alert about what a tiger did cannot be more confident than the
    match that says which tiger it was; see blueprint §9.4."""
    row = _one(connect().execute(
        "SELECT AVG(a.confidence) v FROM assignments a"
        " JOIN flank_crops c ON c.crop_id=a.crop_id"
        " JOIN detections  d ON d.det_id=c.det_id"
        " JOIN images     im ON im.image_id=d.image_id"
        " WHERE im.run_id=? AND a.ind_id=? AND a.superseded_by IS NULL",
        (run_id, ind_id)))
    v = row["v"] if row else None
    return round(v, 3) if v is not None else None


# ── alerts ──────────────────────────────────────────────────────────────

_SEVERITY_ORDER = "CASE severity WHEN 'act' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END"


def alerts(run_id: str, suppressed: bool = False) -> list[dict]:
    rows = _rows(connect().execute(
        f"SELECT * FROM alerts WHERE run_id=? AND suppressed=?"
        f" ORDER BY {_SEVERITY_ORDER}, confidence DESC",
        (run_id, 1 if suppressed else 0)))
    for r in rows:
        try:
            r["evidence"] = json.loads(r["evidence"])
        except json.JSONDecodeError:
            r["evidence"] = {}
    return rows


def alert_counts(run_id: str) -> dict:
    row = _one(connect().execute(
        "SELECT SUM(suppressed=0 AND severity='act') act,"
        " SUM(suppressed=0 AND severity='watch') watch,"
        " SUM(suppressed=0 AND severity='info') info,"
        " SUM(suppressed=1) suppressed FROM alerts WHERE run_id=?", (run_id,))) or {}
    return {k: (v or 0) for k, v in row.items()}


def acknowledge_alert(alert_id: str, actor: str) -> bool:
    conn = connect()
    cur = conn.execute(
        "UPDATE alerts SET acknowledged_by=?, acknowledged_at=?"
        " WHERE alert_id=? AND acknowledged_at IS NULL", (actor, now(), alert_id))
    conn.commit()
    if cur.rowcount:
        audit("alert.acknowledge", actor=actor, entity_type="alert", entity_id=alert_id)
    return bool(cur.rowcount)


# ── ops ─────────────────────────────────────────────────────────────────

def drift_indicators(reserve_id: str) -> list[dict]:
    """Per-run health signals. A rising review rate or a falling mean score
    is the earliest sign the model no longer fits the data — new camera
    hardware, a new season, a new reserve."""
    return _rows(connect().execute(
        "SELECT r.run_id, r.cycle_label, r.started_at,"
        " (SELECT COUNT(*) FROM images i WHERE i.run_id=r.run_id) images,"
        " (SELECT COUNT(*) FROM images i WHERE i.run_id=r.run_id"
        "    AND i.status='quarantined') blanks,"
        " (SELECT ROUND(AVG(a.confidence),3) FROM assignments a"
        "    WHERE a.superseded_by IS NULL AND a.decision='auto') mean_auto_conf,"
        " (SELECT COUNT(*) FROM review_queue q WHERE q.state='open') review_open"
        " FROM runs r WHERE r.reserve_id=? ORDER BY r.started_at DESC LIMIT 10",
        (reserve_id,)))


# ── scale/hardening extension (v0.2.0) ─────────────────────────────────
# Rule 1 still holds: one import path (`repo.`), one place to look.
# See edge/db/repo_ext.py for why the additions live in their own file.
from edge.db.repo_ext import *  # noqa: E402,F401,F403
