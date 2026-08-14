"""Stage 3 in bulk — the pipeline v0.1.1 asked a human to be.

In v0.1.1 there is no bulk Stage 3. `identify_upload.process_upload()`
handles exactly one photograph, and the only way to run it over a run's
frames is `POST /api/runs/{run_id}/images/{image_id}/identify`, one frame at
a time, driven by a button the UI renders next to every animal frame in an
unpaginated table. On the seeded demo that is 945 buttons. Scaled to a
50,000-frame import it is roughly 4,000 buttons, each one a round trip that:

    * loads a 100 MB ResNet-50 from disk           (`load_embedder()` per call)
    * re-reads and re-deserialises the ENTIRE side catalogue
    * calls `rebuild_entities()` — a full regroup of every assignment in the
      reserve — after every single assignment
    * processes only the single highest-confidence animal box, discarding
      every other animal in the frame

This module does the same work as a stage. The corrections are not clever;
they are the obvious ones that a per-photo entry point structurally could
not make:

    warm models        loaded once per job, not once per frame
    cached catalogue   one matrix, built once, appended to in memory as new
                       entities are enrolled during the job
    vectorised match   one (N, 256) @ (256,) dot product instead of N dots
    entities once      `rebuild_entities()` at the end of the job
    every animal       each animal detection is its own crop and its own
                       identity question — two tigers in one frame is the
                       normal case at a waterhole, not an edge case
    species gate       a detection that is not the target species never
                       reaches the tiger catalogue at all

**The side problem is not fixed here, and must not be papered over.**
`edge/pipeline/keypoints.py` labels every prediction `right_shoulder` /
`right_hip` regardless of which flank is showing, so `infer_side()` returns
'R' for every real frame — confirmed empirically in docs/RESULTS.md. On a
real deployment roughly half of captures show the left flank and would be
matched against the right-side catalogue. Running 50,000 frames through
that silently is far worse than running one, because it fills the catalogue
with confident nonsense at scale.

So this module refuses by default. `Identify.require_side_classifier`
(config) is True out of the box, and with it set, bulk Stage 3 computes
crops, quality and embeddings but writes NO assignment and NO enrolment —
every crop goes to the review queue with the reason stated. Set it to False
only once a side classifier exists, and the run's audit log will record
that you did. This is CLAUDE.md rule 8 applied to the largest gap in the
system: refusing to answer is a valid output, and the right one here.
"""
from __future__ import annotations

import json

import numpy as np

from edge import config, jobs
from edge.db import repo
from edge.pipeline import identify, keypoints, postprocess

BATCH = 100


# ── the catalogue, held once ─────────────────────────────────────────────

class SideCatalogue:
    """One side's gallery as a matrix, not a list of dicts.

    v0.1.1 called `repo.crop_embeddings_for_side()` per photo, deserialised
    every BLOB again, and scored with a Python loop of `np.dot` calls. For
    a 300-crop catalogue over 4,000 frames that is 1.2 million per-crop
    deserialisations. Here the gallery is deserialised once and matching is
    a single matrix product.

    `add()` matters as much as the caching: a tiger enrolled at frame 200 of
    a job must be matchable at frame 201. A per-photo pipeline got that for
    free by re-reading the database every time; a cached one has to maintain
    it deliberately, or the same new tiger gets enrolled once per frame it
    appears in.
    """

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
        """Cosine similarity as one dot product. Both sides are L2-normalised
        by `TripletEmbedder.forward()`, so this is exactly what
        `identify.match()` computes, at O(1) numpy calls instead of O(N)."""
        if not len(self.ind_ids):
            return []
        scores = self.matrix @ query.astype(np.float32)
        order = np.argsort(-scores)[:top_k]
        # Best score per individual, not per crop: five crops of the same
        # tiger filling all five candidate slots tells a reviewer nothing.
        seen, out = set(), []
        for i in order:
            ind = self.ind_ids[i]
            if ind in seen:
                continue
            seen.add(ind)
            out.append({"ind_id": ind, "entity_id": self.entity_ids[i],
                        "score": float(scores[i])})
        return out


# ── species ──────────────────────────────────────────────────────────────

def classify_species(det: dict, image_path: str) -> tuple[str | None, float | None, str]:
    """Which animal is this?

    MegaDetector answers `animal`, `person` or `vehicle`. It does not answer
    `tiger`. v0.1.1 treated every `animal` detection as a tiger candidate
    and sent it into the flank pipeline — so a leopard, a sambar, a wild dog
    or a langur could be embedded, scored against the tiger catalogue and,
    below `t_low`, enrolled as a brand-new tiger with a provisional ID. On a
    real reserve, where the overwhelming majority of animal detections are
    not tigers, that is not a rare failure; it is the common one.

    No species classifier ships with this build, and inventing one here
    would be exactly the "geometric guess dressed up as the real thing" the
    identify module's docstring refuses. So this returns `unknown` with
    source `none`, and the caller's behaviour under `unknown` is governed by
    `Identify.species_gate`:

        strict  — only detections with species == target_species proceed.
                  With no classifier installed that is nothing, and the run
                  says so rather than pretending.
        review  — unknown-species detections get crops and embeddings but go
                  to the review queue, where a human names the animal. This
                  is the honest default.
        off     — v0.1.1's behaviour: every animal is a tiger candidate.
                  Available, recorded in the audit log, not recommended.

    Wiring a real classifier (SpeciesNet, or a small fine-tune on the
    reserve's own labelled crops) means replacing this function's body and
    nothing else.
    """
    return None, None, "none"


# ── the stage ────────────────────────────────────────────────────────────

def run_stage3(run_id: str, job_id: str | None = None, actor: str = "system") -> dict:
    """Every un-cropped animal detection in a run, in batches, resumably.

    Checkpoints after each batch, in the same transaction as that batch's
    rows, so an interrupted job resumes from a cursor that is guaranteed
    consistent with what is actually in the database.
    """
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id!r}")
    if run["stage"] not in ("triaged", "identified", "complete"):
        raise ValueError(f"run {run_id} is at stage {run['stage']!r}; triage must run first")

    cfg = config.CONFIG.identify
    gate = getattr(cfg, "species_gate", "review")
    target = getattr(cfg, "target_species", "tiger")
    require_side = getattr(cfg, "require_side_classifier", True)

    species_filter = target if gate == "strict" else None
    total = repo.count_detections_pending_stage3(run_id, species_filter)
    if job_id:
        jobs.checkpoint(job_id, total=total)

    if total == 0:
        return {"processed": 0, "note": _nothing_to_do_note(run_id, gate, target)}

    embedder = identify.load_embedder(identify.WEIGHTS_PATH)
    catalogues: dict[str, SideCatalogue] = {}
    skip = jobs.failed_items(job_id) if job_id else set()

    job = jobs.get(job_id) if job_id else None
    cursor = job.cursor if job and job.cursor else ""
    done = job.done_count if job else 0
    counts = {"auto": 0, "review": 0, "enroll": 0, "refuse": 0,
              "species_rejected": 0, "unreadable": 0, "side_withheld": 0}

    while True:
        if job_id and jobs.should_stop(job_id):
            break
        batch = repo.detections_pending_stage3(run_id, species_filter, cursor, BATCH)
        if not batch:
            break

        with repo.transaction() as conn:
            for det in batch:
                cursor = det["det_id"]
                if det["det_id"] in skip:
                    continue
                try:
                    outcome = _process_detection(
                        det, run, embedder, catalogues, cfg, gate, target,
                        require_side, actor, conn)
                    counts[outcome] = counts.get(outcome, 0) + 1
                    done += 1
                except Exception as exc:                           # noqa: BLE001
                    jobs.fail_item(job_id or "adhoc", det["det_id"],
                                   f"{type(exc).__name__}: {exc}", conn)
                    counts["unreadable"] += 1
            if job_id:
                # Cursor and rows in ONE transaction. This is the guarantee
                # that a crash cannot leave the cursor disagreeing with the
                # data it claims to describe.
                jobs.checkpoint(job_id, done=done, cursor=cursor,
                                detail=counts, conn=conn)

    repo.rebuild_entities(run["reserve_id"])       # once per job, not per photo
    repo.set_run_models(run_id, {"embedder": identify.EMBED_MODEL_VERSION,
                                 "keypoints": _keypoint_model_version()})
    repo.audit("stage3.bulk", actor=actor, entity_type="run", entity_id=run_id,
               model_version=identify.EMBED_MODEL_VERSION,
               threshold=cfg.t_high,
               after={**counts, "species_gate": gate,
                      "side_classifier_required": require_side})

    return {"processed": done, "total": total, **counts,
            "note": _outcome_note(counts, require_side, gate)}


def _process_detection(det, run, embedder, catalogues, cfg, gate, target,
                       require_side, actor, conn) -> str:
    """One animal box: species gate, keypoints, quality, rectify, embed,
    match, decide. Returns the outcome name for the counters."""
    from edge import imageio

    # ── species ──
    species, sp_conf, sp_source = classify_species(det, det["orig_path"])
    repo.set_detection_species(det["det_id"], species, sp_conf, sp_source, conn)
    if gate == "strict" and species != target:
        return "species_rejected"

    image = imageio.read_bgr(det["orig_path"])
    if image is None:
        raise imageio.UnreadableImage(f"cannot decode {det['orig_path']}")

    kp = keypoints.estimate_keypoints(
        (det["x"], det["y"], det["w"], det["h"]), det["orig_path"])

    result = identify.identify_crop(image, kp, [], embedder, cfg)
    crop_id = repo.new_id("crop_")
    crop_path = None
    if result["rect"] is not None:
        import cv2
        config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
        crop_path = config.CROPS_DIR / f"{crop_id}.jpg"
        cv2.imwrite(str(crop_path), result["rect"])

    embedding = result["embedding"]
    conn.execute(
        "INSERT INTO flank_crops(crop_id, det_id, side, rect_ok, quality, path,"
        " embedding, embed_model_version) VALUES (?,?,?,?,?,?,?,?)",
        (crop_id, det["det_id"], result["side"] or "unknown",
         1 if embedding is not None else 0, result["quality"],
         str(crop_path) if crop_path else None,
         identify.serialize_embedding(embedding) if embedding is not None else None,
         identify.EMBED_MODEL_VERSION if embedding is not None else None))

    if embedding is None:
        return "refuse"

    side = result["side"]
    if side not in catalogues:
        catalogues[side] = SideCatalogue(run["reserve_id"], side)
    ranked = catalogues[side].rank(embedding, cfg.top_k_candidates)
    best = ranked[0] if ranked else None
    decision, why = identify.decide(best["score"] if best else None, cfg)

    # ── the side gate ──
    # Every keypoint prediction is labelled 'right_*' regardless of the flank
    # actually shown (keypoints.py's own module docstring; docs/RESULTS.md's
    # wild evaluation found side='R' on 100% of held-out images). Auto-
    # assigning or enrolling on that basis at 50,000-frame scale writes
    # thousands of confident wrong rows into the catalogue. Withhold the
    # write, keep the crop and the embedding, and let a human see it.
    if require_side and decision in ("auto", "enroll"):
        _queue_review(crop_id, ranked, cfg,
                      "Automatic assignment is disabled: no side classifier is installed, "
                      "so this crop's flank (left or right) is not known. It was scored "
                      f"against the '{side}' catalogue by convention only. "
                      f"Model's own reading: {why}.", conn)
        return "side_withheld"

    if gate == "review" and decision in ("auto", "enroll"):
        _queue_review(crop_id, ranked, cfg,
                      "Species is not confirmed: no species classifier is installed, so "
                      "this animal detection has not been shown to be a "
                      f"{target}. Model's own reading: {why}.", conn)
        return "review"

    if decision == "auto":
        _assign(crop_id, best["ind_id"], best["score"], "auto", actor, conn)
        return "auto"
    if decision == "review":
        _queue_review(crop_id, ranked, cfg, why, conn)
        return "review"

    ind_id = repo.create_provisional_individual(run["reserve_id"], actor)
    _assign(crop_id, ind_id, 1.0, "enrolled", actor, conn)
    catalogues[side].add(ind_id, f"{ind_id}-{side}", embedding)
    return "enroll"


def _assign(crop_id, ind_id, score, decision, actor, conn) -> None:
    assign_id = repo.new_id("as_")
    prior = repo._one(conn.execute(
        "SELECT assign_id FROM assignments WHERE crop_id=? AND superseded_by IS NULL",
        (crop_id,)))
    conn.execute(
        "INSERT INTO assignments(assign_id, crop_id, ind_id, score, method, decision,"
        " confidence, decided_at, actor) VALUES (?,?,?,?,'embed',?,?,?,?)",
        (assign_id, crop_id, ind_id, score, decision, score, repo.now(), actor))
    if prior:
        conn.execute("UPDATE assignments SET superseded_by=? WHERE assign_id=?",
                     (assign_id, prior["assign_id"]))


def _queue_review(crop_id, ranked, cfg, reason, conn) -> None:
    conn.execute(
        "INSERT INTO review_queue(queue_id, crop_id, candidates, priority, reason, state)"
        " VALUES (?,?,?,?,?,'open')",
        (repo.new_id("rq_"), crop_id,
         json.dumps([{"ind_id": c["ind_id"], "score": round(c["score"], 4)}
                     for c in ranked[:cfg.top_k_candidates]]),
         ranked[0]["score"] if ranked else 0.0, reason))


def _keypoint_model_version() -> str:
    return ("yolo11-pose-2kp@1.0.0" if keypoints.WEIGHTS_PATH.exists()
            else "geometric-stub@1.0.0")


def _nothing_to_do_note(run_id: str, gate: str, target: str) -> str:
    pending = repo.count_detections_pending_stage3(run_id, None)
    if pending == 0:
        return ("Every animal detection in this run already has a crop. Stage 3 has "
                "nothing left to do.")
    return (f"{pending} animal detections are waiting, but the species gate is set to "
            f"'strict' and none of them is confirmed as a {target}. No species classifier "
            "is installed on this machine, so 'strict' can never pass. Set "
            "Identify.species_gate to 'review' to send them to a human instead.")


def _outcome_note(counts: dict, require_side: bool, gate: str) -> str:
    parts = []
    if counts["side_withheld"]:
        parts.append(
            f"{counts['side_withheld']} crops were computed but NOT assigned: no side "
            "classifier is installed, so left and right flanks cannot be told apart and "
            "an automatic match would be a coin flip against the wrong catalogue. They "
            "are in the review queue.")
    if counts["species_rejected"]:
        parts.append(f"{counts['species_rejected']} detections were not the target species.")
    if counts["refuse"]:
        parts.append(f"{counts['refuse']} crops failed the quality gate and were not matched.")
    if counts["unreadable"]:
        parts.append(f"{counts['unreadable']} frames could not be read; see the run's "
                     "dead-letter list.")
    if not parts:
        parts.append(f"{counts['auto']} matched automatically, {counts['review']} sent for "
                     f"review, {counts['enroll']} enrolled as new individuals.")
    return " ".join(parts)


# ── the whole pipeline, as one job ───────────────────────────────────────

def run_full_pipeline(run_id: str, job_id: str, actor: str = "system") -> dict:
    """Triage -> Stage 3 -> occupancy -> alerts, as one resumable job.

    This is the answer to "everything should be automated without any broken
    pipeline". In v0.1.1 the officer drove four separate buttons and the
    last two did not exist: nothing after triage ever ran automatically, and
    occupancy and alerts had no caller at all.

    Each stage checks cancellation before it starts, and each one is
    individually resumable, so cancelling during Stage 3 does not throw away
    the triage that preceded it.
    """
    from edge.pipeline import triage as triage_pipeline

    out: dict = {"run_id": run_id, "stages": {}}
    run = repo.run(run_id)

    if run["stage"] == "confirmed":
        jobs.checkpoint(job_id, detail={"stage": "triage"})
        out["stages"]["triage"] = triage_pipeline.run_triage(run_id, job_id=job_id)
        if jobs.should_stop(job_id):
            return out

    jobs.checkpoint(job_id, detail={"stage": "identify"})
    out["stages"]["stage3"] = run_stage3(run_id, job_id=job_id, actor=actor)
    if jobs.should_stop(job_id):
        return out
    repo.set_run_stage_checked(run_id, "identified", actor)

    jobs.checkpoint(job_id, detail={"stage": "occupancy_and_alerts"})
    out["stages"]["postprocess"] = postprocess.run(run_id, actor=actor)
    repo.set_run_stage_checked(run_id, "complete", actor)
    repo.finish_run(run_id)
    repo.checkpoint_wal()
    return out
