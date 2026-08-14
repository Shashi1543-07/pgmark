"""Stages 2A and 2B -- the motion prefilter and the detector. See
blueprint §6.

**Stage A**: classical computer vision, no model: a per-station,
per-night-window MEDIAN background (median, not mean -- a passing animal
in a few frames does not move a median) compared against each frame on a
coarse cell grid, with the burned-in timestamp band masked out first.
This is genuinely the "Stage A" cascade blueprint §6.2 describes -- the
cheap cut before the detector runs.

Two passes, not one: pass one fixes the background from every frame in a
station/night group, pass two scores every frame in that group against it.
An earlier causal, single-pass version scored each frame against only the
frames processed before it, which meant the first frame at every station
had no background to compare against, and the result depended on
processing order (AUDIT_AND_REVISED_PLAN.md P2-6). See _score_group().

Only a frame at or below stage_a_blank_threshold is actually quarantined
-- one gate, and the number on the Ops screen is the number in force
(see cell_score() and the history in AUDIT_AND_REVISED_PLAN.md P0-2/P0-3:
an earlier version scored the mean of the cell grid, which is what the
grid exists to avoid, and gated on a second, derived confidence value
that was mathematically 10x stricter than the configured threshold it
was supposedly checking).

**Stage B**: MegaDetector V6 (MDV6-mit-yolov9-c), on whatever Stage A
left 'pending' -- see edge/pipeline/detector.py and
docs/MODEL_CHOICES.md for the model and its licence. Animal detections
go to status='subject'; person detections are blurred and routed to
persons_restricted, never the tiger pipeline; a frame with no detection
above threshold becomes status='blank', genuinely agreeing with Stage A's
uncertainty rather than Stage A's own guess. If the detector's weights
are not present on this machine, every pending frame stays pending,
exactly as it did before Stage B existed (CLAUDE.md rule 8) -- see
_run_stage_b().

Quarantine is a real file operation: the frame is physically moved into
data/quarantine/<run_id>/, and manifest.json is written before the DB
row -- restore() reverses it from that manifest alone, so it survives
the database being lost, per blueprint §6.5.

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from edge import config
from edge.db import repo

def _detector_mod():
    """Import edge/pipeline/detector.py on first use, not at module import.

    detector.py imports torch at module scope, triage.py imported detector
    at module scope, and app.py imports triage -- so one missing optional
    dependency for one stage killed the entire server, including every
    screen that needs no model at all. Stage B already refuses gracefully
    when the WEIGHTS are absent (CLAUDE.md rule 8); it should do the same
    when the RUNTIME is."""
    from edge.pipeline import detector
    return detector


def run_triage(run_id: str) -> dict:
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id!r}")
    if run["stage"] != "confirmed":
        raise ValueError(
            f"run {run_id!r} is at stage {run['stage']!r}; ingest must be confirmed first")

    cfg = config.CONFIG.triage
    band_frac = config.CONFIG.ingest.timestamp_band_frac
    # A station-less image (from a folder skipped at confirm) has no
    # per-station history to compare against -- motion prefiltering is
    # inherently a per-station concept. Leave it untouched rather than
    # grouping unrelated skipped folders under one meaningless "no
    # station" background.
    pending = [i for i in repo.images_for_run(run_id)
               if i["status"] == "pending" and i["station_id"]]
    skipped_no_station = sum(1 for i in repo.images_for_run(run_id)
                              if i["status"] == "pending" and not i["station_id"])

    by_station: dict[str, list[dict]] = {}
    for img in pending:
        by_station.setdefault(img["station_id"], []).append(img)

    quarantine_dir = config.QUARANTINE_DIR / run_id
    manifest: list[dict] = []
    quarantined, awaiting_detector, unreadable = 0, 0, 0

    for station_id, rows in by_station.items():
        groups: dict[int, list[dict]] = {}
        for row in rows:
            night_key = row["is_night"] if cfg.stage_a_separate_night else 0
            groups.setdefault(night_key, []).append(row)

        for night_key, group_rows in groups.items():
            grids: dict[str, np.ndarray] = {}
            readable: list[dict] = []
            for row in group_rows:
                grid = _read_grid(row["orig_path"], cfg.stage_a_grid, band_frac)
                if grid is None:
                    repo.set_image_status(row["image_id"], "pending", "A")
                    unreadable += 1
                    continue
                grids[row["image_id"]] = grid
                readable.append(row)
            if not readable:
                continue

            decisions = _score_group(readable, grids, cfg)
            for row in readable:
                should_quarantine, conf_blank = decisions[row["image_id"]]
                if should_quarantine:
                    _quarantine_file(run_id, row, conf_blank, quarantine_dir, manifest)
                    quarantined += 1
                else:
                    repo.set_image_status(row["image_id"], "pending", "A")
                    awaiting_detector += 1

    if manifest:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        (quarantine_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        repo.insert_many("quarantine", [_manifest_row(m, run_id) for m in manifest])

    repo.audit("triage.stage_a", actor="system", entity_type="run", entity_id=run_id,
               after={"quarantined": quarantined, "awaiting_detector": awaiting_detector,
                      "unreadable": unreadable, "skipped_no_station": skipped_no_station})

    stage_b = _run_stage_b(run_id, quarantine_dir)

    repo.set_run_stage(run_id, "triaged")
    return {
        "run_id": run_id, "quarantined": quarantined,
        "awaiting_detector": stage_b["awaiting_detector"], "unreadable": unreadable,
        "skipped_no_station": skipped_no_station,
        "subject": stage_b["subject"], "person": stage_b["person"],
        "vehicle": stage_b["vehicle"], "blank_by_detector": stage_b["blank"],
        "note": stage_b["note"],
    }


def _run_stage_b(run_id: str, quarantine_dir: Path) -> dict:
    """Stage 2B: the actual detector, on whatever Stage A left 'pending'.
    MegaDetector V6 (MDV6-mit-yolov9-c) -- see edge/pipeline/detector.py
    and docs/MODEL_CHOICES.md for the model and licence.

    If the weights are not present (a laptop that only ever fetched Stage
    A's dependencies, or a build that has not run
    `python -m tools.fetch_data --set megadetector`), refuse rather than
    crash: every frame stays 'pending', genuinely awaiting a detector
    that is not there, exactly as it did before Stage B existed
    (CLAUDE.md rule 8).

    A frame the detector finds nothing in is physically quarantined here
    too, exactly like Stage A's motion-prefilter blanks -- the Blank
    Frames screen promises 'moved to quarantine, never deleted, can be
    put back' without carving out an exception for which stage decided
    that, so there isn't one. images.status still records 'blank'/'B'
    rather than reusing Stage A's 'quarantined'/'A', so it stays visible
    *which* stage made the call."""
    detector_pipeline = _detector_mod()
    pending = [i for i in repo.images_for_run(run_id)
               if i["status"] == "pending" and i["triage_stage"] == "A"]
    subject = person = vehicle = blank = awaiting_detector = 0
    if not pending:
        return dict(subject=0, person=0, vehicle=0, blank=0, awaiting_detector=0,
                     note="Stage B: nothing left for the detector after Stage A.")

    try:
        det = detector_pipeline.get_detector()
    except FileNotFoundError:
        repo.audit("triage.stage_b", actor="system", entity_type="run", entity_id=run_id,
                   after={"skipped": "weights not found"})
        return dict(subject=0, person=0, vehicle=0, blank=0, awaiting_detector=len(pending),
                     note="Stage B (the detector) weights are not downloaded on this "
                          "machine; frames not confidently blank by Stage A stay pending, "
                          "genuinely awaiting it -- run "
                          "`python -m tools.fetch_data --set megadetector`.")

    cfg = config.CONFIG.triage
    model_name, _, model_version = detector_pipeline.MODEL_VERSION.partition("@")
    manifest: list[dict] = []
    for row in pending:
        try:
            detections = det.detect(row["orig_path"], conf_threshold=cfg.detector_conf_threshold)
        except Exception:
            repo.set_image_status(row["image_id"], "pending", "A")
            awaiting_detector += 1
            continue

        if detections:
            repo.insert_many("detections", [
                dict(det_id=repo.new_id("det_"), image_id=row["image_id"], model=model_name,
                     model_version=model_version, label=d.label, species=None,
                     conf=round(d.conf, 4), x=round(d.x, 4), y=round(d.y, 4),
                     w=round(d.w, 4), h=round(d.h, 4))
                for d in detections])

        best_person = max((d for d in detections if d.label == detector_pipeline.PERSON_LABEL),
                           key=lambda d: d.conf, default=None)
        if best_person is not None:
            _restrict_person(row, best_person)
            person += 1
        elif any(d.label == detector_pipeline.VEHICLE_LABEL for d in detections):
            # A jeep is not a subject. v0.1.1 wrote status='subject' here,
            # so every vehicle frame joined the animal frames in the list
            # the UI offered for identification, and Stage 3 would try to
            # find a shoulder and a hip on a Mahindra Bolero.
            repo.set_image_status(row["image_id"], "vehicle", "B")
            vehicle += 1
        elif any(d.label == detector_pipeline.ANIMAL_LABEL for d in detections):
            repo.set_image_status(row["image_id"], "subject", "B")
            subject += 1
        else:
            manifest.append(_quarantine_move(
                run_id, row, "Stage B: no animal, person or vehicle detected", 1.0,
                detector_pipeline.MODEL_VERSION, cfg.detector_conf_threshold, quarantine_dir))
            repo.set_image_status(row["image_id"], "blank", "B")
            blank += 1

    if manifest:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = quarantine_dir / "manifest_stage_b.json"
        existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
        manifest_path.write_text(json.dumps(existing + manifest, indent=2))
        repo.insert_many("quarantine", [_manifest_row(m, run_id) for m in manifest])

    repo.audit("triage.stage_b", actor="system", entity_type="run", entity_id=run_id,
               after={"subject": subject, "person": person, "vehicle": vehicle,
                      "blank": blank, "unreadable": awaiting_detector})
    return dict(subject=subject, person=person, vehicle=vehicle, blank=blank,
                awaiting_detector=awaiting_detector,
                note="Stage B ran; frames the detector could not read stay pending.")


def _restrict_person(row: dict, detection) -> None:
    """Blurs the person's box (config.Privacy.blur_person_boxes) and
    routes the frame into persons_restricted rather than the tiger
    pipeline -- a person is not a tiger sighting, and CLAUDE.md's
    role-gating rules exist because this frame carries a face, not a
    flank."""
    detector_pipeline = _detector_mod()
    src = Path(row["orig_path"])
    blurred_path = config.RESTRICTED_DIR / f"{row['image_id']}_blurred.jpg"
    blurred_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            if config.CONFIG.privacy.blur_person_boxes:
                w, h = img.size
                box = (int(detection.x * w), int(detection.y * h),
                       int((detection.x + detection.w) * w), int((detection.y + detection.h) * h))
                region = img.crop(box).filter(ImageFilter.GaussianBlur(radius=25))
                img.paste(region, box)
            img.save(blurred_path, "JPEG")
    except Exception:
        blurred_path = None

    repo.insert_many("persons_restricted", [dict(
        image_id=row["image_id"], blurred_path=str(blurred_path) if blurred_path else "",
        access_count=0)])
    repo.set_image_status(row["image_id"], detector_pipeline.PERSON_LABEL, "B")


def restore(run_id: str, actor: str = "system") -> int:
    """Reverses quarantine from the on-disk manifests alone -- idempotent
    (a file already moved back is simply not found a second time), and
    the DB row update happens regardless, since seeded/demonstration
    quarantine rows were never backed by a real file to move in the first
    place. Two manifests, not one: Stage A (motion prefilter) and Stage B
    (the real detector) each quarantine independently and each keep their
    own manifest.json, but 'put every frame back' means both."""
    run_dir = config.QUARANTINE_DIR / run_id
    for manifest_path in (run_dir / "manifest.json", run_dir / "manifest_stage_b.json"):
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = []
        for m in manifest:
            src, dest = Path(m["quarantine_path"]), Path(m["orig_path"])
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
    # repo.restore_quarantine() already resets images.status back to
    # 'pending' for the whole run; nothing further to do on that side.
    return repo.restore_quarantine(run_id, actor)


# ── the prefilter itself ──────────────────────────────────────────────────

def cell_score(grid: np.ndarray, background: np.ndarray) -> float:
    """The maximum per-cell difference against the background, normalised
    to [0, 1]. Deliberately the max, not the mean or a high percentile:
    a 16x16 grid has 256 cells, so a subject occupying a single cell is
    1/256 of the frame -- below the 99.6th percentile, let alone the
    98th, so nothing short of the max reliably survives being outvoted
    by 255 quiet cells. Each cell is already a downsampled average of a
    large block of source pixels (see _read_grid), so this is a max over
    already-smoothed values, not raw per-pixel noise -- see
    tests/unit/test_triage_scoring.py for the worst-case table this
    threshold has to survive (AUDIT_AND_REVISED_PLAN.md P0-2)."""
    return float(np.max(np.abs(grid.astype(float) - background.astype(float)))) / 255.0


def _score_group(rows: list[dict], grids: dict[str, np.ndarray],
                  cfg: config.Triage) -> dict[str, tuple[bool, float]]:
    """Two-pass and order-independent: pass one fixes a single median
    background over every frame in the group, pass two scores every frame
    in the group against that one background.

    This replaces an earlier causal, single-pass design where each frame
    was scored against a running window of only the frames *before* it --
    which made the first frame at every station/night a special case with
    no background at all, and made the result depend on the order frames
    happened to be processed in (AUDIT_AND_REVISED_PLAN.md P2-6). Sorting
    by (captured_at, image_id) before sampling makes the chosen background
    depend only on the *set* of frames in the group, not on the order this
    function receives them in -- see the "shuffled input order" and
    "running triage twice" checks in tests/live/test_routes.py.

    Below stage_a_min_frames_for_background frames, there isn't enough of
    a group to form a background that means anything -- refusing to guess
    (CLAUDE.md rule 8) beats a median that is mostly the frame it is being
    compared against. Every frame in that group stays pending, exactly
    like the old cold-start case.

    Returns {image_id: (should_quarantine, confidence)}.
    """
    ordered = sorted(rows, key=lambda r: (r["captured_at"] or "", r["image_id"]))
    if len(ordered) < cfg.stage_a_min_frames_for_background:
        return {r["image_id"]: (False, 0.0) for r in ordered}

    all_grids = [grids[r["image_id"]] for r in ordered]
    sample = all_grids
    if len(sample) > cfg.stage_a_median_window:
        idx = np.linspace(0, len(sample) - 1, cfg.stage_a_median_window).astype(int)
        sample = [sample[i] for i in idx]
    background = np.median(np.stack(sample), axis=0)

    out: dict[str, tuple[bool, float]] = {}
    for row in ordered:
        score = cell_score(grids[row["image_id"]], background)
        if score <= cfg.stage_a_blank_threshold:
            conf_blank = max(0.0, min(1.0, 1 - score / cfg.stage_a_blank_threshold))
            out[row["image_id"]] = (True, conf_blank)
        else:
            out[row["image_id"]] = (False, 0.0)
    return out


def _read_grid(orig_path: str, grid_n: int, band_frac: float) -> np.ndarray | None:
    """Greyscale, timestamp band masked out (blueprint §6.3), resized to a
    coarse cell grid -- resizing doubles as the cell-average: a subject
    filling 4% of the frame barely moves a global mean but reliably shows
    up in several cells at this resolution."""
    # Delegates to edge/imageio.py, which asks libjpeg to decode the JPEG's
    # DCT coefficients at 1/8 scale instead of building the full 12-megapixel
    # bitmap and immediately throwing 99.99% of it away. Measured on a
    # 4000x3000 camera-trap frame: 143.7 ms -> 38.4 ms, which is the
    # difference between two hours and half an hour over 50,000 frames.
    # It oversamples to grid_n*8 before the final resize so cell_score()'s
    # calibration (tests/unit/test_triage_scoring.py) is unchanged.
    from edge import imageio
    return imageio.read_grid(orig_path, grid_n, band_frac)


def _append_manifest(quarantine_dir: Path, name: str, entries: list[dict]) -> None:
    """Write manifest entries BEFORE the files they describe are moved.

    This is the fix for the worst bug in v0.1.1. The module docstring above
    claims the manifest is written before the DB row so restore() survives
    losing the database -- but the write happened after the whole
    station loop, so a crash mid-run left thousands of original frames
    physically moved into quarantine with nothing anywhere recording where
    they came from.

    Append-then-move, per batch, with an fsync. The worst case is now a
    manifest entry for a file that was never moved, which restore()
    already tolerates (it checks src.exists() first) -- the opposite and
    survivable failure.
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    path = quarantine_dir / name
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = []
    have = {e["image_id"] for e in existing}
    merged = existing + [e for e in entries if e["image_id"] not in have]
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.flush()
        import os as _os
        _os.fsync(fh.fileno())
    tmp.replace(path)


def _quarantine_move(run_id: str, row: dict, reason: str, conf: float,
                      model_version: str, threshold: float, quarantine_dir: Path) -> dict:
    """Physically moves a frame into data/quarantine/<run_id>/ and returns
    a manifest entry -- shared by Stage A (motion prefilter) and Stage B
    (the real detector confirming blank), so 'moved to quarantine, never
    deleted, can be put back' (the Blank Frames screen's own promise) is
    true regardless of which stage made the call."""
    src = Path(row["orig_path"])
    # image_id (content-addressed) prefixes the name so two files sharing a
    # filename from different station folders can't collide in one run's
    # flat quarantine directory.
    dest = quarantine_dir / f"{row['image_id']}_{src.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.move(str(src), str(dest))
    return {"image_id": row["image_id"], "orig_path": row["orig_path"],
            "quarantine_path": str(dest), "reason": reason, "conf": round(conf, 3),
            "model_version": model_version, "threshold": threshold}


def _quarantine_file(run_id: str, row: dict, conf_blank: float, quarantine_dir: Path,
                      manifest: list[dict]) -> None:
    manifest.append(_quarantine_move(
        run_id, row, "no motion vs station background", conf_blank,
        "motion-prefilter-classical@1.0.0", config.CONFIG.triage.stage_a_blank_threshold,
        quarantine_dir))
    repo.set_image_status(row["image_id"], "quarantined", "A")


def _manifest_row(m: dict, run_id: str) -> dict:
    try:
        size = Path(m["quarantine_path"]).stat().st_size
    except OSError:
        size = 0
    return {"q_id": repo.new_id("q_"), "run_id": run_id, "image_id": m["image_id"],
            "orig_path": m["orig_path"], "quarantine_path": m["quarantine_path"],
            "reason": m["reason"], "conf": m["conf"], "model_version": m["model_version"],
            "threshold": m["threshold"], "bytes": size, "restored_at": None}
