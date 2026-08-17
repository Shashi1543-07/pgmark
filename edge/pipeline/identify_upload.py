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
from edge.db import repo_ext
from edge.pipeline import classifiers, detector as detector_pipeline
from edge.pipeline import identify, keypoints, postprocess


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
    node = repo.node_id()
    file_bytes = Path(image_path).stat().st_size if Path(image_path).exists() else 0
    img_row = dict(
        image_id=image_id, reserve_id=reserve_id, run_id=None, station_id=station_id,
        orig_path=str(image_path), sha256=image_id, dhash=None, captured_at=ts,
        captured_at_raw=None, captured_at_source="unknown", drift_applied_s=0, is_night=0,
        width=None, height=None, bytes=file_bytes,
        status="pending", triage_stage=None, flags="[]",
        origin_node=node, lamport=repo.next_lamport(), synced_at=None)
    img_row["row_hash"] = repo.compute_row_hash(img_row)
    repo.insert("images", img_row)

    if not animal_detections:
        repo_ext.set_image_terminal_status(image_id, "BLANK")
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

    image = cv2.imread(image_path)
    if image is None:
        repo_ext.set_image_terminal_status(image_id, "UNREADABLE")
        repo.audit("identify.upload", actor=actor, entity_type="detection", entity_id=det_id,
                   after={"decision": "unreadable", "reason": "OpenCV could not decode upload"})
        return {"decision": "unreadable", "image_id": image_id, "det_id": det_id}

    box = (best.x, best.y, best.w, best.h)
    species = classifiers.classify_species(image, box, config.CONFIG.classifiers)
    repo_ext.set_detection_species(
        det_id, species.label, species.confidence, species.source,
        model_version=species.model_version)
    if species.label != cfg.target_species:
        crop_id = repo.new_id("crop_")
        repo_ext.record_flank_crop(
            crop_id=crop_id, det_id=det_id, side="unknown", rect_ok=False,
            quality=0.0, path=classifiers.save_raw_crop(image, box, crop_id),
            embedding=None, embed_model_version=None,
            side_confidence=None, side_source="not_run", side_model_version=None)
        decision = "unknown_species" if species.label == "unknown" else "non_target_species"
        repo_ext.set_image_terminal_status(
            image_id, "UNKNOWN_SPECIES" if species.label == "unknown" else "NON_TARGET_SPECIES")
        out = {"decision": decision, "image_id": image_id, "det_id": det_id,
               "crop_id": crop_id, "species": species.label,
               "species_confidence": species.confidence}
        if species.label == "unknown":
            out["queue_id"] = repo_ext.queue_crop_review(
                crop_id, [], 0.0,
                "Species is UNKNOWN or the local species classifier is unavailable; "
                "this image was not sent to tiger identification.")
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": decision, "species": species.label,
                          "species_source": species.source})
        return out

    side_evidence = classifiers.classify_side(image, box, config.CONFIG.classifiers)
    if side_evidence.label not in ("L", "R"):
        crop_id = repo.new_id("crop_")
        repo_ext.record_flank_crop(
            crop_id=crop_id, det_id=det_id, side="unknown", rect_ok=False,
            quality=0.0, path=classifiers.save_raw_crop(image, box, crop_id),
            embedding=None, embed_model_version=None,
            side_confidence=side_evidence.confidence, side_source=side_evidence.source,
            side_model_version=side_evidence.model_version)
        queue_id = repo_ext.queue_crop_review(
            crop_id, [], 0.0,
            "Physical flank side is UNKNOWN or the local side classifier is unavailable; "
            "this confirmed tiger was not searched against an L or R catalogue.")
        repo_ext.set_image_terminal_status(image_id, "SIDE_UNKNOWN")
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": "side_unknown", "side_source": side_evidence.source})
        return {"decision": "side_unknown", "image_id": image_id, "det_id": det_id,
                "crop_id": crop_id, "queue_id": queue_id,
                "species": species.label, "species_confidence": species.confidence,
                "side": "unknown", "side_confidence": side_evidence.confidence}

    raw_keypoints = keypoints.estimate_keypoints(box, image_path)
    kp = keypoints.apply_physical_side(raw_keypoints, side_evidence.label)
    if kp is None:
        crop_id = repo.new_id("crop_")
        repo_ext.record_flank_crop(
            crop_id=crop_id, det_id=det_id, side=side_evidence.label, rect_ok=False,
            quality=0.0, path=classifiers.save_raw_crop(image, box, crop_id),
            embedding=None, embed_model_version=None,
            side_confidence=side_evidence.confidence, side_source=side_evidence.source,
            side_model_version=side_evidence.model_version)
        queue_id = repo_ext.queue_crop_review(
            crop_id, [], 0.0,
            "Flank side is known, but the pose model did not provide a complete "
            "shoulder/hip pair for safe rectification.")
        repo_ext.set_image_terminal_status(image_id, "LOW_QUALITY")
        return {"decision": "refuse", "image_id": image_id, "det_id": det_id,
                "crop_id": crop_id, "queue_id": queue_id, "reason": "incomplete pose"}

    crop_id = repo.new_id("crop_")
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

    repo_ext.record_flank_crop(
        crop_id=crop_id, det_id=det_id, side=side_evidence.label,
        rect_ok=result["embedding"] is not None, quality=result["quality"],
        path=str(crop_path) if crop_path else None,
        embedding=identify.serialize_embedding(result["embedding"])
                  if result["embedding"] is not None else None,
        embed_model_version=identify.EMBED_MODEL_VERSION if result["embedding"] is not None else None,
        side_confidence=side_evidence.confidence, side_source=side_evidence.source,
        side_model_version=side_evidence.model_version)

    # Candidates carry a raw numpy embedding internally (identify.match()'s
    # own return shape) -- strip it before this dict goes anywhere near a
    # JSON response; ind_id/score is everything a caller needs anyway.
    clean_candidates = [{"ind_id": c["ind_id"], "entity_id": c["entity_id"],
                          "score": round(c["score"], 4)} for c in ranked[:cfg.top_k_candidates]]
    out = {"decision": decision, "reason": why, "side": side_evidence.label,
           "quality": result["quality"], "image_id": image_id, "det_id": det_id,
           "crop_id": crop_id, "candidates": clean_candidates,
           "species": species.label, "species_confidence": species.confidence,
           "side_confidence": side_evidence.confidence}

    if decision == "refuse" or result["embedding"] is None:
        repo_ext.set_image_terminal_status(image_id, "LOW_QUALITY")
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
        repo_ext.set_image_terminal_status(image_id, "IDENTIFIED")
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
        repo_ext.set_image_terminal_status(image_id, "IDENTITY_REVIEW")
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
        repo_ext.set_image_terminal_status(image_id, "NEW_INDIVIDUAL")
        out["ind_id"] = ind_id
        repo.audit("identify.upload", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"decision": "enroll", "ind_id": ind_id})

    return out


def complete_side_unknown(crop_id: str, side: str, actor: str, model=None) -> dict:
    """A human has looked at the actual photo and confirmed which flank is
    showing, for a crop Stage 3 refused because the side classifier could
    not reach its confidence threshold. Re-runs the same rectify -> embed ->
    match -> decide chain process_upload() and stage3.py use, now that the
    one missing input is known.

    This is completion of an unfinished analysis, not a new guess dressed
    up as a fact -- the side came from a person looking at the photo, not
    from the classifier that already failed to determine it, and the
    caller (the /confirm-side route) only reaches this for crops where
    species was already confirmed 'tiger' and no side had been resolved.
    A crop where the pose model still cannot find a usable near-side
    shoulder/hip pair, even with the side now known, correctly still
    refuses (CLAUDE.md rule 8) -- confirming the side is not the same as
    inventing keypoints that are not there.
    """
    if side not in ("L", "R"):
        raise ValueError(f"side must be 'L' or 'R', got {side!r}")

    ctx = repo_ext.crop_context(crop_id)
    if not ctx:
        raise KeyError(crop_id)

    image = cv2.imread(ctx["orig_path"])
    if image is None:
        raise FileNotFoundError(f"original photo no longer readable: {ctx['orig_path']}")

    box = (ctx["x"], ctx["y"], ctx["w"], ctx["h"])
    raw_keypoints = keypoints.estimate_keypoints(box, ctx["orig_path"])
    kp = keypoints.apply_physical_side(raw_keypoints, side)

    cfg = config.CONFIG.identify
    if kp is None:
        # A refusal is a terminal outcome (CLAUDE.md rule 8), not "still
        # needs a human" -- there are no keypoints to hand a reviewer, side
        # or no side. Leaving the queue item open/claimed here was the bug:
        # the reviewer's own answer was recorded (the audit entry below),
        # but the item never closed, so it kept reappearing on every page
        # load as if nothing had been answered.
        repo_ext.close_review_items_for_crop(crop_id)
        repo.audit("review.side_confirmed", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"side": side, "outcome": "still_incomplete_pose"})
        return {"decision": "refuse", "crop_id": crop_id, "side": side,
                "reason": "Side confirmed, but the pose model still has no complete "
                          "shoulder/hip pair to rectify from."}

    embed_model = model or identify.load_embedder(identify.WEIGHTS_PATH)
    result = identify.identify_crop(image, kp, [], embed_model, cfg)

    if result["embedding"] is None:
        repo_ext.close_review_items_for_crop(crop_id)
        repo.audit("review.side_confirmed", actor=actor, entity_type="crop", entity_id=crop_id,
                   after={"side": side, "outcome": "refuse", "reason": result["reason"]})
        return {"decision": "refuse", "crop_id": crop_id, "side": side, "reason": result["reason"]}

    rows = repo.crop_embeddings_for_side(ctx["reserve_id"], side)
    catalogue = [{**r, "embedding": identify.deserialize_embedding(r["embedding"])} for r in rows]
    ranked = identify.match(result["embedding"], catalogue)
    best_match = ranked[0] if ranked else None
    decision, why = identify.decide(best_match["score"] if best_match else None, cfg)

    crop_path = None
    if result["rect"] is not None:
        config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
        crop_path = config.CROPS_DIR / f"{crop_id}.jpg"
        cv2.imwrite(str(crop_path), result["rect"])

    repo_ext.update_flank_crop_analysis(
        crop_id=crop_id, side=side, rect_ok=True, quality=result["quality"],
        path=str(crop_path) if crop_path else None,
        embedding=identify.serialize_embedding(result["embedding"]),
        embed_model_version=identify.EMBED_MODEL_VERSION,
        side_confidence=1.0, side_source="human", side_model_version=None)
    repo_ext.close_review_items_for_crop(crop_id)

    clean_candidates = [{"ind_id": c["ind_id"], "entity_id": c["entity_id"], "score": round(c["score"], 4)}
                         for c in ranked[:cfg.top_k_candidates]]
    out = {"decision": decision, "reason": why, "side": side, "quality": result["quality"],
           "crop_id": crop_id, "candidates": clean_candidates}

    if decision == "auto":
        assign_id = repo.new_id("as_")
        repo.insert("assignments", dict(
            assign_id=assign_id, crop_id=crop_id, ind_id=best_match["ind_id"], score=best_match["score"],
            method="embed", decision="auto", confidence=best_match["score"],
            superseded_by=None, decided_at=repo.now(), actor=actor))
        repo.rebuild_entities(ctx["reserve_id"])
        repo_ext.set_image_terminal_status(ctx["image_id"], "IDENTIFIED")
        out["ind_id"] = best_match["ind_id"]
    elif decision == "review":
        queue_id = repo.new_id("rq_")
        repo.insert("review_queue", dict(
            queue_id=queue_id, crop_id=crop_id,
            candidates=json.dumps([{"ind_id": c["ind_id"], "score": c["score"]} for c in clean_candidates]),
            priority=best_match["score"] if best_match else 0.0, reason=why, state="open"))
        repo_ext.set_image_terminal_status(ctx["image_id"], "IDENTITY_REVIEW")
        out["queue_id"] = queue_id
    else:  # enroll
        ind_id = repo.create_provisional_individual(ctx["reserve_id"], actor)
        assign_id = repo.new_id("as_")
        repo.insert("assignments", dict(
            assign_id=assign_id, crop_id=crop_id, ind_id=ind_id, score=1.0,
            method="embed", decision="enrolled", confidence=1.0,
            superseded_by=None, decided_at=repo.now(), actor=actor))
        repo.rebuild_entities(ctx["reserve_id"])
        repo_ext.set_image_terminal_status(ctx["image_id"], "NEW_INDIVIDUAL")
        out["ind_id"] = ind_id

    if ctx.get("run_id"):
        try:
            postprocess.after_review_decision(crop_id, actor)
        except Exception:
            pass

    repo.audit("review.side_confirmed", actor=actor, entity_type="crop", entity_id=crop_id,
               after={"side": side, "decision": decision, **({"ind_id": out["ind_id"]} if "ind_id" in out else {})})
    return out
