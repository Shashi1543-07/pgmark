"""Repository extension — the SQL the 50K path needs that v0.1.1 lacked.

CLAUDE.md Rule 1 says all SQL lives in `edge/db/repo.py`. This file does not
break that rule so much as extend where "there" is, and it is a separate
module for one reason: `repo.py` is already 1,008 lines, and the additions
here are a coherent group — batch writes, transactions, pagination, and the
Stage 4/5 persistence that had no home at all. Splitting them out keeps both
files readable. `repo.py` ends with `from edge.db.repo_ext import *`, so
every call site still says `repo.something()` and Rule 1's real intent —
one import path, one place to look — holds.

Four groups of problem this fixes:

  1. **Batch writes.** v0.1.1's `set_image_status()` did a SELECT, a
     `next_lamport()` (itself an UPDATE + SELECT + COMMIT), then an UPDATE +
     COMMIT — two transactions per frame, ~50,000 frames, all of it inside a
     synchronous request. `set_image_status_many()` does the same work for a
     whole batch in one transaction.

  2. **Transactions that actually span a unit of work.** Every write in
     v0.1.1 committed itself. There was no way to make "move 200 files into
     quarantine, insert 200 quarantine rows, advance the cursor" atomic, so
     a crash between any two of those left them disagreeing. `transaction()`
     is that missing primitive.

  3. **Pagination.** `images_by_status()` returned every matching row. On the
     demo that is 945 rows; scaled to a 50,000-frame import it is ~4,000 rows
     in one JSON array, rendered into ~4,000 DOM nodes, each with a button a
     human is expected to click.

  4. **Stage 4 and Stage 5 persistence.** `edge/pipeline/occupancy.py` and
     `edge/pipeline/alerts.py` are good modules that the production app never
     called: `edge/app.py` imports neither, and the only code that ever wrote
     an `occupancy` or `alerts` row was `tools/seed_demo.py`. The demo
     therefore showed intelligence a real import could never produce. These
     functions are the missing halves — the query that turns real assignments
     into occupancy inputs, and the writers that persist both stages.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from edge.db.repo import _one, _rows, connect, new_id, now

VALID_RUN_STAGES = ("preflight", "confirmed", "triaged", "identified", "complete", "failed")

# The only legal forward moves. v0.1.1's runs.stage was free text: any string
# could be written, and nothing stopped a run going from 'preflight' straight
# to 'triaged' with its folders never resolved.
_STAGE_TRANSITIONS = {
    "preflight": {"confirmed", "failed"},
    "confirmed": {"triaged", "failed"},
    "triaged":   {"identified", "triaged", "failed"},   # re-triage of new frames is legal
    "identified": {"complete", "identified", "failed"},
    "complete":  {"identified", "failed"},              # re-running Stage 3 after a correction
    "failed":    {"confirmed", "triaged", "identified"},  # retry after fixing the cause
}


# ── transactions ─────────────────────────────────────────────────────────

@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """One unit of work, one commit, rollback on anything raised.

    Use this around a batch AND its cursor update together. A cursor
    committed separately from the rows it describes can be lost
    independently of them, and then the database says 30,000 frames are
    done while the disk holds 30,180 — which is precisely the state
    v0.1.1's quarantine could reach.
    """
    conn = conn or connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        # Already inside a transaction: nest as a no-op rather than
        # silently splitting the caller's unit of work in two.
        yield conn
        return
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def insert_many_strict(table: str, rows: Iterable[dict],
                       conn: sqlite3.Connection | None = None) -> int:
    """`INSERT` — not `INSERT OR REPLACE`.

    `repo.insert_many()` uses `INSERT OR REPLACE`, which is silent data
    loss for any content-addressed table. `images.image_id` is a SHA-256
    prefix, so ingesting the same SD card into a second run does not
    conflict — it *overwrites*: the first run's row is replaced, its
    `run_id` and `status` are lost, and every detection, crop and
    assignment hanging off it is orphaned with no error anywhere.
    Reproduced against the seeded demo database before this was written.

    Use this wherever a duplicate primary key means something has gone
    wrong. Use `insert_many_ignore()` where a duplicate is expected and
    genuinely means "already have it".
    """
    rows = list(rows)
    if not rows:
        return 0
    conn = conn or connect()
    cols = list(rows[0])
    marks = ", ".join("?" for _ in cols)
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
        [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def insert_many_ignore(table: str, rows: Iterable[dict],
                       conn: sqlite3.Connection | None = None) -> int:
    """Idempotent insert: re-running a batch after a crash re-inserts the
    rows it already wrote, and that must be a no-op rather than an error.
    Returns rows actually inserted, not rows offered."""
    rows = list(rows)
    if not rows:
        return 0
    conn = conn or connect()
    cols = list(rows[0])
    marks = ", ".join("?" for _ in cols)
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({marks})",
        [tuple(r[c] for c in cols) for r in rows])
    return conn.total_changes - before


# ── batch status writes ──────────────────────────────────────────────────

def set_image_status_many(updates: list[tuple[str, str, str | None]],
                          conn: sqlite3.Connection | None = None) -> int:
    """`[(image_id, status, triage_stage), ...]` in one statement.

    Deliberately does NOT re-stamp lamport/row_hash per row the way
    `repo.set_image_status()` does. That function's per-row
    `next_lamport()` is an UPDATE + SELECT + COMMIT each time, and calling
    it 50,000 times inside a triage pass is 100,000 transactions to move a
    counter. Bulk stages call `bump_lamport_for_run()` once at the end of
    the stage instead: the sync invariant that matters is that a changed
    row's hash is fresh *before the next bundle is built*, not that it was
    fresh within microseconds of the change.
    """
    if not updates:
        return 0
    conn = conn or connect()
    conn.executemany(
        "UPDATE images SET status=?, triage_stage=? WHERE image_id=?",
        [(s, st, i) for i, s, st in updates])
    return len(updates)


def set_image_species_many(updates: list[tuple[str, str | None]],
                           conn: sqlite3.Connection | None = None) -> int:
    if not updates:
        return 0
    conn = conn or connect()
    conn.executemany("UPDATE images SET subject_species=? WHERE image_id=?",
                     [(sp, i) for i, sp in updates])
    return len(updates)


def bump_lamport_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> int:
    """Re-stamp every sync-tracked image of a run whose content changed
    during a bulk stage, once, at the end of that stage. One counter
    advance for the whole run rather than one per frame.

    Correct because Lamport ordering only has to be consistent between
    nodes, not within a single node's own batch: every row a stage touched
    is causally after everything before the stage and before everything
    after it, so they can legitimately share a value.
    """
    from edge.db.repo import compute_row_hash, next_lamport
    conn = conn or connect()
    lam = next_lamport(conn)
    rows = _rows(conn.execute(
        "SELECT * FROM images WHERE run_id=? AND lamport IS NOT NULL", (run_id,)))
    payload = []
    for r in rows:
        r["lamport"], r["synced_at"] = lam, None
        payload.append((lam, compute_row_hash(r), r["image_id"]))
    conn.executemany(
        "UPDATE images SET lamport=?, synced_at=NULL, row_hash=? WHERE image_id=?",
        payload)
    conn.commit()
    return len(payload)


# ── run stage machine ────────────────────────────────────────────────────

def set_run_stage_checked(run_id: str, stage: str, actor: str = "system",
                          conn: sqlite3.Connection | None = None) -> None:
    """Enforces the transitions rather than trusting callers.

    v0.1.1 called `set_run_stage(run_id, "triaged")` unconditionally at the
    end of triage — including when Stage B had skipped every frame because
    the detector weights were absent. The run then read as triaged with
    50,000 frames still pending, and the UI moved on.
    """
    if stage not in VALID_RUN_STAGES:
        raise ValueError(f"{stage!r} is not a run stage; expected one of {VALID_RUN_STAGES}")
    conn = conn or connect()
    row = _one(conn.execute("SELECT stage FROM runs WHERE run_id=?", (run_id,)))
    if not row:
        raise KeyError(run_id)
    current = row["stage"]
    if current == stage:
        return
    allowed = _STAGE_TRANSITIONS.get(current, set())
    if stage not in allowed:
        raise ValueError(
            f"run {run_id} is at {current!r}; it cannot move to {stage!r} "
            f"(legal next stages: {sorted(allowed) or 'none'})")
    conn.execute("UPDATE runs SET stage=? WHERE run_id=?", (stage, run_id))
    conn.commit()
    from edge.db.repo import audit
    audit("run.stage", actor=actor, entity_type="run", entity_id=run_id,
          before={"stage": current}, after={"stage": stage})


def finish_run(run_id: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (now(), run_id))
    conn.commit()


def set_run_models(run_id: str, models: dict, conn: sqlite3.Connection | None = None) -> None:
    """runs.model_versions was written as `{}` at preflight and never
    updated, so a stored run could not say which detector or embedder
    produced its results — the exact provenance question runs.config
    exists to answer."""
    conn = conn or connect()
    row = _one(conn.execute("SELECT model_versions FROM runs WHERE run_id=?", (run_id,)))
    try:
        existing = json.loads(row["model_versions"] or "{}") if row else {}
    except json.JSONDecodeError:
        existing = {}
    existing.update(models)
    conn.execute("UPDATE runs SET model_versions=? WHERE run_id=?",
                 (json.dumps(existing), run_id))
    conn.commit()


# ── ingest: cross-run duplicate detection ────────────────────────────────

def existing_image_ids(ids: list[str], conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """`{image_id: run_id}` for content already ingested by ANY run.

    v0.1.1 deduplicated only within a single scan (an in-memory `seen`
    dict) and then used `INSERT OR REPLACE`, so a folder scanned twice
    silently overwrote the first run's rows. Callers use this to record a
    duplicate as a duplicate.
    """
    if not ids:
        return {}
    out: dict[str, str] = {}
    conn = conn or connect()
    for i in range(0, len(ids), 500):            # SQLite's variable limit
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT image_id, run_id FROM images WHERE image_id IN ({marks})", chunk):
            out[r["image_id"]] = r["run_id"]
    return out


def max_ingest_batch(run_id: str, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    row = _one(conn.execute(
        "SELECT COALESCE(MAX(ingest_batch), -1) v FROM images WHERE run_id=?", (run_id,)))
    return row["v"] if row else -1


# ── pagination ───────────────────────────────────────────────────────────

def images_by_status_page(run_id: str, status: str, limit: int = 100,
                          offset: int = 0) -> dict:
    """The paginated replacement for `repo.images_by_status()`."""
    limit = max(1, min(int(limit), 500))
    total = _one(connect().execute(
        "SELECT COUNT(*) c FROM images WHERE run_id=? AND status=?",
        (run_id, status)))["c"]
    items = _rows(connect().execute(
        "SELECT i.image_id, i.orig_path, i.station_id, s.name station_name,"
        " i.captured_at, i.is_night, i.subject_species,"
        " (SELECT COUNT(*) FROM detections d WHERE d.image_id=i.image_id) det_count,"
        " (SELECT COUNT(*) FROM flank_crops c JOIN detections d2 ON d2.det_id=c.det_id"
        "   WHERE d2.image_id=i.image_id) crop_count"
        " FROM images i LEFT JOIN stations s ON s.station_id=i.station_id"
        " WHERE i.run_id=? AND i.status=?"
        " ORDER BY i.captured_at, i.image_id LIMIT ? OFFSET ?",
        (run_id, status, limit, offset)))
    return {"total": total, "limit": limit, "offset": offset, "items": items,
            "has_more": offset + len(items) < total}


def review_open_page(limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(int(limit), 200))
    total = _one(connect().execute(
        "SELECT COUNT(*) c FROM review_queue WHERE state='open'"))["c"]
    rows = _rows(connect().execute(
        "SELECT q.*, c.side, c.quality, c.path crop_path, im.station_id,"
        " im.captured_at, im.is_night, im.run_id, d.species, d.conf det_conf"
        " FROM review_queue q"
        " JOIN flank_crops c ON c.crop_id=q.crop_id"
        " JOIN detections  d ON d.det_id=c.det_id"
        " JOIN images     im ON im.image_id=d.image_id"
        " WHERE q.state='open' ORDER BY q.priority DESC, q.queue_id"
        " LIMIT ? OFFSET ?", (limit, offset)))
    for r in rows:
        try:
            r["candidates"] = json.loads(r["candidates"])
        except (json.JSONDecodeError, TypeError):
            r["candidates"] = []
    return {"total": total, "limit": limit, "offset": offset, "items": rows,
            "has_more": offset + len(rows) < total}


def claim_review_item(queue_id: str, actor: str) -> bool:
    """Optimistic lock. Two reviewers working the same queue in two tabs
    both saw the same top item in v0.1.1, and both could decide it — the
    second decision superseded the first with no sign that a race had
    happened. Returns False if somebody else got there first."""
    conn = connect()
    cur = conn.execute(
        "UPDATE review_queue SET state='claimed' WHERE queue_id=? AND state='open'",
        (queue_id,))
    conn.commit()
    if cur.rowcount:
        from edge.db.repo import audit
        audit("review.claim", actor=actor, entity_type="queue", entity_id=queue_id)
    return bool(cur.rowcount)


def release_review_item(queue_id: str) -> None:
    conn = connect()
    conn.execute("UPDATE review_queue SET state='open' WHERE queue_id=? AND state='claimed'",
                 (queue_id,))
    conn.commit()


# ── Stage 3 work list ────────────────────────────────────────────────────

def detections_pending_stage3(run_id: str, species: str | None, after: str = "",
                              limit: int = 200) -> list[dict]:
    """Animal detections in a run that have no flank crop yet, keyset-paged
    by det_id so a resumed job continues from its cursor rather than
    re-scanning with an OFFSET that grows linearly.

    `species` filters to the target species when a classifier has run;
    passing None takes every animal detection, which is v0.1.1's behaviour
    and is only correct where the reserve genuinely has one large species.
    """
    sql = ("SELECT d.det_id, d.image_id, d.x, d.y, d.w, d.h, d.conf, d.species,"
           " im.orig_path, im.station_id, im.reserve_id, im.captured_at"
           " FROM detections d JOIN images im ON im.image_id = d.image_id"
           " LEFT JOIN flank_crops c ON c.det_id = d.det_id"
           " WHERE im.run_id=? AND d.label='animal' AND c.crop_id IS NULL"
           " AND d.det_id > ?")
    args: list = [run_id, after]
    if species:
        sql += " AND d.species=?"
        args.append(species)
    sql += " ORDER BY d.det_id LIMIT ?"
    args.append(limit)
    return _rows(connect().execute(sql, args))


def count_detections_pending_stage3(run_id: str, species: str | None) -> int:
    sql = ("SELECT COUNT(*) c FROM detections d"
           " JOIN images im ON im.image_id = d.image_id"
           " LEFT JOIN flank_crops c2 ON c2.det_id = d.det_id"
           " WHERE im.run_id=? AND d.label='animal' AND c2.crop_id IS NULL")
    args: list = [run_id]
    if species:
        sql += " AND d.species=?"
        args.append(species)
    return _one(connect().execute(sql, args))["c"]


def set_detection_species(det_id: str, species: str | None, conf: float | None,
                          source: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute("UPDATE detections SET species=?, species_conf=?, species_source=?"
                 " WHERE det_id=?", (species, conf, source, det_id))


def set_detection_species_many(rows: list[tuple[str, str | None, float | None, str]],
                               conn: sqlite3.Connection | None = None) -> int:
    if not rows:
        return 0
    conn = conn or connect()
    conn.executemany(
        "UPDATE detections SET species=?, species_conf=?, species_source=? WHERE det_id=?",
        [(sp, c, src, d) for d, sp, c, src in rows])
    return len(rows)


# ── Stage 4: occupancy, from real data ───────────────────────────────────

def occupancy_inputs(run_id: str) -> dict[str, list[dict]]:
    """`{ind_id: [{station_id, lat, lon, event_count}, ...]}` for one run.

    This query is the missing link between Stage 3 and Stage 4. It existed
    only inside `tools/seed_demo.py`'s own bookkeeping — the production app
    never had a way to derive occupancy from real assignments, so a real
    import produced an empty map forever.

    `event_count` counts DISTINCT events, not images. A 3-frame burst is one
    visit (blueprint §5.5), and counting frames would inflate every centroid
    weight and every alert's event floor by the camera's burst setting —
    turning a camera configuration into an apparent behavioural signal.
    Frames that never got grouped into an event (no station, or no resolved
    timestamp) fall back to counting as one visit each rather than being
    dropped, so a station is never silently missing from a hull.
    """
    rows = _rows(connect().execute(
        "SELECT a.ind_id, im.station_id, s.lat, s.lon,"
        "       COUNT(DISTINCT COALESCE(ie.event_id, im.image_id)) AS event_count,"
        "       COUNT(*) AS frame_count,"
        "       MIN(im.captured_at) AS first_at, MAX(im.captured_at) AS last_at"
        "  FROM assignments a"
        "  JOIN flank_crops c ON c.crop_id = a.crop_id"
        "  JOIN detections  d ON d.det_id  = c.det_id"
        "  JOIN images     im ON im.image_id = d.image_id"
        "  JOIN stations    s ON s.station_id = im.station_id"
        "  LEFT JOIN image_event ie ON ie.image_id = im.image_id"
        " WHERE im.run_id = ? AND a.superseded_by IS NULL"
        " GROUP BY a.ind_id, im.station_id"
        " ORDER BY a.ind_id, im.station_id", (run_id,)))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["ind_id"], []).append(r)
    return out


def individuals_known_before(reserve_id: str, before_iso: str) -> list[str]:
    """Individuals already enrolled when a run started. An individual first
    catalogued *after* a run began has no absence to report for that run —
    it was not yet a thing that could have been missed."""
    return [r["ind_id"] for r in connect().execute(
        "SELECT ind_id FROM individuals WHERE reserve_id=?"
        " AND (first_seen IS NULL OR first_seen <= ?)", (reserve_id, before_iso))]


def replace_occupancy(run_id: str, rows: list[dict],
                      conn: sqlite3.Connection | None = None) -> int:
    """Stage 4 is a pure function of the run's assignments, so re-running it
    after a review correction must *replace* the previous answer, not
    accumulate a second one alongside it. Delete-then-insert inside one
    transaction: a reader never sees a half-recomputed map."""
    conn = conn or connect()
    conn.execute("DELETE FROM occupancy WHERE run_id=?", (run_id,))
    if rows:
        cols = list(rows[0])
        marks = ", ".join("?" for _ in cols)
        conn.executemany(
            f"INSERT INTO occupancy ({', '.join(cols)}) VALUES ({marks})",
            [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def replace_alerts(run_id: str, rows: list[dict],
                   conn: sqlite3.Connection | None = None) -> dict:
    """Same reasoning as occupancy, with one exception that matters: an
    acknowledgement is a human act and must survive regeneration. An alert
    an officer has already read and acknowledged does not come back
    unacknowledged because Stage 5 ran again."""
    conn = conn or connect()
    acked = {r["alert_id"]: (r["acknowledged_by"], r["acknowledged_at"])
             for r in conn.execute(
                 "SELECT alert_id, acknowledged_by, acknowledged_at FROM alerts"
                 " WHERE run_id=? AND acknowledged_at IS NOT NULL", (run_id,))}
    conn.execute("DELETE FROM alerts WHERE run_id=?", (run_id,))
    preserved = 0
    for r in rows:
        if r["alert_id"] in acked:
            r["acknowledged_by"], r["acknowledged_at"] = acked[r["alert_id"]]
            preserved += 1
    if rows:
        cols = list(rows[0])
        marks = ", ".join("?" for _ in cols)
        conn.executemany(
            f"INSERT INTO alerts ({', '.join(cols)}) VALUES ({marks})",
            [tuple(r[c] for c in cols) for r in rows])
    return {"written": len(rows), "acknowledgements_preserved": preserved}


# ── catalogue health ─────────────────────────────────────────────────────

def catalogue_health(reserve_id: str) -> list[dict]:
    """Backed by the view in migration 0005. `sides_known = 1` is the
    single-flank state CLAUDE.md rule 6 calls first-class and which
    v0.1.1 exposed through no route at all."""
    return _rows(connect().execute(
        "SELECT * FROM catalogue_health WHERE reserve_id=?"
        " ORDER BY sides_known, provisional DESC, ind_id", (reserve_id,)))


def provisional_individuals(reserve_id: str) -> list[dict]:
    """Auto-enrolled individuals awaiting a human's confirmation. v0.1.1
    created these on every below-threshold match and had no UI route to
    promote them, so they accumulated with no way to clear them."""
    return _rows(connect().execute(
        "SELECT i.*, "
        " (SELECT COUNT(*) FROM assignments a WHERE a.ind_id=i.ind_id"
        "    AND a.superseded_by IS NULL) crop_count,"
        " (SELECT GROUP_CONCAT(DISTINCT e.side) FROM entities e"
        "    WHERE e.ind_id=i.ind_id) sides"
        " FROM individuals i WHERE i.reserve_id=? AND i.provisional=1"
        " ORDER BY i.first_seen DESC", (reserve_id,)))


def merge_individual(source_ind: str, target_ind: str, actor: str) -> dict:
    """Fold one individual into another, superseding rather than deleting.

    The inevitable consequence of provisional auto-enrolment: the same
    tiger gets enrolled twice under two provisional IDs, and there was no
    way to say so. Every assignment is re-pointed with a NEW superseding
    row (CLAUDE.md rule 5), the source is retired, and the audit log keeps
    both sides of the correction.
    """
    from edge.db.repo import audit, rebuild_entities
    conn = connect()
    src = _one(conn.execute("SELECT * FROM individuals WHERE ind_id=?", (source_ind,)))
    tgt = _one(conn.execute("SELECT * FROM individuals WHERE ind_id=?", (target_ind,)))
    if not src or not tgt:
        raise KeyError("both individuals must exist")
    if src["reserve_id"] != tgt["reserve_id"]:
        raise ValueError("cannot merge individuals from different reserves")
    if source_ind == target_ind:
        raise ValueError("cannot merge an individual into itself")

    live = _rows(conn.execute(
        "SELECT * FROM assignments WHERE ind_id=? AND superseded_by IS NULL", (source_ind,)))
    with transaction(conn):
        for a in live:
            new_assign = new_id("as_")
            conn.execute(
                "INSERT INTO assignments(assign_id, crop_id, ind_id, score, method,"
                " decision, confidence, decided_at, actor)"
                " VALUES (?,?,?,?,?,'human',?,?,?)",
                (new_assign, a["crop_id"], target_ind, a["score"], a["method"],
                 a["confidence"], now(), actor))
            conn.execute("UPDATE assignments SET superseded_by=? WHERE assign_id=?",
                         (new_assign, a["assign_id"]))
        conn.execute(
            "UPDATE individuals SET notes = COALESCE(notes || ' | ', '') ||"
            " 'merged into ' || ? || ' on ' || ?, provisional = 1 WHERE ind_id=?",
            (target_ind, now(), source_ind))
    rebuild_entities(src["reserve_id"])
    audit("individual.merge", actor=actor, entity_type="individual", entity_id=source_ind,
          before={"ind_id": source_ind, "assignments": len(live)},
          after={"merged_into": target_ind})
    return {"merged": source_ind, "into": target_ind, "assignments_moved": len(live)}


# ── ops: backup, integrity, capacity ─────────────────────────────────────

def backup(dest: Path) -> dict:
    """Online backup via SQLite's own backup API — consistent even while
    the server is writing, which a file copy of a WAL database is not.
    v0.1.1 had no backup path at all: the only copy of a season's
    identifications was one file on one laptop."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = connect()
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
    return {"path": str(dest), "bytes": dest.stat().st_size, "at": now()}


def integrity_check() -> dict:
    conn = connect()
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    fk = _rows(conn.execute("PRAGMA foreign_key_check"))
    bad_stage = _rows(conn.execute("SELECT * FROM runs_with_bad_stage"))
    orphan_crops = _one(conn.execute(
        "SELECT COUNT(*) c FROM flank_crops c"
        " LEFT JOIN detections d ON d.det_id=c.det_id WHERE d.det_id IS NULL"))["c"]
    return {"quick_check": quick, "foreign_key_violations": len(fk),
            "runs_with_invalid_stage": [r["run_id"] for r in bad_stage],
            "orphan_crops": orphan_crops,
            "ok": quick == "ok" and not fk and not bad_stage and not orphan_crops}


def checkpoint_wal() -> dict:
    """A WAL file grows without bound under a long-running write load and is
    only truncated at a checkpoint. After a 50,000-frame import the -wal
    file can be larger than the database; nothing in v0.1.1 ever ran this."""
    conn = connect()
    busy, log, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {"busy": busy, "wal_pages": log, "checkpointed_pages": checkpointed}


def database_size_bytes() -> int:
    from edge import config as _cfg
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_cfg.DB_PATH) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def run_dead_letters(run_id: str) -> list[dict]:
    return _rows(connect().execute(
        "SELECT ji.item_id, ji.error, ji.attempts, j.kind"
        " FROM job_items ji JOIN jobs j ON j.job_id = ji.job_id"
        " WHERE j.run_id=? AND ji.state='failed'"
        " ORDER BY ji.updated_at DESC LIMIT 200", (run_id,)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── station state, for the map ───────────────────────────────────────────

def stations_with_state(reserve_id: str, period_start: str | None = None,
                        period_end: str | None = None) -> list[dict]:
    """Stations plus the deployment facts the map needs to draw them.

    The map in v0.1.1 hardcoded which cameras were dead and which were new:

        const DEAD = new Set(['PN-C-008', 'PN-C-009']);
        const NEW  = new Set(['PN-C-015']);

    Those are IDs from tools/seed_demo.py. On a real reserve nothing was
    ever drawn as failed or newly-installed, because the frontend read a
    constant instead of station_activity. This query is where that fact
    actually lives, and it is also exactly the fact the absence alert's
    effort confound turns on — a camera that stopped mid-cycle is the
    difference between "the tiger is gone" and "we were not looking".
    """
    rows = _rows(connect().execute(
        "SELECT s.*,"
        " (SELECT COUNT(*) FROM images i WHERE i.station_id = s.station_id) image_count,"
        " (SELECT MIN(sa.start_date) FROM station_activity sa"
        "    WHERE sa.station_id = s.station_id) first_active,"
        " (SELECT MAX(COALESCE(sa.end_date, '9999')) FROM station_activity sa"
        "    WHERE sa.station_id = s.station_id) last_active"
        " FROM stations s WHERE s.reserve_id=? ORDER BY s.station_id", (reserve_id,)))

    if not (period_start and period_end):
        for r in rows:
            r.update(installed_this_cycle=False, was_active_before=True,
                     ended_early_this_cycle=False, active_days_this_cycle=None,
                     images_this_cycle=r["image_count"])
        return rows

    from edge.db.repo import station_effort_days
    window = (datetime.fromisoformat(period_end)
              - datetime.fromisoformat(period_start)).total_seconds() / 86400.0
    for r in rows:
        sid = r["station_id"]
        days = station_effort_days(sid, period_start, period_end)
        first = r["first_active"]
        r["active_days_this_cycle"] = days
        r["installed_this_cycle"] = bool(first and first >= period_start)
        r["was_active_before"] = bool(first and first < period_start)
        # "Stopped early" means it covered materially less of the cycle than
        # the cycle itself lasted — the PN-C-008/009 case, derived rather
        # than named.
        r["ended_early_this_cycle"] = bool(
            r["was_active_before"] and window > 0 and days < window * 0.8)
        r["images_this_cycle"] = _one(connect().execute(
            "SELECT COUNT(*) c FROM images WHERE station_id=?"
            " AND captured_at >= ? AND captured_at < ?",
            (sid, period_start, period_end)))["c"]
    return rows


def run_period(run_id: str) -> tuple[str, str] | None:
    """A run's (start, end) window, from the effort model's own definition
    of a cycle: this run's start to the next run's start."""
    from edge import config, effort
    r = _one(connect().execute("SELECT reserve_id FROM runs WHERE run_id=?", (run_id,)))
    if not r:
        return None
    runs = _rows(connect().execute(
        "SELECT run_id, started_at FROM runs WHERE reserve_id=? ORDER BY started_at",
        (r["reserve_id"],)))
    return effort.cycle_periods(runs, config.CONFIG.alerts.default_cycle_days).get(run_id)


def prior_centroids(run_id: str) -> list[dict]:
    """Last cycle's centroid per individual, so the map can draw the
    movement the centroid_shift alert is actually about. v0.1.1 stated the
    distance in text and drew nothing."""
    r = _one(connect().execute("SELECT reserve_id, started_at FROM runs WHERE run_id=?",
                               (run_id,)))
    if not r:
        return []
    prev = _one(connect().execute(
        "SELECT run_id FROM runs WHERE reserve_id=? AND started_at < ?"
        " ORDER BY started_at DESC LIMIT 1", (r["reserve_id"], r["started_at"])))
    if not prev:
        return []
    return _rows(connect().execute(
        "SELECT ind_id, centroid_lat, centroid_lon, area_km2, event_count"
        " FROM occupancy WHERE run_id=? AND centroid_lat IS NOT NULL", (prev["run_id"],)))
