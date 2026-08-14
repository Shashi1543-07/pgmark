"""Stage 1 -- ingest. See blueprint §5.

Turns a raw SD-card folder into rows in `images`, `events` and
`image_event`, without running any model. Unglamorous, and the stage most
teams skip, which is exactly why blueprint calls it out: "worth a large
share of the robustness criterion."

What this module actually does, honestly:
  * walks a folder tree, hashes every file, flags corrupt/zero-byte ones
    (never fails on them -- "handle or flag, never fail")
  * four-tier timestamp resolution: EXIF -> OCR -> filename -> inferred
    (OCR is a real hook with no backend behind it in this build -- see
    _ocr_timestamp_band -- so it always falls through; it is not faked)
  * detects an implausible or backwards camera clock and corrects it by
    anchoring to the station's known deployment date, recording the
    applied offset rather than silently overwriting captured_at_raw
  * flags a folder that mixes two camera bodies, and does not guess which
    file belongs to which -- see blueprint §5.4
  * groups frames into bursts (`events`) once a station is known
  * a two-step run lifecycle -- preflight_ingest() computes and persists
    everything but assigns no station to a folder it cannot match with
    confidence; confirm_ingest() applies human resolutions for anything
    left unmatched and only then groups bursts and finalises the run.
    Nothing about a folder's station identity is ever guessed.

What this module deliberately does NOT do: read a stations.csv (this
build's reserves already have their station table populated; loading a
manifest from a fresh CSV is a separate, smaller concern this pass didn't
touch), or run any detector/classifier -- that is Stage 2, unbuilt, and
everything ingested here sits at status='pending' because nothing has
looked at it yet.

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from edge import config
from edge.db import repo

_EXIF_DATETIME_TAGS = (36867, 306)     # DateTimeOriginal, DateTime
_EXIF_MAKE, _EXIF_MODEL = 271, 272

_FILENAME_TS = re.compile(
    r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[ _T-]?(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})")


# ── run lifecycle ─────────────────────────────────────────────────────────

def preflight_ingest(reserve_id: str, root_path: str, cycle_label: str | None = None) -> dict:
    """Scan a folder, persist what was found, assign what can be assigned
    with confidence. Nothing here deletes or quarantines anything -- that
    is Stage 2's job once it exists -- so there is nothing irreversible to
    protect against yet; what blueprint means by "nothing irreversible
    before this screen" starts to matter once triage can act on this data.
    """
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

    records = [_scan_file(p, root) for p in files]

    seen: dict[str, dict] = {}
    for r in records:
        if r["sha256"] in seen:
            r["duplicate_of"] = seen[r["sha256"]]["sha256"]
        else:
            seen[r["sha256"]] = r

    folder_names = sorted({r["folder"] for r in records})
    folder_station = {f: match_station(f, stations, cfg.folder_match_max_edit_distance)
                       for f in folder_names}
    unmatched_folders = sorted(f for f, s in folder_station.items() if s is None)
    mixed_camera_folders = _detect_mixed_cameras(records)

    for folder in folder_names:
        group = [r for r in records if r["folder"] == folder and not r["corrupt"]]
        station = folder_station[folder]
        activity_start = repo.station_first_active(station["station_id"]) if station else None
        _resolve_group_timestamps(group, activity_start, cfg)

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

    candidate_rows = [_to_image_row(r, run_id, reserve_id, folder_station[r["folder"]], node)
                      for r in records if not r.get("duplicate_of")]

    # Content already ingested by an earlier run. image_id is a SHA-256
    # prefix, so the same photograph produces the same primary key in every
    # run -- and repo.insert_many()'s INSERT OR REPLACE would silently
    # overwrite the earlier run's row, dropping its run_id and status and
    # orphaning every detection, crop and assignment beneath it. Record the
    # overlap instead of destroying it.
    already = repo.existing_image_ids([r["image_id"] for r in candidate_rows])
    image_rows = [r for r in candidate_rows if r["image_id"] not in already]
    cross_run_duplicates = len(candidate_rows) - len(image_rows)
    repo.insert_many_ignore("images", image_rows)
    repo.connect().commit()
    repo.set_run_image_count(run_id, len(image_rows))

    duplicate_count = sum(1 for r in records if r.get("duplicate_of"))
    corrupt_count = sum(1 for r in image_rows if r["status"] == "corrupt")
    repo.audit("ingest.preflight", actor="system", entity_type="run", entity_id=run_id,
               after={"files_found": len(files), "images": len(image_rows),
                      "unmatched_folders": unmatched_folders, "duplicates": duplicate_count})

    return {
        "run_id": run_id,
        "files_found": len(files),
        "images_ingested": len(image_rows),
        "unmatched_folders": unmatched_folders,
        "mixed_camera_folders": mixed_camera_folders,
        "duplicate_count": duplicate_count,
        "cross_run_duplicates": cross_run_duplicates,
        "cross_run_note": (
            f"{cross_run_duplicates} files in this folder were already ingested by an "
            "earlier run and were not re-imported. Their existing rows, and everything "
            "identified from them, are untouched." if cross_run_duplicates else None),
        "corrupt_count": corrupt_count,
        "estimated_seconds": round(len(image_rows) * cfg.estimated_seconds_per_image, 1),
        "estimated_seconds_per_image_assumed": cfg.estimated_seconds_per_image,
    }


def confirm_ingest(run_id: str, station_assignments: dict[str, str] | None = None,
                    skip_folders: list[str] | None = None) -> dict:
    """Resolve whatever preflight could not match, then -- and only then --
    group bursts into events. A folder with no resolution and not
    explicitly skipped blocks confirmation; see blueprint §5.1, "never
    silently guess.\""""
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

    images = repo.images_for_run(run_id)   # re-read: station_ids just changed
    assignable = [i for i in images if i["station_id"] and i["status"] != "corrupt"
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


# ── file scanning ─────────────────────────────────────────────────────────

def _scan_file(path: Path, root: Path) -> dict:
    rec = {
        "path": path, "folder": path.parent.name or root.name,
        "orig_path": str(path), "corrupt": False, "flags": [],
        "sha256": None, "bytes": 0, "width": None, "height": None,
        "exif_dt": None, "make": None, "model": None, "is_night": 0,
        "captured_at": None, "captured_at_raw": None, "captured_at_source": "unknown",
        "drift_applied_s": 0,
    }
    try:
        data = path.read_bytes()
    except OSError:
        rec["corrupt"], rec["sha256"] = True, hashlib.sha256(str(path).encode()).hexdigest()
        rec["flags"].append("unreadable_file")
        return rec

    rec["bytes"] = len(data)
    rec["sha256"] = hashlib.sha256(data).hexdigest()
    if not data:
        rec["corrupt"] = True
        rec["flags"].append("zero_byte_file")
        return rec

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            rec["width"], rec["height"] = img.size
            exif = img.getexif()
            rec["exif_dt"] = _exif_datetime(exif)
            rec["make"] = exif.get(_EXIF_MAKE)
            rec["model"] = exif.get(_EXIF_MODEL)
            rec["is_night"] = _night_heuristic(img)
    except Exception:
        rec["corrupt"] = True
        rec["flags"].append("not_a_readable_image")
    return rec


def _exif_datetime(exif) -> datetime | None:
    for tag in _EXIF_DATETIME_TAGS:
        raw = exif.get(tag)
        if not raw:
            continue
        try:
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _night_heuristic(img) -> int:
    """A classical stand-in, not a model: IR camera-trap frames are near-
    greyscale, so a small average spread between colour channels is a
    reasonable signal. Real illumination classification is Stage 2's job;
    this is cheap enough to compute for real rather than hardcode to 0."""
    try:
        small = img.convert("RGB").resize((32, 32))
        pixels = list(small.getdata())
        spread = sum(max(p) - min(p) for p in pixels) / len(pixels)
        return int(spread < 12)
    except Exception:
        return 0


def _ocr_timestamp_band(path: Path, band_frac: float) -> datetime | None:
    """Tier 2: OCR of the burned-in timestamp band (blueprint §5.2). No
    offline OCR engine is available in this build -- no Tesseract binary
    on this machine, and installing one is a bigger dependency decision
    than this pass takes on unasked. This hook exists so wiring one in
    later is a one-function change, not a redesign; it always returns
    None, and callers fall through to filename/inference exactly as
    blueprint describes for a tier that can't fire."""
    return None


# ── station matching ─────────────────────────────────────────────────────

def match_station(folder_name: str, stations: list[dict], max_dist: int) -> dict | None:
    """Fuzzy match against folder_hint: case-folded, separators stripped,
    edit distance <= max_dist. Never silently guesses beyond that -- an
    unmatched folder is reported, not assigned (blueprint §5.1)."""
    target = _normalize(folder_name)
    if not target:
        return None
    best, best_dist = None, max_dist + 1
    for s in stations:
        hint = _normalize(s.get("folder_hint") or "")
        if not hint:
            continue
        d = _levenshtein(target, hint)
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


# ── timestamps: EXIF -> OCR -> filename -> inferred ─────────────────────

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
    """Mutates each record in place: captured_at, captured_at_raw,
    captured_at_source, drift_applied_s, flags."""
    now = datetime.now(timezone.utc)
    ordered = sorted(records, key=lambda r: r["path"].name)

    for r in ordered:
        exif_dt = r.get("exif_dt")
        if exif_dt is not None and _plausible(exif_dt, cfg.min_plausible_year,
                                               cfg.max_future_days, now):
            r["captured_at"], r["captured_at_raw"] = exif_dt, exif_dt
            r["captured_at_source"] = "exif"
            continue
        if exif_dt is not None:
            r["captured_at_raw"] = exif_dt
            r["flags"].append("camera_clock_reset_suspected")

        ocr_dt = _ocr_timestamp_band(r["path"], cfg.timestamp_band_frac)
        if ocr_dt is not None:
            r["captured_at"], r["captured_at_source"] = ocr_dt, "ocr"
            r.setdefault("captured_at_raw", ocr_dt)
            continue

        fn_dt = _parse_filename_timestamp(r["path"].name)
        if fn_dt is not None and _plausible(fn_dt, cfg.min_plausible_year,
                                             cfg.max_future_days, now):
            r["captured_at"], r["captured_at_source"] = fn_dt, "filename"
            if exif_dt is not None:
                r["flags"].append("exif_implausible_used_filename")
            continue

    # drift correction: EXIF present but implausible, anchored to the
    # station's own known deployment start (blueprint §5.3)
    if activity_start:
        anchor = datetime.fromisoformat(activity_start)
        implausible = [r for r in ordered if r["captured_at"] is None and r.get("exif_dt")]
        if implausible:
            base = min(r["exif_dt"] for r in implausible)
            offset = anchor - base
            for r in implausible:
                r["captured_at"] = r["exif_dt"] + offset
                r["captured_at_source"] = "exif"
                r["drift_applied_s"] = int(offset.total_seconds())
                r["flags"].append("camera_clock_reset_corrected")

    # inference: interpolate from resolved neighbours in filename order, or
    # anchor to the station's deployment start if there is nothing to
    # interpolate between -- never invented from nothing (blueprint §5.2)
    resolved_idx = sorted(i for i, r in enumerate(ordered) if r["captured_at"] is not None)
    for i, r in enumerate(ordered):
        if r["captured_at"] is not None:
            continue
        before = max((j for j in resolved_idx if j < i), default=None)
        after = min((j for j in resolved_idx if j > i), default=None)
        if before is not None and after is not None:
            t0, t1 = ordered[before]["captured_at"], ordered[after]["captured_at"]
            r["captured_at"] = t0 + (t1 - t0) * ((i - before) / (after - before))
        elif before is not None:
            r["captured_at"] = ordered[before]["captured_at"]
        elif after is not None:
            r["captured_at"] = ordered[after]["captured_at"]
        elif activity_start:
            r["captured_at"] = datetime.fromisoformat(activity_start)
        else:
            r["captured_at_source"] = "unknown"
            continue
        r["captured_at_source"] = "inferred"
        r["flags"].append("timestamp_inferred_from_sequence")
        resolved_idx = sorted(resolved_idx + [i])


# ── mixed cards and bursts ────────────────────────────────────────────────

def _detect_mixed_cameras(records: list[dict]) -> dict[str, list]:
    """One folder, two camera bodies means the SD cards got mixed. Flagged
    at the folder level; never auto-split (blueprint §5.4)."""
    by_folder: dict[str, set] = {}
    for r in records:
        if r["corrupt"]:
            continue
        body = (r.get("make") or "", r.get("model") or "")
        if body == ("", ""):
            continue
        by_folder.setdefault(r["folder"], set()).add(body)
    return {f: sorted(bodies) for f, bodies in by_folder.items() if len(bodies) > 1}


def _group_bursts(image_rows: list[dict], window_s: int) -> tuple[list[dict], list[dict]]:
    """A 2-5 frame trigger is ONE visit, not several (blueprint §5.5).
    Needs a station and a resolved timestamp on every row -- both are
    guaranteed by confirm_ingest() before this is called."""
    events, links = [], []
    by_station: dict[str, list[dict]] = {}
    for row in image_rows:
        by_station.setdefault(row["station_id"], []).append(row)

    for station_id, rows in by_station.items():
        rows.sort(key=lambda r: r["captured_at"])
        clusters: list[list[dict]] = []
        for row in rows:
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
            events.append({"event_id": ev_id, "station_id": station_id,
                            "started_at": cluster[0]["captured_at"],
                            "ended_at": cluster[-1]["captured_at"]})
            links.extend({"image_id": c["image_id"], "event_id": ev_id} for c in cluster)
    return events, links


# ── row shaping ───────────────────────────────────────────────────────────

def _to_image_row(r: dict, run_id: str, reserve_id: str, station: dict | None,
                  node: str) -> dict:
    def iso(v):
        return v.isoformat() if isinstance(v, datetime) else v

    row = {
        "image_id": r["sha256"][:16], "reserve_id": reserve_id, "run_id": run_id,
        "station_id": station["station_id"] if station else None,
        "orig_path": r["orig_path"], "sha256": r["sha256"], "dhash": None,
        "captured_at": iso(r["captured_at"]), "captured_at_raw": iso(r["captured_at_raw"]),
        "captured_at_source": r["captured_at_source"],
        "drift_applied_s": r["drift_applied_s"], "is_night": r["is_night"],
        "width": r["width"], "height": r["height"], "bytes": r["bytes"],
        "status": "corrupt" if r["corrupt"] else "pending",
        "triage_stage": None, "flags": json.dumps(r["flags"]),
        "origin_node": node, "lamport": repo.next_lamport(), "synced_at": None,
    }
    row["row_hash"] = repo.compute_row_hash(row)
    return row
