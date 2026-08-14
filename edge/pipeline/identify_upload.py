"""Stage 3, end to end: a raw uploaded photograph in, a catalogue/review
queue/audit log entry out. See blueprint §7.4, AUDIT_AND_REVISED_PLAN.md
Task 5.

This is the orchestration layer identify.py deliberately does not
contain (its own docstring: "this module has no idea ATRW exists," and
by the same reasoning, it has no idea Pugmark's database exists either).
process_upload() is what wires edge/pipeline/detector.py (Stage B),
edge/pipeline/keypoints.py (Stage 3's keypoint input -- a stub today,
a trained regressor after AUDIT_AND_REVISED_PLAN.md Task 4), and
edge/pipeline/identify.py (rectify/embed/match/decide) into one call a
route can make, with every write going through repo.py (Rule 1).

The three-way decision lands in three different places, on purpose --
CLAUDE.md rule 8 again, refusing to collapse three genuinely different
outcomes into one table:
    auto    -> a new assignments row, superseding whatever came before
    review  -> review_queue, a human decides
    enroll  -> a brand-new provisional individual AND entity
A refusal (no animal detected, or identify.py's own quality gate/
rectification failure) lands nowhere except the audit log -- there is no
crop worth keeping a row for.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2

from edge import config
from edge.db import repo
from edge.pipeline import detector as detector_pipeline
from edge.pipeline import identify, keypoints


def process_upload(image_path: str, reserve_id: str, station_id: str | None,
                    actor: str, model=None) -> dict:
    """The full chain for one uploaded photo. Returns a result dict
    describing exactly what happened, including refusals -- a caller
    (the /api/identify/upload route) never has to guess why nothing
    landed in the catalogue."""
    cfg = config.CONFIG.identify
    det = detector_pipeline.get_detector()
    detections = det.detect(image_path, conf_threshold=config.CONFIG.triage.detector_conf_threshold)
    animal_detections = [d for d in detections if d.label == detector_pipeline.ANIMAL_LABEL]

    image_id = repo.new_id("img_")
    ts = repo.now()
    repo.insert("images", dict(
        image_id=image_id, reserve_id=reserve_id, run_id=None, station_id=station_id,
        orig_path=str(image_path), sha256=image_id, dhash=None, captured_at=ts,
        captured_at_raw=None, captured_at_source="unknown", drift_applied_s=0, is_night=0,
        width=None, height=None, bytes=Path(image_path).stat().st_size,
        status="pending", triage_stage=None, flags="[]"))

    if not animal_detections:
        repo.audit("identify.upload", actor=actor, entity_type="image", entity_id=image_id,
                   after={"decision": "no_animal_detected", "detections": len(detections)})
        return {"decision": "no_animal_detected", "image_id": image_id,
                "reason": "Stage B found no animal in this photo.",
                "other_detections": [d.label for d in detections]}

    best = max(animal_detections, key=lambda d: d.conf)
    model_name, _, model_version = detector_pipeline.MODEL_VERSION.partition("@")
    det_id = repo.new_id("det_")
    repo.insert("detections", dict(
        det_id=det_id, image_id=image_id, model=model_name, model_version=model_version,
        label=best.label, species=None, conf=round(best.conf, 4),
        x=round(best.x, 4), y=round(best.y, 4), w=round(best.w, 4), h=round(best.h, 4)))

    kp = keypoints.estimate_keypoints((best.x, best.y, best.w, best.h), image_path)

    crop_id = repo.new_id("crop_")
    image = cv2.imread(image_path)
    embed_model = model or identify.load_embedder(identify.WEIGHTS_PATH)
    # An empty catalogue here is deliberate: this call is only for
    # quality_gate()/rectify_flank()/embed() -- the real match/decide
    # happens below, against the real side-specific catalogue. Its own
    # match()/decide() (against nothing) would always say "enroll",
    # which is exactly wrong if what actually failed was the quality
    # gate or rectification -- result["embedding"] is None in both those
    # cases, and that, not result["side"], is the real branch condition.
    result = identify.identify_crop(image, kp, [], embed_model, cfg)

    if result["embedding"] is None:
        ranked, best_match = [], None
        decision, why = result["decision"], result["reason"]
    else:
        rows = repo.crop_embeddings_for_side(reserve_id, result["side"])
        catalogue = [{**r, "embedding": identify.deserialize_embedding(r["embedding"])}
                     for r in rows]
        ranked = identify.match(result["embedding"], catalogue)
        best_match = ranked[0] if ranked else None
        decision, why = identify.decide(best_match["score"] if best_match else None, cfg)

    crop_path = None
    if result["rect"] is not None:
        # The rectified crop, not the original photo -- this is what the
        # UI's stripe rail shows in place of the procedurally generated
        # pattern (a real flank image is the actual differentiator, not a
        # decorative stand-in for one).
        config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
        crop_path = config.CROPS_DIR / f"{crop_id}.jpg"
        cv2.imwrite(str(crop_path), result["rect"])

    repo.insert("flank_crops", dict(
        crop_id=crop_id, det_id=det_id, side=result["side"] or "unknown",
        rect_ok=1 if result["embedding"] is not None else 0,
        quality=result["quality"], path=str(crop_path) if crop_path else None,
        embedding=identify.serialize_embedding(result["embedding"])
                  if result["embedding"] is not None else None,
        embed_model_version=identify.EMBED_MODEL_VERSION if result["embedding"] is not None else None))

    # Candidates carry a raw numpy embedding internally (identify.match()'s
    # own return shape) -- strip it before this dict goes anywhere near a
    # JSON response; ind_id/score is everything a caller needs anyway.
    clean_candidates = [{"ind_id": c["ind_id"], "entity_id": c["entity_id"],
                          "score": round(c["score"], 4)} for c in ranked[:cfg.top_k_candidates]]
    out = {"decision": decision, "reason": why, "side": result["side"],
           "quality": result["quality"], "image_id": image_id, "det_id": det_id,
           "crop_id": crop_id, "candidates": clean_candidates}

    if decision == "refuse" or result["embedding"] is None:
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": "refuse", "reason": why})
        out["decision"] = "refuse"
        return out

    if decision == "auto":
        ind_id = best_match["ind_id"]
        assign_id = repo.new_id("as_")
        repo.insert("assignments", dict(
            assign_id=assign_id, crop_id=crop_id, ind_id=ind_id, score=best_match["score"],
            method="embed", decision="auto", confidence=best_match["score"],
            superseded_by=None, decided_at=repo.now(), actor=actor))
        repo.rebuild_entities(reserve_id)
        out["ind_id"] = ind_id
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": "auto", "ind_id": ind_id, "score": best_match["score"]})

    elif decision == "review":
        queue_id = repo.new_id("rq_")
        repo.insert("review_queue", dict(
            queue_id=queue_id, crop_id=crop_id,
            candidates=json.dumps([{"ind_id": c["ind_id"], "score": c["score"]}
                                    for c in ranked[:cfg.top_k_candidates]]),
            priority=best_match["score"] if best_match else 0.0,
            reason=why, state="open"))
        out["queue_id"] = queue_id
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": "review", "queue_id": queue_id})

    else:   # enroll
        ind_id = repo.create_provisional_individual(reserve_id, actor)
        assign_id = repo.new_id("as_")
        repo.insert("assignments", dict(
            assign_id=assign_id, crop_id=crop_id, ind_id=ind_id, score=1.0,
            method="embed", decision="enrolled", confidence=1.0,
            superseded_by=None, decided_at=repo.now(), actor=actor))
        repo.rebuild_entities(reserve_id)
        out["ind_id"] = ind_id
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": "enroll", "ind_id": ind_id})

    return out
