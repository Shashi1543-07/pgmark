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

__all__ = [
    "VALID_RUN_STAGES",
    "transaction",
    "insert_many_strict",
    "insert_many_ignore",
    "set_image_status_many",
    "set_image_species_many",
    "bump_lamport_for_run",
    "set_run_stage_checked",
    "finish_run",
    "set_run_models",
    "existing_image_ids",
    "max_ingest_batch",
    "images_by_status_page",
    "review_open_page",
    "claim_review_item",
    "release_review_item",
    "detections_pending_stage3",
    "count_detections_pending_stage3",
    "set_detection_species",
    "set_detection_species_many",
    "record_flank_crop",
    "record_assignment",
    "queue_crop_review",
    "occupancy_inputs",
    "individuals_known_before",
    "replace_occupancy",
    "replace_alerts",
    "catalogue_health",
    "provisional_individuals",
    "merge_individual",
    "backup",
    "integrity_check",
    "checkpoint_wal",
    "database_size_bytes",
    "run_dead_letters",
    "utc_now",
    "stations_with_state",
    "run_period",
    "prior_centroids",
    "create_station",
    "update_station",
    "delete_station",
    "import_stations_csv",
    "import_stations_geojson",
    "export_stations_geojson",
    "add_station_activity",
    "station_deployments",
    "multi_signal_station_score",
    "create_cross_flank_candidate",
    "cross_flank_candidates",
    "confirm_cross_flank",
    "reject_cross_flank",
    "set_reserve_boundaries",
    "get_reserve_boundaries",
    "record_telemetry",
    "run_telemetry",
    "expire_stale_claims",
    "set_image_terminal_status",
    "run_status_counts",
]


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


def review_open_page(limit: int = 50, offset: int = 0,
                     actor: str | None = None) -> dict:
    """One page of the human review queue.

    `actor` matters more than it looks. The review screen claims the item it
    is displaying, and a claim moves it out of state='open'. Without the
    claimed_by clause below, a reviewer who simply reloaded the page no
    longer saw the item they were working on -- it was filtered out by their
    own lock -- and the next item slid up into its place. The screen still
    said "1 of 50", so it read exactly like the decision had been thrown
    away, which is not what had happened at all. A reviewer must always see
    their own claims; only somebody else's are hidden.
    """
    limit = max(1, min(int(limit), 200))
    expire_stale_claims()
    mine = "(q.state='open' OR (q.state='claimed' AND q.claimed_by=?))" if actor \
        else "q.state='open'"
    args: tuple = (actor,) if actor else ()
    total = _one(connect().execute(
        "SELECT COUNT(*) c FROM review_queue q WHERE " + mine, args))["c"]
    rows = _rows(connect().execute(
        "SELECT q.*, c.side, c.quality, c.path crop_path, c.rect_ok, im.station_id,"
        " im.captured_at, im.is_night, im.run_id, im.image_id, d.species, d.conf det_conf"
        " FROM review_queue q"
        " JOIN flank_crops c ON c.crop_id=q.crop_id"
        " JOIN detections  d ON d.det_id=c.det_id"
        " JOIN images     im ON im.image_id=d.image_id"
        " WHERE " + mine + " ORDER BY q.priority DESC, q.queue_id"
        " LIMIT ? OFFSET ?", args + (limit, offset)))
    for r in rows:
        try:
            r["candidates"] = json.loads(r["candidates"])
        except (json.JSONDecodeError, TypeError):
            r["candidates"] = []
    held = _one(connect().execute(
        "SELECT COUNT(*) c FROM review_queue WHERE state='claimed'"
        + (" AND (claimed_by IS NULL OR claimed_by<>?)" if actor else ""), args))["c"]
    # "open" is the key the review screen reads for its badge. It was absent,
    # so the badge silently fell back to len(items) -- i.e. the page size --
    # and never moved. "held" surfaces items locked by another session so a
    # shrinking queue is explainable rather than mysterious.
    return {"total": total, "open": total, "held": held,
            "limit": limit, "offset": offset, "items": rows,
            "has_more": offset + len(rows) < total}


def claim_review_item(queue_id: str, actor: str) -> bool:
    """Optimistic lock. Two reviewers working the same queue in two tabs
    both saw the same top item in v0.1.1, and both could decide it — the
    second decision superseded the first with no sign that a race had
    happened. Returns False if somebody else got there first."""
    conn = connect()
    expire_stale_claims()
    cur = conn.execute(
        "UPDATE review_queue SET state='claimed', claimed_by=?,"
        " claimed_at=datetime('now') WHERE queue_id=? AND state='open'",
        (actor, queue_id))
    if not cur.rowcount:
        # Re-claiming an item this same reviewer already holds is not a
        # collision. Before this branch existed, a reviewer who reloaded the
        # tab collided with their own stale lock and got a red 409 banner
        # over an item nobody else had touched.
        cur = conn.execute(
            "UPDATE review_queue SET claimed_at=datetime('now')"
            " WHERE queue_id=? AND state='claimed' AND claimed_by=?",
            (queue_id, actor))
    conn.commit()
    if cur.rowcount:
        from edge.db.repo import audit
        audit("review.claim", actor=actor, entity_type="queue", entity_id=queue_id)
    return bool(cur.rowcount)


def expire_stale_claims(ttl_minutes: int = 15) -> int:
    """Return claims held longer than the TTL to the open queue.

    A claim exists to stop two reviewers deciding the same frame at the same
    moment. It is not a permanent assignment, and a closed browser tab must
    not be able to remove a frame from the reserve's workload for good.
    Called on every claim and on every page read, so the queue heals itself
    without an operator knowing the concept of a lock exists.
    """
    conn = connect()
    cur = conn.execute(
        "UPDATE review_queue SET state='open', claimed_by=NULL, claimed_at=NULL"
        " WHERE state='claimed' AND (claimed_at IS NULL OR"
        "       claimed_at <= datetime('now', ?))",
        (f"-{int(ttl_minutes)} minutes",))
    conn.commit()
    return cur.rowcount


def release_review_item(queue_id: str) -> None:
    conn = connect()
    conn.execute("UPDATE review_queue SET state='open', claimed_by=NULL,"
                 " claimed_at=NULL WHERE queue_id=? AND state='claimed'",
                 (queue_id,))
    conn.commit()


def crop_context(crop_id: str) -> dict | None:
    """Everything needed to re-run identification for one crop from scratch:
    the original image, the detector's box, and the reserve it belongs to.
    Used when a human confirms the physical side an automatic classifier
    could not, so Stage 3's rectify -> embed -> match -> decide chain can
    run for real instead of leaving the crop permanently unmatched."""
    return _one(connect().execute(
        "SELECT c.crop_id, d.det_id, d.x, d.y, d.w, d.h, im.image_id,"
        " im.orig_path, im.reserve_id, im.run_id"
        " FROM flank_crops c"
        " JOIN detections d ON d.det_id = c.det_id"
        " JOIN images im ON im.image_id = d.image_id"
        " WHERE c.crop_id=?", (crop_id,)))


def update_flank_crop_analysis(crop_id: str, side: str, rect_ok: bool, quality: float,
                               path: str | None, embedding: bytes | None,
                               embed_model_version: str | None, side_confidence: float,
                               side_source: str, side_model_version: str | None,
                               conn=None) -> None:
    """Overwrite a crop's analysis in place once a human-confirmed side lets
    Stage 3 actually finish it. This completes the SAME crop's unfinished
    analysis -- it never had an embedding to begin with, so there is no
    prior decision being discarded (rule 5 governs assignments, who the
    tiger is, not this row)."""
    owns = conn is None
    conn = conn or connect()
    conn.execute(
        "UPDATE flank_crops SET side=?, rect_ok=?, quality=?, path=?, embedding=?,"
        " embed_model_version=?, side_confidence=?, side_source=?, side_model_version=?"
        " WHERE crop_id=?",
        (side, int(rect_ok), quality, path, embedding, embed_model_version,
         side_confidence, side_source, side_model_version, crop_id))
    if owns:
        conn.commit()


def close_review_items_for_crop(crop_id: str) -> int:
    """Close whatever review-queue state a crop was in (open or claimed) once
    it has been acted on some other way -- a human-confirmed side that let
    Stage 3 finish the analysis, for instance. Creates no assignment; that
    is the caller's job once it knows the real outcome."""
    conn = connect()
    cur = conn.execute(
        "UPDATE review_queue SET state='done' WHERE crop_id=? AND state IN ('open','claimed')",
        (crop_id,))
    conn.commit()
    return cur.rowcount


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
                          source: str, conn: sqlite3.Connection | None = None,
                          *, model_version: str | None = None) -> None:
    owns_connection = conn is None
    conn = conn or connect()
    conn.execute("UPDATE detections SET species=?, species_conf=?, species_source=?,"
                 " species_model_version=? WHERE det_id=?",
                 (species, conf, source, model_version, det_id))
    if owns_connection:
        conn.commit()


def set_detection_species_many(rows: list[tuple[str, str | None, float | None, str]],
                               conn: sqlite3.Connection | None = None) -> int:
    if not rows:
        return 0
    conn = conn or connect()
    conn.executemany(
        "UPDATE detections SET species=?, species_conf=?, species_source=? WHERE det_id=?",
        [(sp, c, src, d) for d, sp, c, src in rows])
    return len(rows)


def record_flank_crop(*, crop_id: str, det_id: str, side: str, rect_ok: bool,
                      quality: float, path: str | None, embedding: bytes | None,
                      embed_model_version: str | None, side_confidence: float | None,
                      side_source: str | None, side_model_version: str | None,
                      conn: sqlite3.Connection | None = None) -> None:
    """Persist one crop plus the independent flank-side evidence.

    ``quality`` remains pose/rectification quality. ``side_confidence`` is
    only the separate side classifier's confidence, never a copied keypoint
    score. A ``side='unknown'`` row is a terminal, reviewable refusal and is
    deliberately excluded from side-specific catalogue queries.
    """
    if side not in ("L", "R", "unknown"):
        raise ValueError(f"invalid flank side {side!r}")
    owns_connection = conn is None
    conn = conn or connect()
    conn.execute(
        "INSERT INTO flank_crops(crop_id, det_id, side, rect_ok, quality, path,"
        " embedding, embed_model_version, side_confidence, side_source, side_model_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (crop_id, det_id, side, int(rect_ok), quality, path, embedding,
         embed_model_version, side_confidence, side_source, side_model_version))
    if owns_connection:
        conn.commit()


def record_assignment(crop_id: str, ind_id: str, score: float, decision: str,
                      actor: str, conn: sqlite3.Connection | None = None) -> str:
    """Create an auditable assignment, superseding rather than overwriting."""
    owns_connection = conn is None
    conn = conn or connect()
    assign_id = new_id("as_")
    prior = _one(conn.execute(
        "SELECT assign_id FROM assignments WHERE crop_id=? AND superseded_by IS NULL",
        (crop_id,)))
    conn.execute(
        "INSERT INTO assignments(assign_id, crop_id, ind_id, score, method, decision,"
        " confidence, decided_at, actor) VALUES (?,?,?,?,'embed',?,?,?,?)",
        (assign_id, crop_id, ind_id, score, decision, score, now(), actor))
    if prior:
        conn.execute("UPDATE assignments SET superseded_by=? WHERE assign_id=?",
                     (assign_id, prior["assign_id"]))
    if owns_connection:
        conn.commit()
    return assign_id


def queue_crop_review(crop_id: str, candidates: list[dict], priority: float,
                      reason: str, conn: sqlite3.Connection | None = None) -> str:
    """Queue a refusal/ambiguous result without turning it into an identity."""
    owns_connection = conn is None
    conn = conn or connect()
    queue_id = new_id("rq_")
    conn.execute(
        "INSERT INTO review_queue(queue_id, crop_id, candidates, priority, reason, state)"
        " VALUES (?,?,?,?,?,'open')",
        (queue_id, crop_id, json.dumps(candidates), priority, reason))
    if owns_connection:
        conn.commit()
    return queue_id


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
        " (SELECT MAX(i.captured_at) FROM images i WHERE i.station_id = s.station_id) last_image_at,"
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


# ── Station CRUD, CSV/GeoJSON import/export ──────────────────────────────────

def create_station(reserve_id: str, data: dict, actor: str = "system",
                   conn: sqlite3.Connection | None = None) -> str:
    """Create a camera station with optional camera body metadata and initial deployment."""
    from edge.db import repo
    conn = conn or repo.connect()
    sid = data.get("station_id") or repo.new_id("stn_")
    row = {
        "station_id": sid,
        "reserve_id": reserve_id,
        "name": data.get("name") or sid,
        "lat": float(data["lat"]),
        "lon": float(data["lon"]),
        "zone": data.get("zone", "core"),
        "village_dist_km": float(data.get("village_dist_km", 5.0)),
        "grid_cell": data.get("grid_cell"),
        "folder_hint": data.get("folder_hint"),
        "camera_make": data.get("camera_make"),
        "camera_model": data.get("camera_model"),
        "camera_serial": data.get("camera_serial"),
        "active_from": data.get("active_from") or repo.now(),
        "active_to": data.get("active_to"),
        "status": data.get("status", "active"),
        "origin_node": repo.node_id(),
        "lamport": repo.next_lamport(),
        "synced_at": None,
    }
    row["row_hash"] = repo.compute_row_hash(row)
    repo.insert("stations", row, conn)

    # Initial station activity record
    act_id = repo.new_id("act_")
    repo.insert("station_activity", {
        "activity_id": act_id,
        "station_id": sid,
        "start_date": row["active_from"],
        "end_date": row["active_to"],
        "note": "station installation",
    }, conn)

    repo.audit("station.create", actor=actor, entity_type="station", entity_id=sid,
               after={"station_id": sid, "name": row["name"], "zone": row["zone"]})
    return sid


def update_station(station_id: str, data: dict, actor: str = "system",
                   conn: sqlite3.Connection | None = None) -> dict:
    from edge.db import repo
    conn = conn or repo.connect()
    existing = _one(conn.execute("SELECT * FROM stations WHERE station_id=?", (station_id,)))
    if not existing:
        raise KeyError(f"station {station_id} not found")

    sets, args = [], []
    for k in ("name", "lat", "lon", "zone", "village_dist_km", "grid_cell",
              "folder_hint", "camera_make", "camera_model", "camera_serial",
              "active_from", "active_to", "status"):
        if k in data:
            sets.append(f"{k}=?")
            args.append(data[k])
    if not sets:
        return existing

    args.append(station_id)
    conn.execute(f"UPDATE stations SET {', '.join(sets)} WHERE station_id=?", args)
    updated = _one(conn.execute("SELECT * FROM stations WHERE station_id=?", (station_id,)))
    repo.audit("station.update", actor=actor, entity_type="station", entity_id=station_id,
               after=updated)
    return updated


def delete_station(station_id: str, actor: str = "system",
                   conn: sqlite3.Connection | None = None) -> bool:
    from edge.db import repo
    conn = conn or repo.connect()
    imgs = _one(conn.execute("SELECT COUNT(*) c FROM images WHERE station_id=?", (station_id,)))["c"]
    if imgs > 0:
        raise ValueError(f"cannot delete station {station_id}: {imgs} images are attached")
    conn.execute("DELETE FROM station_activity WHERE station_id=?", (station_id,))
    conn.execute("DELETE FROM stations WHERE station_id=?", (station_id,))
    repo.audit("station.delete", actor=actor, entity_type="station", entity_id=station_id)
    return True


def import_stations_csv(reserve_id: str, csv_text: str, actor: str = "system") -> dict:
    """Import stations from CSV with columns: station_id, name, lat, lon, zone, village_dist_km, folder_hint, etc."""
    import csv
    import io
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    created, updated, errors = 0, 0, []
    from edge.db import repo
    for row_idx, row in enumerate(reader, 1):
        try:
            sid = (row.get("station_id") or row.get("id") or "").strip()
            lat = float(row.get("lat") or row.get("latitude") or 0.0)
            lon = float(row.get("lon") or row.get("longitude") or 0.0)
            if lat == 0.0 or lon == 0.0:
                raise ValueError("missing lat/lon")
            zone = (row.get("zone") or "core").strip().lower()
            if zone not in ("core", "buffer", "corridor"):
                zone = "core"
            station_data = {
                "station_id": sid,
                "name": row.get("name") or sid,
                "lat": lat,
                "lon": lon,
                "zone": zone,
                "village_dist_km": float(row.get("village_dist_km") or 5.0),
                "folder_hint": row.get("folder_hint") or sid,
                "camera_make": row.get("camera_make") or row.get("make"),
                "camera_model": row.get("camera_model") or row.get("model"),
                "camera_serial": row.get("camera_serial") or row.get("serial"),
                "grid_cell": row.get("grid_cell"),
            }
            existing = _one(repo.connect().execute("SELECT station_id FROM stations WHERE station_id=?", (sid,)))
            if existing:
                update_station(sid, station_data, actor)
                updated += 1
            else:
                create_station(reserve_id, station_data, actor)
                created += 1
        except Exception as exc:
            errors.append(f"Row {row_idx}: {exc}")
    return {"created": created, "updated": updated, "errors": errors}


def import_stations_geojson(reserve_id: str, geojson_text: str, actor: str = "system") -> dict:
    """Import stations from GeoJSON FeatureCollection with Point features."""
    from edge.db import repo
    fc = json.loads(geojson_text)
    features = fc.get("features", []) if isinstance(fc, dict) else []
    created, updated, errors = 0, 0, []
    for idx, f in enumerate(features, 1):
        try:
            geom = f.get("geometry") or {}
            props = f.get("properties") or {}
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            lon, lat = float(coords[0]), float(coords[1])
            sid = str(props.get("station_id") or props.get("id") or f.get("id") or repo.new_id("stn_")).strip()
            zone = str(props.get("zone") or "core").strip().lower()
            if zone not in ("core", "buffer", "corridor"):
                zone = "core"
            station_data = {
                "station_id": sid,
                "name": props.get("name") or sid,
                "lat": lat,
                "lon": lon,
                "zone": zone,
                "village_dist_km": float(props.get("village_dist_km") or 5.0),
                "folder_hint": props.get("folder_hint") or sid,
                "camera_make": props.get("camera_make"),
                "camera_model": props.get("camera_model"),
                "camera_serial": props.get("camera_serial"),
            }
            existing = _one(repo.connect().execute("SELECT station_id FROM stations WHERE station_id=?", (sid,)))
            if existing:
                update_station(sid, station_data, actor)
                updated += 1
            else:
                create_station(reserve_id, station_data, actor)
                created += 1
        except Exception as exc:
            errors.append(f"Feature {idx}: {exc}")
    return {"created": created, "updated": updated, "errors": errors}


def export_stations_geojson(reserve_id: str) -> dict:
    from edge.db import repo
    stns = repo.stations(reserve_id)
    features = []
    for s in stns:
        features.append({
            "type": "Feature",
            "id": s["station_id"],
            "geometry": {
                "type": "Point",
                "coordinates": [s["lon"], s["lat"]]
            },
            "properties": {
                "station_id": s["station_id"],
                "name": s["name"],
                "zone": s["zone"],
                "village_dist_km": s.get("village_dist_km"),
                "folder_hint": s.get("folder_hint"),
                "camera_make": s.get("camera_make"),
                "camera_model": s.get("camera_model"),
                "camera_serial": s.get("camera_serial"),
            }
        })
    return {"type": "FeatureCollection", "features": features}


# ── Deployment intervals ───────────────────────────────────────────────────

def add_station_activity(station_id: str, start_date: str, end_date: str | None = None,
                         note: str | None = None, conn: sqlite3.Connection | None = None) -> str:
    from edge.db import repo
    conn = conn or repo.connect()
    act_id = repo.new_id("act_")
    repo.insert("station_activity", {
        "activity_id": act_id,
        "station_id": station_id,
        "start_date": start_date,
        "end_date": end_date,
        "note": note,
    }, conn)
    return act_id


def station_deployments(station_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    from edge.db import repo
    conn = conn or repo.connect()
    return _rows(conn.execute(
        "SELECT * FROM station_activity WHERE station_id=? ORDER BY start_date",
        (station_id,)))


# ── Multi-signal station ID scoring ────────────────────────────────────────

def _levenshtein_ratio(s1: str, s2: str) -> float:
    import re
    a = re.sub(r"[^A-Z0-9]", "", (s1 or "").upper())
    b = re.sub(r"[^A-Z0-9]", "", (s2 or "").upper())
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    from edge.pipeline.ingest import _levenshtein
    dist = _levenshtein(a, b)
    max_len = max(len(a), len(b))
    return max(0.0, 1.0 - dist / max_len)


def multi_signal_station_score(file_rec: dict, station: dict, cfg=None) -> tuple[float, list[str]]:
    """Score matching confidence for a file against a station record using multiple signals."""
    weights = {
        "folder": 0.40,
        "serial": 0.25,
        "make_model": 0.15,
        "filename": 0.10,
        "deployment": 0.10,
    }
    signals = []
    total_score = 0.0

    # 1. Folder match
    folder_ratio = _levenshtein_ratio(file_rec.get("folder", ""), station.get("folder_hint") or station.get("name") or "")
    if folder_ratio > 0.6:
        total_score += weights["folder"] * folder_ratio
        signals.append(f"folder({folder_ratio:.2f})")

    # 2. Camera serial match
    rec_serial = file_rec.get("serial")
    stn_serial = station.get("camera_serial")
    if rec_serial and stn_serial:
        if str(rec_serial).strip() == str(stn_serial).strip():
            total_score += weights["serial"]
            signals.append("serial_match")
        elif str(rec_serial).strip() in str(stn_serial) or str(stn_serial) in str(rec_serial):
            total_score += weights["serial"] * 0.7
            signals.append("serial_partial")

    # 3. Make and Model match
    rec_body = f"{file_rec.get('make') or ''} {file_rec.get('model') or ''}".strip().upper()
    stn_body = f"{station.get('camera_make') or ''} {station.get('camera_model') or ''}".strip().upper()
    if rec_body and stn_body and (rec_body in stn_body or stn_body in rec_body):
        total_score += weights["make_model"]
        signals.append("body_match")

    # 4. Filename pattern match against station name/hint
    fn_ratio = _levenshtein_ratio(file_rec.get("orig_path", "").split("/")[-1].split("\\")[-1], station.get("name", ""))
    if fn_ratio > 0.4:
        total_score += weights["filename"] * fn_ratio
        signals.append(f"filename({fn_ratio:.2f})")

    # 5. Deployment window match
    dt = file_rec.get("captured_at") or file_rec.get("exif_dt")
    if dt and station.get("active_from"):
        dt_iso = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        if dt_iso >= str(station["active_from"]) and (not station.get("active_to") or dt_iso <= str(station["active_to"])):
            total_score += weights["deployment"]
            signals.append("in_deployment_window")

    return min(1.0, round(total_score, 3)), signals


# ── Cross-flank association tracking ───────────────────────────────────────

def create_cross_flank_candidate(reserve_id: str, l_ind_id: str, r_ind_id: str,
                                  confidence: float, evidence: dict,
                                  conn: sqlite3.Connection | None = None) -> str:
    from edge.db import repo
    conn = conn or repo.connect()
    assoc_id = repo.new_id("xflank_")
    row = {
        "assoc_id": assoc_id,
        "reserve_id": reserve_id,
        "l_ind_id": l_ind_id,
        "r_ind_id": r_ind_id,
        "status": "UNKNOWN_RELATIONSHIP",
        "confidence": round(confidence, 4),
        "evidence": json.dumps(evidence),
        "confirmed_by": None,
        "confirmed_at": None,
        "created_at": repo.now(),
    }
    repo.insert("cross_flank_associations", row, conn)
    repo.audit("cross_flank.candidate", actor="system", entity_type="individual",
               entity_id=l_ind_id, after={"assoc_id": assoc_id, "l_ind": l_ind_id, "r_ind": r_ind_id})
    return assoc_id


def cross_flank_candidates(reserve_id: str, status: str | None = None,
                           conn: sqlite3.Connection | None = None) -> list[dict]:
    from edge.db import repo
    conn = conn or repo.connect()
    if status:
        return _rows(conn.execute(
            "SELECT * FROM cross_flank_associations WHERE reserve_id=? AND status=? ORDER BY created_at DESC",
            (reserve_id, status)))
    return _rows(conn.execute(
        "SELECT * FROM cross_flank_associations WHERE reserve_id=? ORDER BY created_at DESC",
        (reserve_id,)))


def confirm_cross_flank(assoc_id: str, primary_ind_id: str, actor: str) -> dict:
    """Confirm that two sided individuals are the same physical tiger, merging them."""
    from edge.db import repo
    conn = repo.connect()
    assoc = _one(conn.execute("SELECT * FROM cross_flank_associations WHERE assoc_id=?", (assoc_id,)))
    if not assoc:
        raise KeyError(f"cross flank association {assoc_id} not found")
    source_ind = assoc["r_ind_id"] if primary_ind_id == assoc["l_ind_id"] else assoc["l_ind_id"]

    # Merge source individual into primary
    merge_result = merge_individual(source_ind, primary_ind_id, actor)
    conn.execute(
        "UPDATE cross_flank_associations SET status='CONFIRMED', confirmed_by=?, confirmed_at=? WHERE assoc_id=?",
        (actor, repo.now(), assoc_id))
    conn.commit()
    repo.audit("cross_flank.confirm", actor=actor, entity_type="cross_flank", entity_id=assoc_id,
               after={"primary_ind": primary_ind_id, "merged_ind": source_ind})
    return {"assoc_id": assoc_id, "status": "CONFIRMED", "merge": merge_result}


def reject_cross_flank(assoc_id: str, actor: str) -> dict:
    """Reject cross flank hypothesis; keep both entities as distinct individuals."""
    from edge.db import repo
    conn = repo.connect()
    conn.execute(
        "UPDATE cross_flank_associations SET status='REJECTED', confirmed_by=?, confirmed_at=? WHERE assoc_id=?",
        (actor, repo.now(), assoc_id))
    conn.commit()
    repo.audit("cross_flank.reject", actor=actor, entity_type="cross_flank", entity_id=assoc_id)
    return {"assoc_id": assoc_id, "status": "REJECTED"}


# ── Reserve boundary GeoJSON layers ────────────────────────────────────────

def set_reserve_boundaries(reserve_id: str, boundaries: dict, actor: str = "system") -> dict:
    from edge.db import repo
    conn = repo.connect()
    sets, args = [], []
    for k in ("boundary_geojson", "core_geojson", "buffer_geojson", "corridor_geojson"):
        if k in boundaries:
            sets.append(f"{k}=?")
            val = boundaries[k]
            args.append(json.dumps(val) if isinstance(val, (dict, list)) else val)
    if not sets:
        return get_reserve_boundaries(reserve_id)
    args.append(reserve_id)
    conn.execute(f"UPDATE reserves SET {', '.join(sets)} WHERE reserve_id=?", args)
    conn.commit()
    repo.audit("reserve.boundaries_update", actor=actor, entity_type="reserve", entity_id=reserve_id)
    return get_reserve_boundaries(reserve_id)


def get_reserve_boundaries(reserve_id: str) -> dict:
    from edge.db import repo
    r = _one(repo.connect().execute(
        "SELECT reserve_id, name, utm_epsg, boundary_geojson, core_geojson, buffer_geojson, corridor_geojson"
        " FROM reserves WHERE reserve_id=?", (reserve_id,)))
    if not r:
        raise KeyError(f"reserve {reserve_id} not found")
    out = dict(r)
    for k in ("boundary_geojson", "core_geojson", "buffer_geojson", "corridor_geojson"):
        if out.get(k):
            try:
                out[k] = json.loads(out[k])
            except Exception:
                pass
    return out


# ── Run telemetry ──────────────────────────────────────────────────────────

def record_telemetry(run_id: str, metrics: dict, conn: sqlite3.Connection | None = None) -> str:
    from edge.db import repo
    conn = conn or repo.connect()
    telem_id = repo.new_id("tel_")
    row = {
        "telemetry_id": telem_id,
        "run_id": run_id,
        "images_per_sec": metrics.get("images_per_sec"),
        "gpu_util": metrics.get("gpu_util"),
        "vram_used_mb": metrics.get("vram_used_mb"),
        "vram_total_mb": metrics.get("vram_total_mb"),
        "cpu_util": metrics.get("cpu_util"),
        "ram_used_mb": metrics.get("ram_used_mb"),
        "disk_read_mb": metrics.get("disk_read_mb"),
        "disk_write_mb": metrics.get("disk_write_mb"),
        "timing_decode_s": metrics.get("timing_decode_s"),
        "timing_detect_s": metrics.get("timing_detect_s"),
        "timing_species_s": metrics.get("timing_species_s"),
        "timing_side_s": metrics.get("timing_side_s"),
        "timing_keypoints_s": metrics.get("timing_keypoints_s"),
        "timing_identify_s": metrics.get("timing_identify_s"),
        "timing_db_s": metrics.get("timing_db_s"),
        "status_counts": json.dumps(metrics.get("status_counts", {})),
        "recorded_at": repo.now(),
    }
    repo.insert("run_telemetry", row, conn)
    return telem_id


def run_telemetry(run_id: str) -> list[dict]:
    from edge.db import repo
    rows = _rows(repo.connect().execute(
        "SELECT * FROM run_telemetry WHERE run_id=? ORDER BY recorded_at DESC", (run_id,)))
    for r in rows:
        try:
            r["status_counts"] = json.loads(r["status_counts"])
        except Exception:
            r["status_counts"] = {}
    return rows


# ── Terminal status & counts ───────────────────────────────────────────────

def set_image_terminal_status(image_id: str, status: str, error_stage: str | None = None,
                              error_type: str | None = None, last_error: str | None = None,
                              conn: sqlite3.Connection | None = None) -> None:
    """Set terminal status on an image row.

    When ``conn`` is supplied (bulk batch path), the caller owns the
    transaction and commit.  When called without a connection (single-image
    upload path) we open our own connection and commit immediately so the
    status is durable even if the caller never commits.
    """
    from edge.db import repo
    _own_conn = conn is None
    conn = conn or repo.connect()
    conn.execute(
        "UPDATE images SET status=?, error_stage=?, error_type=?, last_error=?"
        " WHERE image_id=?",
        (status, error_stage, error_type, last_error, image_id))
    if _own_conn:
        conn.commit()


def run_status_counts(run_id: str, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    from edge.db import repo
    conn = conn or repo.connect()
    rows = _rows(conn.execute("SELECT status, COUNT(*) as c FROM images WHERE run_id=? GROUP BY status", (run_id,)))
    return {r["status"]: r["c"] for r in rows}

