"""Database access. Every SQL statement in PUGMARK lives in this module.

Rule: no other file writes SQL. When a query is wrong there is exactly one
place to look, and when the schema changes there is exactly one place to fix.
"""
from __future__ import annotations

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
    on-disk manifest alone even if this database is lost."""
    conn = connect()
    n = conn.execute(
        "SELECT COUNT(*) c FROM quarantine WHERE run_id=? AND restored_at IS NULL",
        (run_id,)).fetchone()["c"]
    if not n:
        return 0
    conn.execute(
        "UPDATE quarantine SET restored_at=? WHERE run_id=? AND restored_at IS NULL",
        (now(), run_id))
    conn.execute(
        "UPDATE images SET status='pending' WHERE run_id=? AND status='quarantined'",
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
        "   WHERE a2.ind_id=i.ind_id AND a2.superseded_by IS NULL) AS sides"
        " FROM individuals i WHERE i.reserve_id=?"
        " ORDER BY i.provisional DESC, i.ind_id", (reserve_id,)))


def individual(ind_id: str) -> dict | None:
    return _one(connect().execute(
        "SELECT * FROM individuals WHERE ind_id=?", (ind_id,)))


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
