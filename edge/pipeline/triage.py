"""Stages 2A and 2B -- the motion prefilter and the detector. See blueprint §6.

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
processing order. See _score_group().

Only a frame at or below stage_a_blank_threshold is actually quarantined
-- one gate, and the number on the Ops screen is the number in force.

**Stage B**: MegaDetector V6 (MDV6-mit-yolov9-c), on whatever Stage A
left 'pending' -- see edge/pipeline/detector.py and docs/MODEL_CHOICES.md.
Animal detections go to status='subject'; person detections are blurred
and routed to persons_restricted, never the tiger pipeline; a frame with
no detection above threshold becomes status='blank', genuinely agreeing
with Stage A's uncertainty rather than Stage A's own guess. If the detector's
weights are not present on this machine, every pending frame stays pending
(CLAUDE.md rule 8) -- see _run_stage_b().

Quarantine is a real file operation: the frame is physically moved into
data/quarantine/<run_id>/, and manifest.json is persisted with fsync
BEFORE the file is moved -- restore() reverses it from that manifest
alone, so it survives the database being lost or power loss mid-run,
per blueprint §6.5.

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from edge import config, jobs
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


def run_triage(run_id: str, job_id: str | None = None) -> dict:
    """Triage pass with resumable checkpoints and fail-safe quarantine manifests."""
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id!r}")
    if run["stage"] != "confirmed":
        raise ValueError(
            f"run {run_id!r} is at stage {run['stage']!r}; ingest must be confirmed first")

    cfg = config.CONFIG.triage
    band_frac = config.CONFIG.ingest.timestamp_band_frac
    pending = [i for i in repo.images_for_run(run_id)
               if i["status"] == "pending" and i["station_id"]]
    skipped_no_station = sum(1 for i in repo.images_for_run(run_id)
                             if i["status"] == "pending" and not i["station_id"])

    by_station: dict[str, list[dict]] = {}
    for img in pending:
        by_station.setdefault(img["station_id"], []).append(img)

    quarantine_dir = config.QUARANTINE_DIR / run_id
    quarantined, awaiting_detector, unreadable = 0, 0, 0
    total_items = len(pending)
    processed_count = 0

    if job_id:
        jobs.checkpoint(job_id, total=total_items, done=0, detail={"phase": "stage_a_prefilter"})

    for station_id, rows in by_station.items():
        if job_id and jobs.should_stop(job_id):
            break

        groups: dict[int, list[dict]] = {}
        for row in rows:
            night_key = row["is_night"] if cfg.stage_a_separate_night else 0
            groups.setdefault(night_key, []).append(row)

        for night_key, group_rows in groups.items():
            if job_id and jobs.should_stop(job_id):
                break

            grids: dict[str, np.ndarray] = {}
            readable: list[dict] = []
            for row in group_rows:
                grid = _read_grid(row["orig_path"], cfg.stage_a_grid, band_frac)
                if grid is None:
                    repo.set_image_status(row["image_id"], "pending", "A")
                    unreadable += 1
                    processed_count += 1
                    continue
                grids[row["image_id"]] = grid
                readable.append(row)

            if not readable:
                continue

            decisions = _score_group(readable, grids, cfg)
            for row in readable:
                should_quarantine, conf_blank = decisions[row["image_id"]]
                if should_quarantine:
                    _quarantine_file(run_id, row, conf_blank, quarantine_dir)
                    quarantined += 1
                else:
                    repo.set_image_status(row["image_id"], "pending", "A")
                    awaiting_detector += 1
                processed_count += 1

            if job_id:
                jobs.checkpoint(
                    job_id, done=processed_count, total=total_items,
                    cursor=f"{station_id}:{night_key}",
                    detail={
                        "phase": "stage_a",
                        "station": station_id,
                        "quarantined": quarantined,
                        "awaiting_detector": awaiting_detector,
                        "unreadable": unreadable,
                    })

    repo.audit("triage.stage_a", actor="system", entity_type="run", entity_id=run_id,
               after={"quarantined": quarantined, "awaiting_detector": awaiting_detector,
                      "unreadable": unreadable, "skipped_no_station": skipped_no_station})

    if job_id and jobs.should_stop(job_id):
        return {
            "run_id": run_id, "quarantined": quarantined,
            "awaiting_detector": awaiting_detector, "unreadable": unreadable,
            "skipped_no_station": skipped_no_station, "interrupted": True,
            "note": "Triage paused at safe checkpoint boundary.",
        }

    stage_b = _run_stage_b(run_id, quarantine_dir, job_id=job_id)

    repo.set_run_stage(run_id, "triaged")
    return {
        "run_id": run_id, "quarantined": quarantined,
        "awaiting_detector": stage_b["awaiting_detector"], "unreadable": unreadable,
        "skipped_no_station": skipped_no_station,
        "subject": stage_b["subject"], "person": stage_b["person"],
        "vehicle": stage_b["vehicle"], "blank_by_detector": stage_b["blank"],
        "note": stage_b["note"],
    }


def _run_stage_b(run_id: str, quarantine_dir: Path, job_id: str | None = None) -> dict:
    """Stage 2B: MegaDetector V6 on whatever Stage A left 'pending'."""
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
                    note="Stage B (the detector) weights are not present in edge/models; "
                         "frames not confidently blank by Stage A stay pending, genuinely "
                         "awaiting it -- install the verified offline model bundle.")

    cfg = config.CONFIG.triage
    model_name, _, model_version = detector_pipeline.MODEL_VERSION.partition("@")

    try:
        detection_batches = det.detect_many(
            [row["orig_path"] for row in pending],
            conf_threshold=cfg.detector_conf_threshold)
    except AttributeError:
        detection_batches = []
        for row in pending:
            try:
                detection_batches.append(det.detect(
                    row["orig_path"], conf_threshold=cfg.detector_conf_threshold))
            except Exception:
                detection_batches.append(None)
    except Exception:
        detection_batches = [None] * len(pending)

    for row, detections in zip(pending, detection_batches):
        if job_id and jobs.should_stop(job_id):
            break

        if detections is None:
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
            repo.set_image_status(row["image_id"], "vehicle", "B")
            vehicle += 1
        elif any(d.label == detector_pipeline.ANIMAL_LABEL for d in detections):
            repo.set_image_status(row["image_id"], "subject", "B")
            subject += 1
        else:
            _quarantine_move(
                run_id, row, "Stage B: no animal, person or vehicle detected", 1.0,
                detector_pipeline.MODEL_VERSION, cfg.detector_conf_threshold, quarantine_dir,
                manifest_name="manifest_stage_b.json")
            repo.set_image_status(row["image_id"], "blank", "B")
            blank += 1

    repo.audit("triage.stage_b", actor="system", entity_type="run", entity_id=run_id,
               after={"subject": subject, "person": person, "vehicle": vehicle,
                      "blank": blank, "unreadable": awaiting_detector})
    return dict(subject=subject, person=person, vehicle=vehicle, blank=blank,
                awaiting_detector=awaiting_detector,
                note="Stage B ran; frames the detector could not read stay pending.")


def _restrict_person(row: dict, detection) -> None:
    """Blurs the person's box and routes into persons_restricted."""
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
    """Reverses quarantine from the on-disk manifests alone."""
    run_dir = config.QUARANTINE_DIR / run_id
    for manifest_path in (run_dir / "manifest.json", run_dir / "manifest_stage_b.json"):
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = []
        for m in manifest:
            src, dest = Path(m["quarantine_path"]), Path(m["orig_path"])
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                for attempt in range(5):
                    try:
                        shutil.move(str(src), str(dest))
                        break
                    except (PermissionError, OSError):
                        time.sleep(0.02 * (attempt + 1))
    return repo.restore_quarantine(run_id, actor)


# ── the prefilter itself ──────────────────────────────────────────────────

def cell_score(grid: np.ndarray, background: np.ndarray) -> float:
    """The maximum per-cell difference against the background, normalised to [0, 1]."""
    return float(np.max(np.abs(grid.astype(float) - background.astype(float)))) / 255.0


def _score_group(rows: list[dict], grids: dict[str, np.ndarray],
                 cfg: config.Triage) -> dict[str, tuple[bool, float]]:
    """Two-pass and order-independent: pass one fixes a single median background."""
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
    from edge import imageio
    return imageio.read_grid(orig_path, grid_n, band_frac)


def _append_manifest(quarantine_dir: Path, name: str, entries: list[dict]) -> None:
    """Write manifest entries BEFORE the files they describe are moved with fsync."""
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    path = quarantine_dir / name
    existing = []
    if path.exists():
        for _ in range(5):
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                break
            except (json.JSONDecodeError, PermissionError, OSError):
                time.sleep(0.02)
        else:
            existing = []
    have = {e["image_id"] for e in existing}
    merged = existing + [e for e in entries if e["image_id"] not in have]

    # Unique tmp path per write prevents file-locking collisions on Windows
    tmp = quarantine_dir / f"{name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

        # Windows-resilient atomic replace with exponential backoff
        replaced = False
        for attempt in range(10):
            try:
                os.replace(str(tmp), str(path))
                replaced = True
                break
            except (PermissionError, OSError):
                time.sleep(0.03 * (attempt + 1))

        if not replaced:
            try:
                shutil.copyfile(str(tmp), str(path))
            except Exception:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(merged, fh, indent=2)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _quarantine_move(run_id: str, row: dict, reason: str, conf: float,
                     model_version: str, threshold: float, quarantine_dir: Path,
                     manifest_name: str = "manifest.json") -> dict:
    """Durably writes manifest to disk BEFORE moving the file, and syncs DB."""
    src = Path(row["orig_path"])
    dest = quarantine_dir / f"{row['image_id']}_{src.name}"
    entry = {
        "image_id": row["image_id"],
        "orig_path": row["orig_path"],
        "quarantine_path": str(dest),
        "reason": reason,
        "conf": round(conf, 3),
        "model_version": model_version,
        "threshold": threshold,
    }
    # 1. Persist to manifest on disk with fsync BEFORE moving file
    _append_manifest(quarantine_dir, manifest_name, [entry])

    # 2. Move file with Windows retry resilience
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and src != dest:
        moved = False
        for attempt in range(5):
            try:
                shutil.move(str(src), str(dest))
                moved = True
                break
            except (PermissionError, OSError):
                time.sleep(0.03 * (attempt + 1))
        if not moved:
            try:
                shutil.copy2(str(src), str(dest))
                try:
                    src.unlink()
                except OSError:
                    pass
            except Exception:
                pass

    # 3. Synchronize DB quarantine row
    repo.insert_many("quarantine", [_manifest_row(entry, run_id)])
    return entry


def _quarantine_file(run_id: str, row: dict, conf_blank: float, quarantine_dir: Path,
                     manifest: list[dict] | None = None) -> dict:
    entry = _quarantine_move(
        run_id, row, "no motion vs station background", conf_blank,
        "motion-prefilter-classical@1.0.0", config.CONFIG.triage.stage_a_blank_threshold,
        quarantine_dir, manifest_name="manifest.json")
    repo.set_image_status(row["image_id"], "quarantined", "A")
    if manifest is not None:
        manifest.append(entry)
    return entry


def _manifest_row(m: dict, run_id: str) -> dict:
    try:
        size = Path(m["quarantine_path"]).stat().st_size
    except OSError:
        size = 0
    return {"q_id": repo.new_id("q_"), "run_id": run_id, "image_id": m["image_id"],
            "orig_path": m["orig_path"], "quarantine_path": m["quarantine_path"],
            "reason": m["reason"], "conf": m["conf"], "model_version": m["model_version"],
            "threshold": m["threshold"], "bytes": size, "restored_at": None}
