"""The routes v0.1.1 was missing, and the ones it had but never reached.

Two separate problems are fixed here.

**Unreachable routes.** Cross-referencing every `@app.route` in
`edge/app.py` against every URL in `edge/ui/`, five routes were defined,
tested and never called by any screen:

    POST /api/individuals/{ind_id}/promote     no button anywhere
    POST /api/alerts/{alert_id}/acknowledge    no button anywhere
    GET  /api/stations/export.geojson          no link anywhere
    GET  /api/runs/{run_id}/preflight          never re-fetched
    GET  /api/health                           never displayed

The first two matter most, and in the same way: the app auto-enrols
provisional individuals and raises alerts, and gave the officer no way to
confirm either. Provisional tigers accumulate for ever; alerts can be read
but never marked as handled.

**Capabilities with no route at all.** `repo.entities()`,
`repo.single_flank()` and `repo.rebuild_entities()` are exposed nowhere —
even though CLAUDE.md rule 6 calls single-flank "a first-class state, not
an absence". `occupancy.py` and `alerts.py` had no caller. There was no job
status, no pagination, no readiness check, no backup, and no way to say
"just run the whole thing".

Everything here is additive. `edge/app.py` needs one line:

    from edge.routes_scale import register as _register_scale_routes
    _register_scale_routes(app)
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from fastapi import Body, Depends, HTTPException

from edge import config, jobs
from edge.db import repo
from edge.db import repo_ext


def register(app) -> None:                                        # noqa: C901
    from edge.app import current_user, require_role

    # ── the whole pipeline, as one action ────────────────────────────────

    @app.post("/api/runs/{run_id}/pipeline")
    def start_pipeline(run_id: str, payload: dict = Body(default={}),
                       user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
        """Triage -> identify -> occupancy -> alerts, as one resumable
        background job.

        This is the route the product was missing. In v0.1.1 an officer
        pressed Scan, then Confirm, then Triage, and then had to identify
        every animal frame by hand one click at a time — and occupancy and
        alerts never ran at all, on any path, because nothing imported
        those modules. A 50,000-frame import produced an empty map and an
        empty alert list, permanently, with no error to explain it.
        """
        run = repo.run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        if run["stage"] == "preflight":
            raise HTTPException(400, "confirm the folder-to-station assignments first")

        existing = [j for j in jobs.for_run(run_id)
                    if j["kind"] == "stage3" and j["state"] in ("queued", "running")]
        if existing:
            return {"job_id": existing[0]["job_id"], "already_running": True}

        actor = user["username"]
        job_id = jobs.create("stage3", run["reserve_id"], run_id, actor=actor)
        from edge.pipeline import stage3
        jobs.start(job_id, lambda jid: stage3.run_full_pipeline(run_id, jid, actor))
        return {"job_id": job_id, "run_id": run_id,
                "note": "Running in the background. Poll /api/jobs/{job_id} for progress; "
                        "it survives a page reload and can be resumed after a restart."}

    @app.post("/api/runs/{run_id}/stage3")
    def start_stage3(run_id: str, payload: dict = Body(default={}),
                     user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
        """Bulk Stage 3 alone, for a run already triaged."""
        run = repo.run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        actor = user["username"]
        job_id = jobs.create("stage3", run["reserve_id"], run_id, actor=actor)
        from edge.pipeline import stage3
        jobs.start(job_id, lambda jid: stage3.run_stage3(run_id, jid, actor))
        return {"job_id": job_id}

    @app.post("/api/runs/{run_id}/postprocess")
    def run_postprocess(run_id: str, payload: dict = Body(default={}),
                        user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
        """Recompute occupancy and alerts for a run.

        Fast enough to run inline (well under a second on the seeded
        reserve), because both stages are arithmetic over data already on
        disk. Called automatically at the end of the pipeline and after a
        review decision; exposed separately so a correction made outside
        the review queue can also be reflected.
        """
        if not repo.run(run_id):
            raise HTTPException(404, "run not found")
        from edge.pipeline import postprocess
        try:
            return postprocess.run(run_id, actor=user["username"])
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── jobs ─────────────────────────────────────────────────────────────

    @app.get("/api/jobs")
    def list_jobs(run_id: str | None = None, user: dict = Depends(current_user)) -> dict:
        return {"active": jobs.active(),
                "for_run": jobs.for_run(run_id) if run_id else None}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, user: dict = Depends(current_user)) -> dict:
        s = jobs.status(job_id)
        if not s:
            raise HTTPException(404, "job not found")
        return s

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
        """Cooperative: the stage stops at its next batch boundary with a
        usable cursor. Nothing is killed mid-write — that is how you get
        files moved into quarantine with no manifest recording where they
        came from."""
        if not jobs.cancel(job_id, user["username"]):
            raise HTTPException(400, "job is not running")
        return {"ok": True, "note": "Stopping at the next batch boundary. Work already "
                                    "committed is kept and the job can be resumed."}

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        actor = user["username"]
        from edge.pipeline import stage3
        try:
            if job.kind == "stage3":
                jobs.resume(job_id, lambda jid: stage3.run_full_pipeline(
                    job.run_id, jid, actor))
            elif job.kind == "ingest":
                from edge.pipeline import ingest
                jobs.resume(job_id, lambda jid: ingest.resume_ingest(job.run_id, jid))
            else:
                raise HTTPException(400, f"{job.kind} jobs are not resumable")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"job_id": job_id, "resumed_from": job.cursor, "done": job.done_count}

    @app.get("/api/runs/{run_id}/dead-letters")
    def dead_letters(run_id: str, user: dict = Depends(current_user)) -> dict:
        """Frames that failed and were skipped. A 50,000-frame import with
        40 unreadable files is not the same import as one with none, and
        v0.1.1 had nowhere to record the difference — a failed frame was
        counted and forgotten."""
        return {"items": repo_ext.run_dead_letters(run_id)}

    # ── pagination ───────────────────────────────────────────────────────

    @app.get("/api/runs/{run_id}/images/page")
    def images_page(run_id: str, status: str, limit: int = 100, offset: int = 0,
                    user: dict = Depends(current_user)) -> dict:
        """`/api/runs/{id}/images` returns every matching row: 945 on the
        seeded demo, ~4,000 scaled to a 50,000-frame import, each rendered
        as a DOM row with a button. This replaces it."""
        if not repo.run(run_id):
            raise HTTPException(404, "run not found")
        return repo_ext.images_by_status_page(run_id, status, limit, offset)

    @app.get("/api/review/page")
    def review_page(limit: int = 50, offset: int = 0, user: dict = Depends(current_user)) -> dict:
        return repo_ext.review_open_page(limit, offset, actor=user["username"])

    @app.post("/api/review/{queue_id}/claim")
    def claim_review(queue_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
        """Two reviewers in two tabs saw the same top item and could both
        decide it; the second silently superseded the first with no sign a
        race had occurred."""
        if not repo_ext.claim_review_item(queue_id, user["username"]):
            raise HTTPException(409, "another reviewer is already working on this item")
        return {"ok": True}

    @app.post("/api/review/{queue_id}/release")
    def release_review(queue_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
        repo_ext.release_review_item(queue_id)
        return {"ok": True}

    # ── catalogue: the states that had no route ──────────────────────────

    @app.get("/api/catalogue/health")
    def catalogue_health(reserve_id: str, user: dict = Depends(current_user)) -> dict:
        """Per-individual catalogue completeness, including the single-flank
        state. CLAUDE.md rule 6 calls that state first-class; v0.1.1 gave it
        no route, so the UI could never ask which individuals were in it."""
        rows = repo_ext.catalogue_health(reserve_id)
        return {
            "items": rows,
            "single_flank": [r["ind_id"] for r in rows if r["sides_known"] == 1],
            "no_flank": [r["ind_id"] for r in rows if r["sides_known"] == 0],
            "both_flanks": [r["ind_id"] for r in rows if r["sides_known"] == 2],
        }

    @app.get("/api/individuals/provisional")
    def provisional(reserve_id: str, user: dict = Depends(current_user)) -> dict:
        """Auto-enrolled individuals awaiting confirmation. `promote` existed
        as a route and had no caller, so these accumulated with no way to
        clear them."""
        return {"items": repo_ext.provisional_individuals(reserve_id)}

    @app.post("/api/individuals/{ind_id}/merge")
    def merge(ind_id: str, payload: dict = Body(...),
              user: dict = Depends(require_role(*config.PERMISSIONS["individual_merge"]))) -> dict:
        """Fold one individual into another.

        The unavoidable consequence of provisional auto-enrolment: the same
        tiger gets enrolled twice under two provisional IDs — once per side,
        or once per cycle before its embedding stabilised. v0.1.1 could
        create that state and could not resolve it. Supersedes rather than
        deletes (rule 5): both halves of the correction survive.
        """
        target = payload.get("into")
        if not target:
            raise HTTPException(400, "`into` (the surviving ind_id) is required")
        try:
            return repo_ext.merge_individual(ind_id, target, user["username"])
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/individuals/rebuild-entities")
    def rebuild_entities(reserve_id: str = Body(..., embed=True),
                         user: dict = Depends(require_role(*config.PERMISSIONS["individual_merge"]))) -> dict:
        return {"entities": repo.rebuild_entities(reserve_id)}

    # ── map data ─────────────────────────────────────────────────────────

    @app.get("/api/runs/{run_id}/map")
    def map_data(run_id: str, user: dict = Depends(current_user)) -> dict:
        """Everything the map needs, in one request.

        Including the two facts the old map hardcoded as literal station IDs
        (`DEAD`/`NEW` sets in app.js) and the previous cycle's centroids,
        which the centroid_shift alert is about and which nothing ever drew.
        """
        run = repo.run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        role = user["role"]
        actor = user["username"]
        period = repo_ext.run_period(run_id)
        generalised = role in config.CONFIG.privacy.generalise_coords_for_roles

        stations = repo_ext.stations_with_state(run["reserve_id"], *(period or (None, None)))
        occ = repo.occupancy(run_id)
        prior = repo_ext.prior_centroids(run_id)

        if generalised:
            step = config.CONFIG.privacy.grid_cell_km / 111.0
            rnd = lambda v: round(v / step) * step if v is not None else None  # noqa: E731
            for s in stations:
                s["lat"], s["lon"] = rnd(s["lat"]), rnd(s["lon"])
            for o in occ:
                o["hull_wkt"] = None
                o["centroid_lat"], o["centroid_lon"] = rnd(o["centroid_lat"]), rnd(o["centroid_lon"])
            for p in prior:
                p["centroid_lat"], p["centroid_lon"] = rnd(p["centroid_lat"]), rnd(p["centroid_lon"])

        repo.audit(f"location.read.{'generalised' if generalised else 'precise'}",
                   actor=actor, entity_type="run", entity_id=run_id, note="map")

        # Reserve boundaries
        res_obj = repo.reserve(run["reserve_id"])
        boundaries = {}
        if res_obj and res_obj.get("boundary_geojson"):
            try:
                bgj = json.loads(res_obj["boundary_geojson"]) if isinstance(res_obj["boundary_geojson"], str) else res_obj["boundary_geojson"]
                boundaries["core_geojson"] = bgj.get("core_geojson") or bgj
                boundaries["buffer_geojson"] = bgj.get("buffer_geojson")
                boundaries["corridor_geojson"] = bgj.get("corridor_geojson")
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                boundaries = {"error": f"boundary_geojson unparseable: {exc}"}

        # Query chronological event sightings for movement player
        events_sql = """
            SELECT e.event_id, e.station_id, e.started_at, a.ind_id
            FROM events e
            JOIN image_event ie ON e.event_id = ie.event_id
            JOIN detections d ON ie.image_id = d.image_id
            JOIN flank_crops c ON d.det_id = c.det_id
            JOIN assignments a ON c.crop_id = a.crop_id
            JOIN images i ON ie.image_id = i.image_id
            WHERE i.run_id=? AND a.decision != 'rejected'
            GROUP BY e.event_id ORDER BY e.started_at
        """
        try:
            events = repo.query(events_sql, (run_id,))
        except Exception:
            events = []

        return {"stations": stations, "occupancy": occ, "prior": prior,
                "events": events,
                "boundaries": boundaries,
                "empty_reason": "occupancy has not been computed for this run" if not occ else None,
                "alerts": [{"ind_id": a["ind_id"], "type": a["type"],
                            "severity": a["severity"]} for a in repo.alerts(run_id, False)],
                "cycle": {"start": period[0], "end": period[1]} if period else None,
                "generalised": generalised}

    # ── readiness ────────────────────────────────────────────────────────

    @app.get("/api/health/ready")
    def readiness(user: dict = Depends(current_user)) -> dict:
        """What can actually run on this machine, right now.

        `/api/health` returned `{"ok": true}` unconditionally — it was true
        while the detector weights were absent, while the disk was full, and
        while the database was corrupt. The UI never called it anyway.

        Every check here is a real probe, and each one carries the command
        that fixes it, because "not ready" without a remedy is just a
        different way of being unhelpful.
        """
        checks = []

        def add(name, ok, detail, fix=None, blocking=True):
            checks.append({"check": name, "ok": bool(ok), "detail": detail,
                           "fix": fix, "blocking": blocking})

        try:
            integ = repo_ext.integrity_check()
            add("database", integ["ok"],
                f"schema v{repo.schema_version()}, "
                f"{repo_ext.database_size_bytes() / 1e6:.1f} MB",
                None if integ["ok"] else "python -m tools.repair_db")
        except Exception as exc:                                   # noqa: BLE001
            add("database", False, f"{type(exc).__name__}: {exc}", "check data/pugmark.db")

        # Probed by PATH, not by import. Importing edge.pipeline.detector
        # pulls in torch, and this endpoint has to answer on a machine where
        # torch is exactly what is broken -- that is when a readiness check
        # earns its keep.
        torch_ok = importlib.util.find_spec("torch") is not None
        add("pytorch runtime", torch_ok,
            "installed" if torch_ok else
            "NOT INSTALLED -- every model stage is unavailable, and in v0.1.1 this "
            "also stopped the whole server from starting, because edge/app.py imported "
            "the detector at module scope",
            "pip install -r requirements.txt")

        det_ok = torch_ok and config.DETECTOR_CHECKPOINT_PATH.exists() \
            and config.DETECTOR_CONFIG_PATH.exists()
        add("detector (Stage B)", det_ok,
            "MDV6-mit-yolov9-c from edge/models" if det_ok else
            "missing from the offline model bundle",
            "install the signed offline model bundle; then run python -m tools.verify_offline_release")

        emb_ok = torch_ok and config.EMBEDDER_MODEL_PATH.exists()
        add("embedder (Stage 3)", emb_ok,
            "trihard-resnet50 from edge/models" if emb_ok else
            "missing from the offline model bundle",
            "install the signed offline model bundle; then run python -m tools.verify_offline_release")

        kp_ok = config.KEYPOINTS_MODEL_PATH.exists()
        add("keypoints", kp_ok,
            "trained 2-point regressor" if kp_ok else
            "NOT PRESENT — falling back to a fixed geometric guess that reports full "
            "confidence, so the quality gate cannot reject it",
            "install the signed offline model bundle", blocking=False)

        side_ok = config.SIDE_MODEL_PATH.exists()
        add("side classifier", side_ok,
            "L/R/UNKNOWN classifier from edge/models/side" if side_ok else
            "model absent — automatic side assignment disabled, frames route to review",
            "install the signed offline model bundle", blocking=False)

        sp_ok = config.SPECIES_MODEL_PATH.exists()
        add("species classifier", sp_ok,
            "tiger/non-tiger gate from edge/models/species" if sp_ok else
            "model absent — every 'animal' detection would reach the identity pipeline",
            "install the signed offline model bundle")

        try:
            usage = shutil.disk_usage(config.DATA_DIR)
            free_gb = usage.free / 1e9
            # 50,000 frames of quarantine at ~3 MB each is ~150 GB if every
            # frame is blank; the realistic worst case for one import is the
            # size of the card being imported.
            add("disk", free_gb > 5, f"{free_gb:.1f} GB free at {config.DATA_DIR}",
                "free space before importing; quarantine moves files, it does not delete "
                "them")
        except OSError as exc:
            add("disk", False, str(exc))

        stale = [j for j in jobs.active() if jobs.status(j["job_id"])["stale"]]
        add("jobs", not stale,
            f"{len(jobs.active())} active" + (f", {len(stale)} stale" if stale else ""),
            "POST /api/jobs/{id}/resume" if stale else None, blocking=False)

        blocking_failures = [c for c in checks if c["blocking"] and not c["ok"]]
        return {
            "ready": not blocking_failures,
            "can_ingest": True,
            "can_triage": det_ok,
            "can_identify": emb_ok and det_ok,
            "auto_assign_enabled": bool(side_ok and getattr(
                config.CONFIG.identify, "enforce_side_separation", True)),
            "checks": checks,
        }

    # ── ops ──────────────────────────────────────────────────────────────

    @app.post("/api/ops/backup")
    def backup(payload: dict = Body(default={}),
               user: dict = Depends(require_role(*config.PERMISSIONS["ops_manage"]))) -> dict:
        """A consistent copy, taken through SQLite's own backup API rather
        than a file copy (which is not safe against a live WAL).

        v0.1.1 had no backup path at all. One laptop, one file, one season of
        identifications, no second copy — and quarantine physically moves
        original frames, so losing the database loses the mapping back to
        where those files came from.
        """
        backup_root = (config.DATA_DIR / "backups").resolve()
        backup_root.mkdir(parents=True, exist_ok=True)
        requested_path = payload.get("path")
        if requested_path:
            dest = Path(requested_path).resolve()
            if not str(dest).startswith(str(backup_root)):
                dest = backup_root / dest.name
        else:
            dest = backup_root / f"pugmark-{repo.now().replace(':', '')}.db"
        try:
            return repo_ext.backup(dest)
        except Exception as exc:                                   # noqa: BLE001
            raise HTTPException(500, f"backup failed: {exc}")

    @app.get("/api/ops/integrity")
    def integrity(user: dict = Depends(current_user)) -> dict:
        return repo_ext.integrity_check()

    @app.post("/api/ops/checkpoint")
    def wal_checkpoint(user: dict = Depends(require_role(*config.PERMISSIONS["ops_manage"]))) -> dict:
        """Truncate the write-ahead log. After a large import the -wal file
        can exceed the database itself; nothing in v0.1.1 ever ran this."""
        return repo_ext.checkpoint_wal()

    @app.get("/api/ops/jobs")
    def ops_jobs(user: dict = Depends(current_user)) -> dict:
        return {"active": jobs.active(),
                "recent": repo._rows(repo.connect().execute(
                    "SELECT job_id, run_id, kind, state, total, done_count, failed_count,"
                    " created_at, finished_at, error FROM jobs"
                    " ORDER BY created_at DESC LIMIT 20"))}

    # ── Station Management & Import/Export ───────────────────────────────

    @app.get("/api/reserves/{reserve_id}/stations")
    def list_stations(reserve_id: str, user: dict = Depends(current_user)) -> list[dict]:
        return repo_ext.stations_with_state(reserve_id)

    @app.post("/api/reserves/{reserve_id}/stations")
    def create_station_route(reserve_id: str, payload: dict = Body(...),
                             user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
        sid = repo_ext.create_station(reserve_id, payload, actor=user["username"])
        return {"station_id": sid, "status": "created"}

    @app.put("/api/reserves/{reserve_id}/stations/{station_id}")
    def update_station_route(reserve_id: str, station_id: str, payload: dict = Body(...),
                             user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
        return repo_ext.update_station(station_id, payload, actor=user["username"])

    @app.delete("/api/reserves/{reserve_id}/stations/{station_id}")
    def delete_station_route(reserve_id: str, station_id: str,
                             user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
        try:
            repo_ext.delete_station(station_id, actor=user["username"])
            return {"station_id": station_id, "status": "deleted"}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/reserves/{reserve_id}/stations/import/csv")
    def import_csv_route(reserve_id: str, payload: dict = Body(...),
                         user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
        csv_text = payload.get("csv") or ""
        if not csv_text:
            raise HTTPException(400, "missing 'csv' text payload")
        return repo_ext.import_stations_csv(reserve_id, csv_text, actor=user["username"])

    @app.post("/api/reserves/{reserve_id}/stations/import/geojson")
    def import_geojson_route(reserve_id: str, payload: dict = Body(...),
                            user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
        gj_text = payload.get("geojson") or ""
        if not gj_text:
            raise HTTPException(400, "missing 'geojson' payload")
        return repo_ext.import_stations_geojson(reserve_id, gj_text, actor=user["username"])

    @app.get("/api/reserves/{reserve_id}/stations/export/geojson")
    def export_geojson_route(reserve_id: str, user: dict = Depends(current_user)) -> dict:
        return repo_ext.export_stations_geojson(reserve_id)

    # ── Reserve Boundaries ───────────────────────────────────────────────

    @app.get("/api/reserves/{reserve_id}/boundaries")
    def get_boundaries_route(reserve_id: str, user: dict = Depends(current_user)) -> dict:
        return repo_ext.get_reserve_boundaries(reserve_id)

    @app.put("/api/reserves/{reserve_id}/boundaries")
    def set_boundaries_route(reserve_id: str, payload: dict = Body(...),
                             user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
        return repo_ext.set_reserve_boundaries(reserve_id, payload, actor=user["username"])

    # ── Cross-Flank Review & Confirmation ────────────────────────────────

    @app.get("/api/reserves/{reserve_id}/cross-flank")
    def list_cross_flank(reserve_id: str, status: str | None = None,
                         user: dict = Depends(current_user)) -> list[dict]:
        return repo_ext.cross_flank_candidates(reserve_id, status=status)

    @app.post("/api/cross-flank/{assoc_id}/confirm")
    def confirm_cross_flank_route(assoc_id: str, payload: dict = Body(...),
                                  user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
        primary_ind = payload.get("primary_ind_id")
        if not primary_ind:
            raise HTTPException(400, "missing 'primary_ind_id'")
        return repo_ext.confirm_cross_flank(assoc_id, primary_ind, actor=user["username"])

    @app.post("/api/cross-flank/{assoc_id}/reject")
    def reject_cross_flank_route(assoc_id: str, payload: dict = Body(default={}),
                                 user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
        return repo_ext.reject_cross_flank(assoc_id, actor=user["username"])

    # ── Telemetry & Resource Preflight ───────────────────────────────────

    @app.get("/api/runs/{run_id}/telemetry")
    def get_run_telemetry_route(run_id: str, user: dict = Depends(current_user)) -> list[dict]:
        return repo_ext.run_telemetry(run_id)

    @app.get("/api/runs/{run_id}/status-counts")
    def get_run_status_counts(run_id: str, user: dict = Depends(current_user)) -> dict:
        return repo_ext.run_status_counts(run_id)

    @app.post("/api/runs/preflight-resources")
    def preflight_resources_route(payload: dict = Body(...),
                                  user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
        reserve_id = payload.get("reserve_id")
        root_path = payload.get("root_path")
        if not reserve_id or not root_path:
            raise HTTPException(400, "missing reserve_id or root_path")
        from edge.pipeline.ingest import resource_preflight
        return resource_preflight(reserve_id, root_path)
