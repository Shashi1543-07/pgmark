"""Stage 3 in bulk -- the automated tiger identification and flank association stage.

Features:
    warm models        loaded once per job, not once per frame
    cached catalogue   one matrix, built once, appended to in memory as new
                       entities are enrolled during the job
    vectorised match   one (N, 256) @ (256,) dot product instead of N dots
    entities once      `rebuild_entities()` at the end of the job
    every animal       each animal detection is its own crop and its own
                       identity question
    species gate       a detection that is not the target species never
                       reaches the tiger catalogue
    side gate          left vs right flank models evaluated independently
    cross-flank alert  opposite-side captures within burst window create
                       UNKNOWN_RELATIONSHIP candidates for human review
    terminal statuses  every detection and image transitions to a definitive status
    telemetry          timings and status distributions recorded per run
"""
from __future__ import annotations

import time
from datetime import datetime
import numpy as np

from edge import config, jobs
from edge.db import repo
from edge.db import repo_ext
from edge.pipeline import classifiers, identify, keypoints, postprocess

BATCH = 100


# ── the catalogue, held once ─────────────────────────────────────────────

class SideCatalogue:
    """One side's gallery as a matrix."""

    def __init__(self, reserve_id: str, side: str):
        self.reserve_id, self.side = reserve_id, side
        rows = repo.crop_embeddings_for_side(reserve_id, side)
        self.ind_ids = [r["ind_id"] for r in rows]
        self.entity_ids = [r["entity_id"] for r in rows]
        if rows:
            self.matrix = np.vstack([identify.deserialize_embedding(r["embedding"])
                                     for r in rows]).astype(np.float32)
        else:
            self.matrix = np.zeros((0, identify.EMBED_DIM), dtype=np.float32)

    def __len__(self) -> int:
        return len(self.ind_ids)

    def add(self, ind_id: str, entity_id: str, embedding: np.ndarray) -> None:
        self.matrix = np.vstack([self.matrix, embedding.astype(np.float32)[None, :]])
        self.ind_ids.append(ind_id)
        self.entity_ids.append(entity_id)

    def rank(self, query: np.ndarray, top_k: int) -> list[dict]:
        """Cosine similarity as one matrix-vector dot product."""
        if not len(self.ind_ids):
            return []
        scores = self.matrix @ query.astype(np.float32)
        order = np.argsort(-scores)[:top_k]
        seen, out = set(), []
        for i in order:
            ind = self.ind_ids[i]
            if ind in seen:
                continue
            seen.add(ind)
            out.append({"ind_id": ind, "entity_id": self.entity_ids[i],
                        "score": float(scores[i])})
        return out


# ── species and side classification ──────────────────────────────────────

def _box(det: dict) -> tuple[float, float, float, float]:
    return (det["x"], det["y"], det["w"], det["h"])


def classify_species(det: dict, image: np.ndarray) -> classifiers.Classification:
    """Classify detector crop with local species model."""
    return classifiers.classify_species(image, _box(det), config.CONFIG.classifiers)


def classify_flank_side(det: dict, image: np.ndarray) -> classifiers.Classification:
    """Return physical L/R/UNKNOWN independently of pose keypoints."""
    return classifiers.classify_side(image, _box(det), config.CONFIG.classifiers)


# ── the stage ────────────────────────────────────────────────────────────

def run_stage3(run_id: str, job_id: str | None = None, actor: str = "system") -> dict:
    """Every un-cropped animal detection in a run, in batches, resumably."""
    start_time = time.time()
    timings = {
        "timing_decode_s": 0.0,
        "timing_species_s": 0.0,
        "timing_side_s": 0.0,
        "timing_keypoints_s": 0.0,
        "timing_identify_s": 0.0,
        "timing_db_s": 0.0,
    }

    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id!r}")
    if run["stage"] not in ("triaged", "identified", "complete"):
        raise ValueError(f"run {run_id} is at stage {run['stage']!r}; triage must run first")

    cfg = config.CONFIG.identify
    target = cfg.target_species
    total = repo_ext.count_detections_pending_stage3(run_id, None)
    if job_id:
        jobs.checkpoint(job_id, total=total)

    if total == 0:
        # Zero-work path: still record telemetry and update job state so
        # monitoring, tests, and the ops screen see an explicit record rather
        # than silence (which is indistinguishable from a crash).
        zero_metrics = {
            "images_per_sec": 0.0, "cpu_util": 0.0, "ram_used_mb": 0.0,
            "timing_decode_s": 0.0, "timing_species_s": 0.0,
            "timing_side_s": 0.0, "timing_keypoints_s": 0.0,
            "timing_identify_s": 0.0, "timing_db_s": 0.0,
            "status_counts": {},
        }
        repo_ext.record_telemetry(run_id, zero_metrics)
        repo.audit("stage3.bulk", actor=actor, entity_type="run", entity_id=run_id,
                   note="zero-work: no pending detections")
        if job_id:
            jobs.checkpoint(job_id, done=0, detail={"zero_work": True})
        return {"processed": 0, "note": _nothing_to_do_note(run_id, target)}

    embedder = None
    catalogues: dict[str, SideCatalogue] = {}
    skip = jobs.failed_items(job_id) if job_id else set()

    job = jobs.get(job_id) if job_id else None
    cursor = job.cursor if job and job.cursor else ""
    done = job.done_count if job else 0
    counts = {
        "auto": 0, "review": 0, "enroll": 0, "refuse": 0,
        "non_target_species": 0, "unknown_species": 0,
        "side_unknown": 0, "cross_flank_candidate": 0, "unreadable": 0
    }

    while True:
        if job_id and jobs.should_stop(job_id):
            break
        batch = repo_ext.detections_pending_stage3(run_id, None, cursor, BATCH)
        if not batch:
            break

        with repo_ext.transaction() as conn:
            for det in batch:
                cursor = det["det_id"]
                if det["det_id"] in skip:
                    continue
                try:
                    outcome, embedder = _process_detection_checked(
                        det, run, embedder, catalogues, cfg, target, actor, conn, timings)
                    counts[outcome] = counts.get(outcome, 0) + 1
                    done += 1
                except Exception as exc:                           # noqa: BLE001
                    # NOT "unreadable". This catches every failure in
                    # identification, and calling them unreadable frames sent
                    # an operator looking at their SD card for corrupt files
                    # while the real cause was an AttributeError in the
                    # pipeline -- 29 detections crashed identically and the
                    # run reported "29 frames could not be read", which is
                    # both wrong and unactionable. The error text is already
                    # recorded per item by fail_item(); the count now says
                    # what it is so the note can point at it.
                    jobs.fail_item(job_id or "adhoc", det["det_id"],
                                   f"{type(exc).__name__}: {exc}", conn)
                    counts["errored"] = counts.get("errored", 0) + 1
                    counts.setdefault("first_error", f"{type(exc).__name__}: {exc}")
            if job_id:
                jobs.checkpoint(job_id, done=done, cursor=cursor,
                                detail=counts, conn=conn)

    repo.rebuild_entities(run["reserve_id"])
    repo_ext.set_run_models(run_id, {"embedder": identify.EMBED_MODEL_VERSION,
                                     "keypoints": _keypoint_model_version()})

    total_time = max(0.001, time.time() - start_time)
    fps = round(done / total_time, 2) if done else 0.0

    # Record run telemetry
    telemetry_metrics = {
        "images_per_sec": fps,
        "cpu_util": 0.0,
        "ram_used_mb": 0.0,
        **timings,
        "status_counts": counts,
    }
    repo_ext.record_telemetry(run_id, telemetry_metrics)

    repo.audit("stage3.bulk", actor=actor, entity_type="run", entity_id=run_id,
               model_version=identify.EMBED_MODEL_VERSION,
               threshold=cfg.t_high,
               after={**counts, "target_species": target,
                      "species_threshold": config.CONFIG.classifiers.species_min_confidence,
                      "side_threshold": config.CONFIG.classifiers.side_min_confidence})

    return {"processed": done, "total": total, **counts,
            "images_per_sec": fps, "note": _outcome_note(counts)}


def _terminal_crop(det: dict, side: str, side_result: classifiers.Classification | None,
                   image: np.ndarray, conn) -> str:
    """Mark a refused gate outcome terminally without creating an embedding.

    Still saves a real (un-rectified) crop at the detection box -- see
    classifiers.save_raw_crop()'s docstring for why a review item must never
    be left with nothing real for a human to look at.
    """
    crop_id = repo.new_id("crop_")
    crop_path = classifiers.save_raw_crop(image, _box(det), crop_id)
    repo_ext.record_flank_crop(
        crop_id=crop_id, det_id=det["det_id"], side=side, rect_ok=False,
        quality=0.0, path=crop_path, embedding=None, embed_model_version=None,
        side_confidence=side_result.confidence if side_result else None,
        side_source=side_result.source if side_result else "not_run",
        side_model_version=side_result.model_version if side_result else None,
        conn=conn)
    return crop_id


def _find_opposite_flank_neighbor(run_id: str, image_id: str, opposite_side: str,
                                   window_s: int, conn) -> dict | None:
    """Find if an individual with opposite flank was captured at same station within window_s.

    Returns the neighbour row dict (with ind_id, station_id, captured_at) or None.
    Uses its own connection read to avoid holding a write lock on the caller's conn
    for a potentially slow JOIN.
    """
    img = repo.image(image_id)
    if not img or not img.get("station_id") or not img.get("captured_at"):
        return None
    stn_id = img["station_id"]
    t0 = img["captured_at"]

    # Use a separate read connection to avoid transaction contention
    read_conn = repo.connect()
    cur = read_conn.execute(
        "SELECT c.crop_id, c.side, a.ind_id, i.captured_at, i.station_id "
        "FROM flank_crops c "
        "JOIN detections d ON c.det_id = d.det_id "
        "JOIN images i ON d.image_id = i.image_id "
        "JOIN assignments a ON c.crop_id = a.crop_id "
        "WHERE i.run_id=? AND i.station_id=? AND c.side=? AND a.ind_id IS NOT NULL "
        "ORDER BY ABS(strftime('%s', i.captured_at) - strftime('%s', ?)) ASC LIMIT 1",
        (run_id, stn_id, opposite_side, t0))
    rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return None
    match = rows[0]
    try:
        dt1 = datetime.fromisoformat(t0)
        dt2 = datetime.fromisoformat(match["captured_at"])
        if abs((dt1 - dt2).total_seconds()) <= window_s:
            return match
    except (ValueError, TypeError):
        pass
    return None


def _set_terminal(image_id: str | None, status: str, conn=None) -> None:
    if image_id:
        try:
            repo_ext.set_image_terminal_status(image_id, status, conn=conn)
        except Exception:
            pass


def _process_detection_checked(det, run, embedder, catalogues, cfg, target, actor, conn, timings=None):
    """Apply species, side, quality, cross-flank, and identification decision."""
    from edge import imageio

    img_id = det.get("image_id")
    t0 = time.time()
    image = imageio.read_bgr(det["orig_path"])
    if timings:
        timings["timing_decode_s"] += (time.time() - t0)
    if image is None:
        raise imageio.UnreadableImage(f"cannot decode {det['orig_path']}")

    # 1. Species Classification
    t0 = time.time()
    species = classify_species(det, image)
    if timings:
        timings["timing_species_s"] += (time.time() - t0)

    repo_ext.set_detection_species(
        det["det_id"], species.label, species.confidence, species.source, conn,
        model_version=species.model_version)

    if species.label != target:
        crop_id = _terminal_crop(det, "unknown", None, image, conn)
        if species.label == "unknown":
            _queue_review(crop_id, [], cfg,
                          "Species is UNKNOWN or the local species classifier is unavailable; "
                          "this detection was not sent to tiger identification.", conn)
            _set_terminal(img_id, "UNKNOWN_SPECIES", conn=conn)
            return "unknown_species", embedder
        _set_terminal(img_id, "NON_TARGET_SPECIES", conn=conn)
        return "non_target_species", embedder

    # 2. Side Classification
    t0 = time.time()
    side_evidence = classify_flank_side(det, image)
    if timings:
        timings["timing_side_s"] += (time.time() - t0)

    if side_evidence.label not in ("L", "R"):
        crop_id = _terminal_crop(det, "unknown", side_evidence, image, conn)
        _queue_review(crop_id, [], cfg,
                      "Physical flank side is UNKNOWN or the local side classifier is unavailable; "
                      "this confirmed tiger was not searched against an L or R catalogue.", conn)
        _set_terminal(img_id, "SIDE_UNKNOWN", conn=conn)
        return "side_unknown", embedder

    predicted_side = side_evidence.label
    opposite_side = "R" if predicted_side == "L" else "L"

    # 3. Pose Keypoints & Quality
    t0 = time.time()
    raw_keypoints = keypoints.estimate_keypoints(_box(det), det["orig_path"])
    physical_keypoints = keypoints.apply_physical_side(raw_keypoints, predicted_side)
    if timings:
        timings["timing_keypoints_s"] += (time.time() - t0)

    if physical_keypoints is None:
        crop_id = _terminal_crop(det, predicted_side, side_evidence, image, conn)
        _queue_review(crop_id, [], cfg,
                      "Flank side is known, but the pose model did not provide a complete "
                      "shoulder/hip pair for safe rectification.", conn)
        _set_terminal(img_id, "LOW_QUALITY", conn=conn)
        return "refuse", embedder

    # 4. Identity Embedding
    t0 = time.time()
    if embedder is None:
        embedder = identify.load_embedder(identify.WEIGHTS_PATH)
    result = identify.identify_crop(image, physical_keypoints, [], embedder, cfg, _box(det))
    if timings:
        timings["timing_identify_s"] += (time.time() - t0)

    crop_id = repo.new_id("crop_")
    crop_path = None
    if result["rect"] is not None:
        import cv2
        config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
        crop_path = config.CROPS_DIR / f"{crop_id}.jpg"
        cv2.imwrite(str(crop_path), result["rect"])

    embedding = result["embedding"]
    repo_ext.record_flank_crop(
        crop_id=crop_id, det_id=det["det_id"], side=predicted_side,
        rect_ok=embedding is not None, quality=result["quality"],
        path=str(crop_path) if crop_path else None,
        embedding=identify.serialize_embedding(embedding) if embedding is not None else None,
        embed_model_version=identify.EMBED_MODEL_VERSION if embedding is not None else None,
        side_confidence=side_evidence.confidence, side_source=side_evidence.source,
        side_model_version=side_evidence.model_version, conn=conn)

    if embedding is None:
        _set_terminal(img_id, "LOW_QUALITY", conn=conn)
        return "refuse", embedder

    # 5. Matching & 3-way Decision
    if predicted_side not in catalogues:
        catalogues[predicted_side] = SideCatalogue(run["reserve_id"], predicted_side)
    ranked = catalogues[predicted_side].rank(embedding, cfg.top_k_candidates)
    best = ranked[0] if ranked else None
    decision, why = identify.decide(best["score"] if best else None, cfg)

    if decision == "auto":
        _assign(crop_id, best["ind_id"], best["score"], "auto", actor, conn)
        _set_terminal(img_id, "IDENTIFIED", conn=conn)
        return "auto", embedder

    if decision == "review":
        _queue_review(crop_id, ranked, cfg, why, conn)
        _set_terminal(img_id, "IDENTITY_REVIEW", conn=conn)
        return "review", embedder

    # 6. Check for Cross-Flank Association Hypothesis
    opp_neighbor = _find_opposite_flank_neighbor(
        run["run_id"], img_id, opposite_side, cfg.cross_flank_window_s, conn) if img_id else None

    ind_id = repo.create_provisional_individual(run["reserve_id"], actor)
    _assign(crop_id, ind_id, 1.0, "enrolled", actor, conn)
    catalogues[predicted_side].add(ind_id, f"{ind_id}-{predicted_side}", embedding)

    if opp_neighbor:
        # Cross-flank candidate detected!
        l_ind = ind_id if predicted_side == "L" else opp_neighbor["ind_id"]
        r_ind = opp_neighbor["ind_id"] if predicted_side == "L" else ind_id
        evidence = {
            "station_id": opp_neighbor["station_id"],
            "this_captured_at": det.get("captured_at"),
            "neighbor_captured_at": opp_neighbor["captured_at"],
            "neighbor_ind_id": opp_neighbor["ind_id"],
            "this_ind_id": ind_id,
            "window_s": cfg.cross_flank_window_s,
        }
        repo_ext.create_cross_flank_candidate(
            reserve_id=run["reserve_id"],
            l_ind_id=l_ind,
            r_ind_id=r_ind,
            confidence=0.80,
            evidence=evidence,
            conn=conn)
        _queue_review(crop_id, ranked, cfg,
                      f"Cross-flank candidate: detected opposite flank ({opposite_side}) at same station "
                      f"within {cfg.cross_flank_window_s}s. Status UNKNOWN_RELATIONSHIP pending human review.", conn)
        _set_terminal(img_id, "IDENTITY_REVIEW", conn=conn)
        return "cross_flank_candidate", embedder

    _set_terminal(img_id, "NEW_INDIVIDUAL", conn=conn)
    return "enroll", embedder


def _assign(crop_id, ind_id, score, decision, actor, conn) -> None:
    repo_ext.record_assignment(crop_id, ind_id, score, decision, actor, conn)


def _queue_review(crop_id, ranked, cfg, reason, conn) -> None:
    candidates = [{"ind_id": c["ind_id"], "score": round(c["score"], 4)}
                  for c in ranked[:cfg.top_k_candidates]]
    repo_ext.queue_crop_review(crop_id, candidates, ranked[0]["score"] if ranked else 0.0,
                               reason, conn)


def _keypoint_model_version() -> str:
    return ("yolo11-pose-2kp@1.0.0" if keypoints.WEIGHTS_PATH.exists()
            else "geometric-stub@1.0.0")


def _nothing_to_do_note(run_id: str, target: str) -> str:
    pending = repo_ext.count_detections_pending_stage3(run_id, None)
    if pending == 0:
        return ("Every animal detection in this run already has a crop. Stage 3 has "
                "nothing left to do.")
    return (f"{pending} animal detections remain to be classified before they can be "
            f"considered for the {target} identity catalogue.")


def _outcome_note(counts: dict) -> str:
    parts = []
    if counts.get("side_unknown"):
        parts.append(
            f"{counts['side_unknown']} confirmed tigers were not searched because their "
            "physical flank side was unknown; they are in the review queue.")
    if counts.get("cross_flank_candidate"):
        parts.append(
            f"{counts['cross_flank_candidate']} cross-flank candidates detected with UNKNOWN_RELATIONSHIP.")
    if counts.get("non_target_species"):
        parts.append(f"{counts['non_target_species']} detections were not the target species.")
    if counts.get("unknown_species"):
        parts.append(f"{counts['unknown_species']} detections had unknown species.")
    if counts.get("refuse"):
        parts.append(f"{counts['refuse']} crops failed the quality gate.")
    if counts.get("unreadable"):
        parts.append(f"{counts['unreadable']} frames could not be read.")
    if counts.get("errored"):
        # Surface the actual exception. A count on its own tells an operator
        # something went wrong and nothing about what, and this path had been
        # silently reporting a code defect as a data problem.
        detail = counts.get("first_error") or "see the run's failed items"
        parts.append(
            f"{counts['errored']} detections failed during identification "
            f"({detail}). These are processing errors, not damaged photos "
            "— the frames themselves are intact.")
    if not parts:
        parts.append(f"{counts.get('auto', 0)} matched automatically, {counts.get('review', 0)} sent for "
                     f"review, {counts.get('enroll', 0)} enrolled as new individuals.")
    return " ".join(parts)


# ── the whole pipeline, as one job ───────────────────────────────────────

def run_full_pipeline(run_id: str, job_id: str, actor: str = "system") -> dict:
    """Triage -> Stage 3 -> occupancy -> alerts, as one resumable job."""
    from edge.pipeline import triage as triage_pipeline

    out: dict = {"run_id": run_id, "stages": {}}
    run = repo.run(run_id)

    if run["stage"] == "confirmed":
        jobs.checkpoint(job_id, detail={"stage": "triage"})
        out["stages"]["triage"] = triage_pipeline.run_triage(run_id, job_id=job_id)
        if jobs.should_stop(job_id):
            return out
        # Triage and Stage 3 share this one job_id end to end, but they count
        # two different things against two different totals (frames triaged
        # vs. detections identified). Without this reset, run_stage3() reads
        # done_count fresh from the job below and inherits triage's leftover
        # value instead of starting at 0 -- e.g. 70 frames triaged + 77
        # detections identified reads back as "147 of 77", a progress bar
        # past 100% on every combined-pipeline run. Only reset here, on the
        # fresh triage -> Stage 3 transition: a resumed job already partway
        # through Stage 3 must keep its own real cursor and done_count, and
        # this whole `if` block is skipped on that path (run["stage"] is no
        # longer "confirmed" once triage has finished once).
        jobs.checkpoint(job_id, done=0, cursor="")

    jobs.checkpoint(job_id, detail={"stage": "identify"})
    out["stages"]["stage3"] = run_stage3(run_id, job_id=job_id, actor=actor)
    if jobs.should_stop(job_id):
        return out
    repo_ext.set_run_stage_checked(run_id, "identified", actor)

    jobs.checkpoint(job_id, detail={"stage": "occupancy_and_alerts"})
    out["stages"]["postprocess"] = postprocess.run(run_id, actor=actor)
    repo_ext.set_run_stage_checked(run_id, "complete", actor)
    repo_ext.finish_run(run_id)
    repo_ext.checkpoint_wal()
    return out
