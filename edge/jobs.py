"""Durable background jobs. The thing v0.1.1 did not have.

v0.1.1 ran every stage inside the HTTP request that asked for it:

    POST /api/runs                     -> scan + hash + decode 50,000 files
    POST /api/runs/{id}/triage/run     -> Stage A + Stage B on all of them

Measured on a 4000x3000 camera-trap JPEG, Stage A alone is ~144 ms/frame,
which is two hours for 50,000 frames — inside one request, holding one
worker thread, with the browser's own timeout counting down, and with no
cursor anywhere. Close the laptop lid at frame 30,000 and there is no
record of which 30,000 were done.

This module is the fix, and it is deliberately small. There is no Celery,
no Redis, no message broker: the deployment target is one laptop with no
internet (CLAUDE.md), so the queue is the `jobs` table and the worker is a
thread.

Three guarantees, in the order they matter:

  1. **Restartable.** Every stage commits a cursor with the batch it just
     finished, in the SAME transaction as the batch's own rows. A crash
     loses at most one batch, never more, and never leaves rows committed
     that the cursor does not account for.
  2. **Observable.** `checkpoint()` is a heartbeat. A job whose
     checkpoint_at has gone stale is a job whose process died, and
     `reap_stale()` marks it so on the next startup instead of leaving a
     progress bar frozen at 61% forever.
  3. **Non-fatal per item.** One corrupt JPEG out of 50,000 records a
     `job_items` row and is skipped. It does not raise, and it does not
     stop the other 49,999. The dead-letter list is a first-class output
     of the run, visible on the run screen.

Cancellation is cooperative: `POST /api/jobs/{id}/cancel` sets the state,
the stage loop checks `should_stop()` between batches, stops at a clean
batch boundary, and leaves a cursor that resume can use. There is no
thread-kill anywhere in here — killing a thread mid-write is how you get
the half-moved-quarantine problem this module exists to prevent.

No SQL lives here beyond the jobs tables themselves; everything else goes
through edge/db/repo.py (Rule 1). The jobs tables are the one exception
and they are declared in edge/db/migrations/0005_jobs_species_auth.sql —
a job's own bookkeeping is infrastructure, not domain data, and threading
it through repo.py would put the worker's locking behaviour behind an
abstraction that hides the one thing you need to see when it deadlocks.
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from edge import config
from edge.db import repo

# A job that has not checkpointed in this long is assumed dead. Generous on
# purpose: Stage B on a slow laptop can legitimately spend 90s on one batch
# of large frames, and declaring a working job dead is worse than noticing
# a dead one late.
STALE_AFTER_S = 600

# How often a stage should call checkpoint(). Every batch is right; the
# constant is here so stages agree on batch size rather than each picking
# their own.
DEFAULT_BATCH = 200

_workers: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, threading.Event] = {}
_lock = threading.Lock()


# ── the job record ───────────────────────────────────────────────────────

@dataclass
class Job:
    job_id: str
    run_id: str | None
    reserve_id: str
    kind: str
    state: str
    total: int
    done_count: int
    failed_count: int
    cursor: str | None
    detail: dict

    @property
    def progress(self) -> float:
        return round(self.done_count / self.total, 4) if self.total else 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(kind: str, reserve_id: str, run_id: str | None = None,
           total: int = 0, actor: str = "system",
           model_versions: dict | None = None) -> str:
    """Register a job. Does not start it — `start()` does that, so a caller
    can create, inspect and then decide."""
    job_id = repo.new_id("job_")
    conn = repo.connect()
    conn.execute(
        "INSERT INTO jobs(job_id, run_id, reserve_id, kind, state, total,"
        " created_at, actor, config, model_versions)"
        " VALUES (?,?,?,?,'queued',?,?,?,?,?)",
        (job_id, run_id, reserve_id, kind, total, _now(), actor,
         config.CONFIG.to_json(),
         json.dumps(model_versions or {})))
    conn.commit()
    repo.audit("job.create", actor=actor, entity_type="job", entity_id=job_id,
               after={"kind": kind, "run_id": run_id, "total": total})
    return job_id


def get(job_id: str) -> Job | None:
    row = repo.connect().execute(
        "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["detail"] = json.loads(d.get("detail") or "{}")
    except json.JSONDecodeError:
        d["detail"] = {}
    return Job(**{k: d[k] for k in Job.__dataclass_fields__})


def status(job_id: str) -> dict | None:
    """The shape the UI polls. Includes the dead-letter tail, because a run
    that finished with 40 unreadable frames is not the same run as one that
    finished with none, and the difference has to be visible without a
    second request."""
    row = repo.connect().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("detail", "model_versions"):
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except json.JSONDecodeError:
            d[k] = {}
    d.pop("config", None)          # frozen config is large; fetch it per-run instead
    d["progress"] = round(d["done_count"] / d["total"], 4) if d["total"] else 0.0
    d["stale"] = _is_stale(d)
    d["eta_seconds"] = _eta(d)
    d["failures"] = repo._rows(repo.connect().execute(
        "SELECT item_id, error, attempts FROM job_items"
        " WHERE job_id=? ORDER BY updated_at DESC LIMIT 25", (job_id,)))
    return d


def for_run(run_id: str) -> list[dict]:
    return repo._rows(repo.connect().execute(
        "SELECT job_id, kind, state, total, done_count, failed_count,"
        " created_at, started_at, finished_at, error"
        " FROM jobs WHERE run_id=? ORDER BY created_at", (run_id,)))


def active() -> list[dict]:
    return repo._rows(repo.connect().execute(
        "SELECT job_id, run_id, kind, state, total, done_count, failed_count,"
        " checkpoint_at FROM jobs WHERE state IN ('queued','running','paused')"
        " ORDER BY created_at"))


def _is_stale(d: dict) -> bool:
    if d["state"] != "running" or not d.get("checkpoint_at"):
        return False
    last = datetime.fromisoformat(d["checkpoint_at"])
    return (datetime.now(timezone.utc) - last).total_seconds() > STALE_AFTER_S


def _eta(d: dict) -> float | None:
    """Measured, not assumed. v0.1.1's only time estimate was
    `image_count * estimated_seconds_per_image` from config — a number
    nobody had measured, shown to an officer as if it were a forecast.
    This one divides elapsed time by work actually completed, and returns
    None until there is enough of both to mean anything."""
    if d["state"] != "running" or not d.get("started_at") or d["done_count"] < 20:
        return None
    elapsed = (datetime.now(timezone.utc)
               - datetime.fromisoformat(d["started_at"])).total_seconds()
    rate = d["done_count"] / elapsed if elapsed > 0 else 0
    remaining = max(0, d["total"] - d["done_count"])
    return round(remaining / rate, 1) if rate > 0 else None


# ── progress, checkpointing, failure ─────────────────────────────────────

def checkpoint(job_id: str, *, done: int | None = None, cursor: str | None = None,
               total: int | None = None, detail: dict | None = None,
               conn=None) -> None:
    """Commit progress. Pass `conn` to write this inside the SAME
    transaction as the batch it describes — that is the whole point. A
    cursor committed in its own transaction after the batch's rows can be
    lost independently of them, which is exactly the window that produces
    "the DB says 30,000 done, the disk says 30,180"."""
    conn = conn or repo.connect()
    sets, args = ["checkpoint_at=?"], [_now()]
    if done is not None:
        sets.append("done_count=?"); args.append(done)
    if cursor is not None:
        sets.append("cursor=?"); args.append(cursor)
    if total is not None:
        sets.append("total=?"); args.append(total)
    if detail is not None:
        sets.append("detail=?"); args.append(json.dumps(detail))
    args.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", args)


def fail_item(job_id: str, item_id: str, error: str, conn=None) -> None:
    """One frame failed. Record it, count it, move on. The `attempts`
    increment means a resumed job that hits the same bad file twice is
    visible as such rather than looking like two different problems."""
    conn = conn or repo.connect()
    conn.execute(
        "INSERT INTO job_items(job_id, item_id, state, attempts, error, updated_at)"
        " VALUES (?,?,'failed',1,?,?)"
        " ON CONFLICT(job_id, item_id) DO UPDATE SET"
        " attempts = attempts + 1, error = excluded.error,"
        " updated_at = excluded.updated_at",
        (job_id, item_id, str(error)[:500], _now()))
    conn.execute("UPDATE jobs SET failed_count ="
                 " (SELECT COUNT(*) FROM job_items WHERE job_id=? AND state='failed')"
                 " WHERE job_id=?", (job_id, job_id))


def failed_items(job_id: str) -> set[str]:
    """Items already known bad. A resumed job skips these rather than
    spending its second pass re-failing on the same corrupt files."""
    return {r["item_id"] for r in repo.connect().execute(
        "SELECT item_id FROM job_items WHERE job_id=? AND state='failed'", (job_id,))}


def finish(job_id: str, state: str = "done", error: str | None = None,
           detail: dict | None = None) -> None:
    conn = repo.connect()
    sets = ["state=?", "finished_at=?", "checkpoint_at=?"]
    args: list = [state, _now(), _now()]
    if error is not None:
        sets.append("error=?"); args.append(str(error)[:2000])
    if detail is not None:
        sets.append("detail=?"); args.append(json.dumps(detail))
    args.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", args)
    conn.commit()
    repo.audit(f"job.{state}", actor="system", entity_type="job", entity_id=job_id,
               after={"error": error, **(detail or {})})


def cancel(job_id: str, actor: str = "director") -> bool:
    """Cooperative. Sets the flag and the state; the stage loop notices at
    its next batch boundary and stops with a usable cursor. Nothing is
    killed mid-write."""
    j = get(job_id)
    if not j or j.state not in ("queued", "running", "paused"):
        return False
    with _lock:
        ev = _cancel_flags.get(job_id)
    if ev:
        ev.set()
    conn = repo.connect()
    conn.execute("UPDATE jobs SET state='cancelled', finished_at=?, checkpoint_at=?"
                 " WHERE job_id=?", (_now(), _now(), job_id))
    conn.commit()
    repo.audit("job.cancel", actor=actor, entity_type="job", entity_id=job_id)
    return True


def should_stop(job_id: str) -> bool:
    with _lock:
        ev = _cancel_flags.get(job_id)
    return bool(ev and ev.is_set())


# ── running ──────────────────────────────────────────────────────────────

def start(job_id: str, fn: Callable[[str], dict]) -> None:
    """Run `fn(job_id)` on a worker thread. `fn` is expected to checkpoint
    as it goes and to return a summary dict; anything it raises is caught,
    recorded on the job, and re-raised nowhere — a background stage must not
    be able to take the server down with it."""
    job = get(job_id)
    if not job:
        raise KeyError(job_id)

    with _lock:
        if job_id in _workers and _workers[job_id].is_alive():
            return
        _cancel_flags[job_id] = threading.Event()

    conn = repo.connect()
    conn.execute("UPDATE jobs SET state='running', started_at=COALESCE(started_at,?),"
                 " checkpoint_at=?, error=NULL WHERE job_id=?", (_now(), _now(), job_id))
    conn.commit()

    def _run() -> None:
        try:
            detail = fn(job_id) or {}
            if should_stop(job_id):
                finish(job_id, "cancelled", detail=detail)
            else:
                finish(job_id, "done", detail=detail)
        except Exception as exc:                                  # noqa: BLE001
            finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}",
                   detail={"traceback": traceback.format_exc()[-2000:]})
        finally:
            # Every thread opens its own SQLite connection (repo.connect()
            # is thread-local); leaking them across a long-running server
            # is how you exhaust file handles on a laptop.
            repo.close()
            with _lock:
                _workers.pop(job_id, None)
                _cancel_flags.pop(job_id, None)

    t = threading.Thread(target=_run, name=f"pugmark-{job.kind}-{job_id[:8]}", daemon=True)
    with _lock:
        _workers[job_id] = t
    t.start()


def resume(job_id: str, fn: Callable[[str], dict]) -> None:
    """Restart a job that stopped without finishing. Same entry point as
    start(); the stage reads its own cursor and picks up from it."""
    job = get(job_id)
    if not job or job.state in ("done", "cancelled"):
        raise ValueError(f"job {job_id} is {job.state if job else 'missing'}, not resumable")
    repo.audit("job.resume", entity_type="job", entity_id=job_id,
               after={"from_cursor": job.cursor, "done": job.done_count})
    start(job_id, fn)


def reap_stale() -> int:
    """Called at startup. A job marked `running` at boot cannot be running —
    this process just started, and nothing survives it. Mark them
    interrupted so the run screen shows "stopped at 30,142 of 50,000,
    resumable" instead of a progress bar that will never move again."""
    conn = repo.connect()
    rows = repo._rows(conn.execute(
        "SELECT job_id, done_count, total FROM jobs WHERE state='running'"))
    for r in rows:
        conn.execute(
            "UPDATE jobs SET state='paused', error=? WHERE job_id=?",
            (f"interrupted at {r['done_count']}/{r['total']} — the process stopped "
             f"before this stage finished. Resumable from its last checkpoint.",
             r["job_id"]))
    conn.commit()
    if rows:
        repo.audit("job.reap_stale", after={"paused": [r["job_id"] for r in rows]})
    return len(rows)


# ── the batching helper every stage uses ─────────────────────────────────

def batched(items: list, size: int = DEFAULT_BATCH) -> Iterator[tuple[int, list]]:
    """Yields (batch_index, batch). Stages iterate this and checkpoint once
    per batch, which is what bounds crash loss to one batch."""
    for i in range(0, len(items), size):
        yield i // size, items[i:i + size]


def stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_S)).isoformat()
