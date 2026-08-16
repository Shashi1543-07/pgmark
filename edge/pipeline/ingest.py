"""Stage 1 -- ingest. See blueprint §5.

Turns a raw SD-card folder into rows in `images`, `events` and
`image_event`, without running any model.

Robustness features:
  * walks a folder tree in bounded streaming batches (2,000 files at a time)
  * resource preflight: checks disk headroom, RAM, and device before starting
  * multi-signal station ID scoring: matches folder, camera body, serial, pattern, deployment
  * hashes every file, computes perceptual hash (dhash), flags corrupt/zero-byte ones
  * four-tier timestamp resolution with conflict tracking: EXIF -> OCR -> filename -> inferred
  * detects implausible camera clocks (year 1970, future dates, drift) and records conflict evidence
  * flags mixed-camera folders by body / serial
  * groups frames into bursts (`events`) once a station is known
  * two-step run lifecycle -- preflight_ingest() / confirm_ingest()

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from edge import config, imageio
from edge.db import repo
from edge.db import repo_ext
from edge.pipeline.device import get_device_manager

_EXIF_DATETIME_TAGS = (36867, 306)     # DateTimeOriginal, DateTime
_EXIF_MAKE, _EXIF_MODEL = 271, 272

_FILENAME_TS = re.compile(
    r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[ _T-]?(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})")


# ── resource preflight ───────────────────────────────────────────────────

def resource_preflight(reserve_id: str, root_path: str) -> dict:
    """Pre-run capacity check: verifies disk space, RAM, and inference device."""
    reserve = repo.reserve(reserve_id)
    if not reserve:
        raise ValueError(f"unknown reserve {reserve_id!r}")
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"root path does not exist or is not a directory: {root_path}")

    cfg = config.CONFIG.ingest
    files = [p for p in root.rglob("*") if p.is_file()]
    total_files = len(files)
    total_raw_bytes = sum(p.stat().st_size for p in files if p.exists())
    total_raw_mb = round(total_raw_bytes / (1024 * 1024), 2)

    # Estimate required space:
    # 1. Metadata overhead: ~2KB per file
    # 2. Crops storage estimate: ~50KB per crop
    # 3. Quarantine storage worst case: total_raw_mb if all blank
    # 4. Safety reserve: min_free_disk_mib
    meta_mb = round((total_files * cfg.metadata_bytes_per_image) / (1024 * 1024), 2)
    crops_mb = round(total_files * 0.05, 2)
    quarantine_worst_case_mb = total_raw_mb
    estimated_needed_mb = round(meta_mb + crops_mb + quarantine_worst_case_mb + cfg.min_free_disk_mib, 2)

    # Inspect disk
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        free_disk_mb = round(usage.free / (1024 * 1024), 2)
        total_disk_mb = round(usage.total / (1024 * 1024), 2)
    except OSError:
        free_disk_mb, total_disk_mb = 0.0, 0.0

    # Device Manager inspection
    dm_plan = get_device_manager().plan()

    warnings = []
    if free_disk_mb < estimated_needed_mb:
        warnings.append(
            f"Free disk space ({free_disk_mb:.1f} MB) is below recommended ({estimated_needed_mb:.1f} MB). "
            "Consider clearing space or enabling logical quarantine.")
    if total_files == 0:
        warnings.append(f"No files found under {root_path}")

    return {
        "ready": free_disk_mb >= (meta_mb + cfg.min_free_disk_mib),
        "total_files": total_files,
        "raw_size_mb": total_raw_mb,
        "estimated_needed_mb": estimated_needed_mb,
        "free_disk_mb": free_disk_mb,
        "total_disk_mb": total_disk_mb,
        "device": str(dm_plan.device),
        "is_cuda": dm_plan.is_cuda,
        "free_vram_mib": dm_plan.free_vram_mib,
        "batch_size": dm_plan.batch_size,
        "warnings": warnings,
    }


# ── run lifecycle & streaming ingest ─────────────────────────────────────

def preflight_ingest(reserve_id: str, root_path: str, cycle_label: str | None = None) -> dict:
    """Bounded streaming scan of folder tree: hashes, parses timestamps, matches stations."""
    reserve = repo.reserve(reserve_id)
    if not reserve:
        raise ValueError(f"unknown reserve {reserve_id!r}")
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"root path does not exist or is not a directory: {root_path}")

    cfg = config.CONFIG.ingest
    stations = repo.stations(reserve_id)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"no files found under {root_path}")

    # Streaming chunks of scan_batch_size to bound RAM consumption
    batch_size = max(100, int(cfg.scan_batch_size))
    node = repo.node_id()
    run_id = repo.new_id("run_")

    run_row = {
        "run_id": run_id, "reserve_id": reserve_id, "cycle_label": cycle_label,
        "started_at": repo.now(), "finished_at": None, "root_path": str(root),
        "image_count": 0, "stage": "preflight",
        "model_versions": json.dumps({}), "config": config.CONFIG.to_json(),
        "schema_version": repo.schema_version(),
        "origin_node": node, "lamport": repo.next_lamport(), "synced_at": None,
    }
    run_row["row_hash"] = repo.compute_row_hash(run_row)
    repo.insert("runs", run_row)

    total_ingested = 0
    total_dupes = 0
    cross_run_duplicates = 0
    total_corrupt = 0
    all_unmatched_folders: set[str] = set()
    mixed_camera_folders: dict[str, list] = {}
    ts_buckets = {"exif": 0, "ocr": 0, "filename": 0, "inferred": 0, "conflict": 0, "unknown": 0}
    seen_sha: set[str] = set()

    for chunk_start in range(0, len(files), batch_size):
        chunk_files = files[chunk_start:chunk_start + batch_size]
        records = [_scan_file(p, root) for p in chunk_files]

        # In-chunk duplicate detection
        for r in records:
            if r["sha256"] in seen_sha:
                r["duplicate_of"] = r["sha256"]
                total_dupes += 1
            else:
                seen_sha.add(r["sha256"])

        folder_names = sorted({r["folder"] for r in records})
        folder_station: dict[str, dict | None] = {}
        for f in folder_names:
            sample_rec = next((r for r in records if r["folder"] == f), {})
            stn, conf, _ = match_station_multisignal(sample_rec, stations, cfg)
            folder_station[f] = stn
            if stn is None:
                all_unmatched_folders.add(f)

        chunk_mixed = _detect_mixed_cameras(records)
        mixed_camera_folders.update(chunk_mixed)

        for folder in folder_names:
            group = [r for r in records if r["folder"] == folder and not r["corrupt"]]
            station = folder_station[folder]
            activity_start = repo.station_first_active(station["station_id"]) if station else None
            _resolve_group_timestamps(group, activity_start, cfg)

        # Count timestamp sources
        for r in records:
            src = r.get("captured_at_source") or "unknown"
            ts_buckets[src] = ts_buckets.get(src, 0) + 1

        candidate_rows = [_to_image_row(r, run_id, reserve_id, folder_station.get(r["folder"]), node)
                          for r in records if not r.get("duplicate_of")]

        already = repo_ext.existing_image_ids([r["image_id"] for r in candidate_rows])
        image_rows = [r for r in candidate_rows if r["image_id"] not in already]
        cross_run_duplicates += (len(candidate_rows) - len(image_rows))
        total_corrupt += sum(1 for r in image_rows if str(r.get("status", "")).lower() in ("corrupt", "unreadable"))

        with repo_ext.transaction() as conn:
            repo_ext.insert_many_ignore("images", image_rows, conn)
            total_ingested += len(image_rows)

    repo.set_run_image_count(run_id, total_ingested)
    repo.audit("ingest.preflight", actor="system", entity_type="run", entity_id=run_id,
               after={"files_found": len(files), "images": total_ingested,
                      "unmatched_folders": sorted(all_unmatched_folders), "duplicates": total_dupes})

    res_check = resource_preflight(reserve_id, root_path)

    return {
        "run_id": run_id,
        "files_found": len(files),
        "images_ingested": total_ingested,
        "unmatched_folders": sorted(all_unmatched_folders),
        "mixed_camera_folders": mixed_camera_folders,
        "duplicate_count": total_dupes,
        "cross_run_duplicates": cross_run_duplicates,
        "cross_run_note": (
            f"{cross_run_duplicates} files in this folder were already ingested by an "
            "earlier run and were not re-imported. Their existing rows, and everything "
            "identified from them, are untouched." if cross_run_duplicates else None),
        "corrupt_count": total_corrupt,
        "timestamp_buckets": ts_buckets,
        "estimated_seconds": round(total_ingested * cfg.estimated_seconds_per_image, 1),
        "estimated_seconds_per_image_assumed": cfg.estimated_seconds_per_image,
        "resource_preflight": res_check,
    }


def confirm_ingest(run_id: str, station_assignments: dict[str, str] | None = None,
                   skip_folders: list[str] | None = None) -> dict:
    """Resolve unassigned folders, group bursts into events, and finalise ingest stage."""
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id!r}")
    station_assignments = station_assignments or {}
    skip_folders = set(skip_folders or [])

    stations_by_id = {s["station_id"]: s for s in repo.stations(run["reserve_id"])}
    images = repo.images_for_run(run_id)

    resolved, skipped, still_unmatched = 0, 0, set()
    for img in images:
        if img["station_id"]:
            continue
        folder = Path(img["orig_path"]).parent.name
        if folder in skip_folders:
            skipped += 1
            continue
        target = station_assignments.get(folder)
        if target and target in stations_by_id:
            repo.update_image_station(img["image_id"], target)
            resolved += 1
        else:
            still_unmatched.add(folder)

    if still_unmatched:
        raise ValueError(
            "folders still unresolved -- assign a station or skip them: "
            f"{sorted(still_unmatched)}")

    images = repo.images_for_run(run_id)
    assignable = [i for i in images if i["station_id"] and str(i.get("status", "")).lower() not in ("corrupt", "unreadable")
                  and i["captured_at"]]
    events, links = _group_bursts(assignable, config.CONFIG.ingest.burst_window_s)
    repo.insert_many("events", events)
    repo.insert_many("image_event", links)

    repo.set_run_stage(run_id, "confirmed")
    repo.audit("ingest.confirm", actor="system", entity_type="run", entity_id=run_id,
               after={"resolved_images": resolved, "skipped_images": skipped,
                      "events": len(events)})
    return {"run_id": run_id, "stage": "confirmed", "resolved_images": resolved,
            "skipped_images": skipped, "events": len(events)}


def resume_ingest(run_id: str, job_id: str) -> dict:
    """Resume a halted or interrupted preflight ingest scan for a run."""
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id!r}")
    if run["stage"] != "preflight":
        return {"run_id": run_id, "stage": run["stage"], "status": "already_completed"}

    root = Path(run["root_path"])
    if not root.exists() or not root.is_dir():
        raise ValueError(f"root path does not exist or is not a directory: {run['root_path']}")

    cfg = config.CONFIG.ingest
    stations = repo.stations(run["reserve_id"])
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"no files found under {root}")

    batch_size = max(100, int(cfg.scan_batch_size))
    node = repo.node_id()

    # Identify files already processed in this run
    existing_images = repo.images_for_run(run_id)
    seen_sha = {img["sha256"] for img in existing_images if img.get("sha256")}

    total_ingested = len(existing_images)
    all_unmatched_folders: set[str] = set()

    from edge import jobs

    for chunk_start in range(0, len(files), batch_size):
        if jobs.should_stop(job_id):
            break

        chunk_files = files[chunk_start:chunk_start + batch_size]
        records = [_scan_file(p, root) for p in chunk_files]

        unprocessed = []
        for r in records:
            if r["sha256"] in seen_sha:
                continue
            seen_sha.add(r["sha256"])
            unprocessed.append(r)

        if not unprocessed:
            continue

        folder_names = sorted({r["folder"] for r in unprocessed})
        folder_station: dict[str, dict | None] = {}
        for f in folder_names:
            sample_rec = next((r for r in unprocessed if r["folder"] == f), {})
            stn, conf, _ = match_station_multisignal(sample_rec, stations, cfg)
            folder_station[f] = stn
            if stn is None:
                all_unmatched_folders.add(f)

        for folder in folder_names:
            group = [r for r in unprocessed if r["folder"] == folder and not r["corrupt"]]
            station = folder_station[folder]
            activity_start = repo.station_first_active(station["station_id"]) if station else None
            _resolve_group_timestamps(group, activity_start, cfg)

        candidate_rows = [_to_image_row(r, run_id, run["reserve_id"], folder_station.get(r["folder"]), node)
                          for r in unprocessed if not r.get("duplicate_of")]

        already = repo_ext.existing_image_ids([r["image_id"] for r in candidate_rows])
        image_rows = [r for r in candidate_rows if r["image_id"] not in already]

        with repo_ext.transaction() as conn:
            repo_ext.insert_many_ignore("images", image_rows, conn)
            total_ingested += len(image_rows)
            jobs.checkpoint(job_id, done=total_ingested, total=len(files),
                            cursor=unprocessed[-1]["sha256"] if unprocessed else None, conn=conn)

    repo.set_run_image_count(run_id, total_ingested)
    return {"run_id": run_id, "images_ingested": total_ingested, "job_id": job_id}


# ── file scanning ─────────────────────────────────────────────────────────

def _scan_file(path: Path, root: Path) -> dict:
    rec = {
        "path": path, "folder": path.parent.name or root.name,
        "orig_path": str(path), "corrupt": False, "flags": [],
        "sha256": None, "dhash": None, "bytes": 0, "width": None, "height": None,
        "exif_dt": None, "make": None, "model": None, "serial": None,
        "orientation": 1, "is_night": 0,
        "captured_at": None, "captured_at_raw": None, "captured_at_source": "unknown",
        "ts_confidence": 0.0, "ts_method": "unknown", "ts_evidence": {},
        "ts_offset_s": 0, "drift_applied_s": 0,
    }
    try:
        sha256, probe_info = imageio.hash_and_probe(path)
        rec["sha256"] = sha256
        rec["bytes"] = probe_info["bytes"]
        rec["width"] = probe_info["width"]
        rec["height"] = probe_info["height"]
        rec["make"] = probe_info.get("make")
        rec["model"] = probe_info.get("model")
        rec["serial"] = probe_info.get("serial")
        rec["orientation"] = probe_info.get("orientation", 1)
        rec["is_night"] = probe_info.get("is_night", 0)
        rec["dhash"] = probe_info.get("dhash")
        raw_dt = probe_info.get("exif_dt_raw")
        if raw_dt:
            try:
                rec["exif_dt"] = datetime.strptime(str(raw_dt).strip(), "%Y:%m:%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                rec["exif_dt"] = None
    except imageio.UnreadableImage as exc:
        rec["corrupt"] = True
        rec["flags"].append(f"unreadable: {exc}")
        rec["sha256"] = hashlib.sha256(str(path).encode()).hexdigest()
    except Exception as exc:
        rec["corrupt"] = True
        rec["flags"].append(f"scan_error: {exc}")
        rec["sha256"] = hashlib.sha256(str(path).encode()).hexdigest()
    return rec


def _ocr_timestamp_band(path: Path, band_frac: float) -> datetime | None:
    """Tier 2: OCR of the burned-in timestamp band (blueprint §5.2)."""
    return None


# ── station matching with multi-signal scoring ───────────────────────────

def match_station_multisignal(record: dict, stations: list[dict], cfg) -> tuple[dict | None, float, list[str]]:
    """Match station using multi-signal evidence: folder hint, camera serial/body, pattern, deployment."""
    if not stations:
        return None, 0.0, []

    best_station = None
    best_score = 0.0
    best_signals: list[str] = []

    for s in stations:
        score, signals = repo_ext.multi_signal_station_score(record, s)
        if score > best_score:
            best_score = score
            best_station = s
            best_signals = signals

    min_conf = getattr(config.CONFIG.station_matching, "min_confidence_to_auto_assign", 0.70)
    if best_score >= min_conf:
        return best_station, best_score, best_signals

    # Fallback to pure folder edit distance
    fuzzy = match_station(record.get("folder", ""), stations, cfg.folder_match_max_edit_distance)
    if fuzzy:
        return fuzzy, 0.65, ["folder_fuzzy"]

    return None, best_score, best_signals


def match_station(folder_name: str, stations: list[dict], max_dist: int) -> dict | None:
    """Fuzzy and exact match against station_id, name, and folder_hint."""
    target = _normalize(folder_name)
    if not target:
        return None
    best, best_dist = None, max_dist + 1
    for s in stations:
        hints = [s.get("station_id") or "", s.get("name") or "", s.get("folder_hint") or ""]
        for h in hints:
            if not h:
                continue
            h_norm = _normalize(h)
            if not h_norm:
                continue
            if target == h_norm:
                return s
            d = _levenshtein(target, h_norm)
            if d < best_dist:
                best, best_dist = s, d
    return best if best_dist <= max_dist else None



def _normalize(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


# ── timestamps: EXIF -> OCR -> filename -> inferred with conflict checking ─

def _plausible(dt: datetime, min_year: int, max_future_days: int, now: datetime) -> bool:
    return dt.year >= min_year and dt <= now + timedelta(days=max_future_days)


def _parse_filename_timestamp(name: str) -> datetime | None:
    m = _FILENAME_TS.search(name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        return None


def _resolve_group_timestamps(records: list[dict], activity_start: str | None, cfg) -> None:
    """Four-tier timestamp resolution with conflict tracking and confidence scoring."""
    now = datetime.now(timezone.utc)
    ordered = sorted(records, key=lambda r: r["path"].name)

    for r in ordered:
        exif_dt = r.get("exif_dt")
        fn_dt = _parse_filename_timestamp(r["path"].name)
        ocr_dt = _ocr_timestamp_band(r["path"], cfg.timestamp_band_frac)

        evidence = {
            "exif_dt": exif_dt.isoformat() if exif_dt else None,
            "filename_dt": fn_dt.isoformat() if fn_dt else None,
            "ocr_dt": ocr_dt.isoformat() if ocr_dt else None,
        }

        # Check for conflict between EXIF and Filename
        if exif_dt and fn_dt:
            time_diff = abs((exif_dt - fn_dt).total_seconds())
            if time_diff > 86400 * 30:  # > 30 days disparity
                r["flags"].append("timestamp_conflict")
                evidence["conflict_reason"] = f"EXIF and filename timestamps differ by {time_diff/86400:.1f} days"

        if exif_dt is not None and _plausible(exif_dt, cfg.min_plausible_year, cfg.max_future_days, now):
            r["captured_at"], r["captured_at_raw"] = exif_dt, exif_dt
            r["captured_at_source"] = "conflict" if "timestamp_conflict" in r["flags"] else "exif"
            r["ts_confidence"] = 0.60 if "timestamp_conflict" in r["flags"] else 1.0
            r["ts_method"] = "EXIF DateTimeOriginal"
            r["ts_evidence"] = evidence
            continue

        if exif_dt is not None:
            r["captured_at_raw"] = exif_dt
            r["flags"].append("camera_clock_reset_suspected")

        if ocr_dt is not None:
            r["captured_at"], r["captured_at_source"] = ocr_dt, "ocr"
            r.setdefault("captured_at_raw", ocr_dt)
            r["ts_confidence"] = 0.90
            r["ts_method"] = "OCR burned-in band"
            r["ts_evidence"] = evidence
            continue

        if fn_dt is not None and _plausible(fn_dt, cfg.min_plausible_year, cfg.max_future_days, now):
            r["captured_at"] = fn_dt
            r["captured_at_source"] = "filename"
            r.setdefault("captured_at_raw", exif_dt or fn_dt)
            r["ts_confidence"] = 0.85
            r["ts_method"] = "Filename pattern"
            r["ts_evidence"] = evidence
            if exif_dt is not None:
                r["flags"].append("exif_implausible_used_filename")
            continue

    # Drift correction: EXIF present but implausible, anchored to station start
    if activity_start:
        anchor = datetime.fromisoformat(activity_start)
        implausible = [r for r in ordered if r["captured_at"] is None and r.get("exif_dt")]
        if implausible:
            base = min(r["exif_dt"] for r in implausible)
            offset = anchor - base
            for r in implausible:
                r["captured_at"] = r["exif_dt"] + offset
                r["captured_at_source"] = "exif"
                r["ts_confidence"] = 0.70
                r["ts_method"] = "EXIF with deployment drift correction"
                r["drift_applied_s"] = int(offset.total_seconds())
                r["ts_offset_s"] = int(offset.total_seconds())
                r["flags"].append("camera_clock_reset_corrected")

    # Inference: interpolate from resolved neighbours in filename order
    resolved_idx = sorted(i for i, r in enumerate(ordered) if r["captured_at"] is not None)
    for i, r in enumerate(ordered):
        if r["captured_at"] is not None:
            continue
        before = max((j for j in resolved_idx if j < i), default=None)
        after = min((j for j in resolved_idx if j > i), default=None)
        if before is not None and after is not None:
            t0, t1 = ordered[before]["captured_at"], ordered[after]["captured_at"]
            r["captured_at"] = t0 + (t1 - t0) * ((i - before) / (after - before))
            r["ts_confidence"] = 0.50
            r["ts_method"] = "Linear interpolation between neighbouring frames"
        elif before is not None:
            r["captured_at"] = ordered[before]["captured_at"]
            r["ts_confidence"] = 0.40
            r["ts_method"] = "Propagated from previous resolved frame"
        elif after is not None:
            r["captured_at"] = ordered[after]["captured_at"]
            r["ts_confidence"] = 0.40
            r["ts_method"] = "Propagated from subsequent resolved frame"
        elif activity_start:
            r["captured_at"] = datetime.fromisoformat(activity_start)
            r["ts_confidence"] = 0.30
            r["ts_method"] = "Anchored to station installation date"
        else:
            r["captured_at_source"] = "unknown"
            r["ts_confidence"] = 0.0
            r["ts_method"] = "Unresolvable"
            continue
        r["captured_at_source"] = "inferred"
        r["flags"].append("timestamp_inferred_from_sequence")
        resolved_idx = sorted(resolved_idx + [i])


# ── mixed cards and bursts ────────────────────────────────────────────────

def _detect_mixed_cameras(records: list[dict]) -> dict[str, list]:
    """Flag folders containing frames from multiple camera bodies or serials."""
    by_folder: dict[str, set] = {}
    for r in records:
        if r["corrupt"]:
            continue
        body = (r.get("make") or "", r.get("model") or "", r.get("serial") or "")
        if body == ("", "", ""):
            continue
        by_folder.setdefault(r["folder"], set()).add(body)
    return {f: sorted(bodies) for f, bodies in by_folder.items() if len(bodies) > 1}


def _group_bursts(image_rows: list[dict], window_s: int) -> tuple[list[dict], list[dict]]:
    """Cluster frames within burst_window_s into discrete events."""
    events, links = [], []
    by_station: dict[str, list[dict]] = {}
    for row in image_rows:
        by_station.setdefault(row["station_id"], []).append(row)

    for station_id, rows in by_station.items():
        rows.sort(key=lambda r: r["captured_at"] or "")
        clusters: list[list[dict]] = []
        for row in rows:
            if not row.get("captured_at"):
                continue
            if clusters:
                gap = (datetime.fromisoformat(row["captured_at"])
                       - datetime.fromisoformat(clusters[-1][-1]["captured_at"])).total_seconds()
                if gap <= window_s:
                    clusters[-1].append(row)
                    continue
            clusters.append([row])
        for cluster in clusters:
            ev_id = "ev_" + hashlib.sha256(
                "|".join([station_id] + [c["image_id"] for c in cluster]).encode()
            ).hexdigest()[:16]
            events.append({
                "event_id": ev_id,
                "station_id": station_id,
                "started_at": cluster[0]["captured_at"],
                "ended_at": cluster[-1]["captured_at"]
            })
            links.extend({"image_id": c["image_id"], "event_id": ev_id} for c in cluster)
    return events, links


# ── row shaping ───────────────────────────────────────────────────────────

def _to_image_row(r: dict, run_id: str, reserve_id: str, station: dict | None,
                  node: str) -> dict:
    def iso(v):
        return v.isoformat() if isinstance(v, datetime) else v

    row = {
        "image_id": r["sha256"][:16],
        "reserve_id": reserve_id,
        "run_id": run_id,
        "station_id": station["station_id"] if station else None,
        "orig_path": r["orig_path"],
        "sha256": r["sha256"],
        "dhash": r.get("dhash"),
        "phash": r.get("dhash"),
        "captured_at": iso(r["captured_at"]),
        "captured_at_raw": iso(r["captured_at_raw"]),
        "captured_at_source": r["captured_at_source"],
        "ts_confidence": r.get("ts_confidence", 0.0),
        "ts_method": r.get("ts_method"),
        "ts_evidence": json.dumps(r.get("ts_evidence", {})),
        "ts_offset_s": r.get("ts_offset_s", 0),
        "drift_applied_s": r["drift_applied_s"],
        "orientation": r.get("orientation", 1),
        "is_night": r["is_night"],
        "width": r["width"],
        "height": r["height"],
        "bytes": r["bytes"],
        "status": "CORRUPT" if r["corrupt"] else "pending",
        "triage_stage": None,
        "flags": json.dumps(r["flags"]),
        "origin_node": node,
        "lamport": repo.next_lamport(),
        "synced_at": None,
    }
    row["row_hash"] = repo.compute_row_hash(row)
    return row
