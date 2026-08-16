"""A large, synthetic reserve for VISUAL evaluation at production scale --
what does the map look like with 150 tigers on it, does the catalogue
grid hold up, does a review queue in the hundreds feel different. This is
NOT tools/seed_demo.py's job and does not touch it: seed_demo's eight
planted scenarios ARE the alert-engine specification (CLAUDE.md is
explicit about this) and must stay exactly as they are. This script has
no scenarios to prove, only volume -- individuals drift, go quiet, or
turn up somewhere new by ordinary randomness, and whatever the real alert
engine makes of that is what you see. Nothing here is hand-tuned to fire.

Every row is the same shape a real run produces (images, detections,
flank_crops, assignments), and occupancy + alerts are computed by the
REAL pipeline (edge/pipeline/postprocess.py) once per cycle -- not
hand-faked geometry -- so what's on screen afterward is exactly what the
app computes from this data, not a mockup of it.

    python -m tools.seed_bulk              # wipe and build a big reserve
    python -m tools.reset_blank            # back to an empty reserve
    python -m tools.seed_demo --reset      # back to the small, spec demo

All three are destructive: each replaces whatever is currently in
data/pugmark.db. There is no undo but re-running one of the other two.
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import config                      # noqa: E402
from edge.db import repo                     # noqa: E402
from edge.pipeline import postprocess        # noqa: E402
from tools.seed_demo import _capture_existing_accounts  # noqa: E402

RESERVE = "PENCH-MH"
RESERVE_UTM_EPSG = 32644
CENTER = (21.6500, 79.3000)
RNG = random.Random(20260815)

GRID_SIDE = 9                      # 81 stations -- dense enough to be a real map
N_INDIVIDUALS = 150
N_CYCLES = 6
CYCLE_SPACING_DAYS = 46
PROVISIONAL_FRACTION = 0.12

NAME_SYLLABLES_A = ["Tara", "Maya", "Durga", "Sher", "Baghin", "Choti", "Wagdoh",
                     "Neelam", "Kesar", "Sultana", "Raja", "Kajri", "Munna", "Simba",
                     "Chhoti", "Gauri", "Bijli", "Rani", "Sultan", "Bahadur", "Kanha",
                     "Savitri", "Jamuna", "Pardhi", "Tendua", "Chandi", "Bela", "Naina",
                     "Shakti", "Veera"]


def sid(n: int, zone: str) -> str:
    return f"PN-{'C' if zone == 'core' else 'B'}-{n:03d}"


def hid(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


CAMERA_MAKES = [
    ("Reconyx", "HyperFire 2", "HF2"),
    ("Cuddeback", "Color X-Change", "CXC"),
    ("Bushnell", "Trophy Cam HD", "BTC"),
    ("Browning", "Strike Force Pro", "BSF"),
    ("Spypoint", "Force-Pro", "SPF"),
]


def build_stations() -> list[dict]:
    out, n = [], 0
    half = (GRID_SIDE - 1) / 2
    for row in range(GRID_SIDE):
        for col in range(GRID_SIDE):
            n += 1
            lat = CENTER[0] + (row - half) * 0.014
            lon = CENTER[1] + (col - half) * 0.0148
            edge_dist = max(abs(row - half), abs(col - half))
            zone = "core" if edge_dist <= half * 0.55 else "buffer"
            village_km = round(1.0 + edge_dist * 2.1 + RNG.uniform(-0.3, 0.3), 1)
            make, model, prefix = RNG.choice(CAMERA_MAKES)
            serial = f"{prefix}-{RNG.randint(10000, 99999)}"
            out.append({
                "station_id": sid(n, zone), "reserve_id": RESERVE,
                "name": f"Station {n:03d} ({'Core' if zone == 'core' else 'Buffer'})",
                "lat": round(lat, 5), "lon": round(lon, 5),
                "zone": zone, "village_dist_km": max(0.6, village_km),
                "grid_cell": f"G{row}{col}", "folder_hint": f"CAM_{n:03d}",
                "camera_make": make, "camera_model": model, "camera_serial": serial,
                "active_from": "2025-01-01T00:00:00Z", "status": "active",
            })
    return out


def build_names(n: int) -> list[str]:
    names, i = [], 0
    while len(names) < n:
        base = NAME_SYLLABLES_A[i % len(NAME_SYLLABLES_A)]
        suffix = "" if i < len(NAME_SYLLABLES_A) else f" {i // len(NAME_SYLLABLES_A) + 1}"
        names.append(base + suffix)
        i += 1
    return names


def main() -> None:
    config.ensure_dirs()
    existing_users = []
    existing_sessions = []
    if config.DB_PATH.exists():
        # Same guarantee as tools/seed_demo.py's --reset: never silently
        # discard real accounts because this read raced with something else
        # holding the database open. See _capture_existing_accounts()'s own
        # docstring for the incident that made this non-optional.
        existing_users, existing_sessions = _capture_existing_accounts()
        repo.close_all()
        try:
            config.DB_PATH.unlink()
            for suffix in ("-wal", "-shm"):
                p = Path(str(config.DB_PATH) + suffix)
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
        except OSError:
            conn = repo.connect()
            conn.execute("PRAGMA foreign_keys = OFF")
            tables = [r["name"] for r in repo._rows(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))]
            for t in tables:
                if t != "schema_migrations":
                    try:
                        conn.execute(f"DELETE FROM {t}")
                    except Exception:
                        pass
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()
    repo.migrate()

    boundary = {
        "type": "Polygon",
        "coordinates": [[[79.16, 21.52], [79.44, 21.52],
                         [79.44, 21.78], [79.16, 21.78], [79.16, 21.52]]],
    }
    core_boundary = {
        "type": "Polygon",
        "coordinates": [[[79.23, 21.58], [79.37, 21.58],
                         [79.37, 21.72], [79.23, 21.72], [79.23, 21.58]]],
    }
    buffer_boundary = {
        "type": "Polygon",
        "coordinates": [[[79.16, 21.52], [79.44, 21.52],
                         [79.44, 21.78], [79.16, 21.78], [79.16, 21.52]]],
    }
    corridor_boundary = {
        "type": "LineString",
        "coordinates": [[79.30, 21.78], [79.34, 21.84], [79.42, 21.90]],
    }
    res_boundaries = {
        "type": "FeatureCollection",
        "core_geojson": core_boundary,
        "buffer_geojson": buffer_boundary,
        "corridor_geojson": corridor_boundary,
    }
    repo.insert("reserves", {
        "reserve_id": RESERVE, "name": "Pench Tiger Reserve", "state": "Maharashtra",
        "utm_epsg": RESERVE_UTM_EPSG, "boundary_geojson": json.dumps(res_boundaries),
        "created_at": repo.now(),
    })

    stations = build_stations()
    dead_stations = set(RNG.sample([s["station_id"] for s in stations], k=4))
    new_station = RNG.choice([s["station_id"] for s in stations if s["station_id"] not in dead_stations])
    for s in stations:
        if s["station_id"] in dead_stations:
            s["status"] = "offline"

    repo.insert_many("stations", stations)
    core = [s for s in stations if s["zone"] == "core"]

    start_all = datetime.now(timezone.utc) - timedelta(days=CYCLE_SPACING_DAYS * N_CYCLES + 40)
    last_cycle_start = datetime.now(timezone.utc) - timedelta(days=CYCLE_SPACING_DAYS)
    activity = []
    for s in stations:
        if s["station_id"] == new_station:
            activity.append({"activity_id": hid("act", s["station_id"]), "station_id": s["station_id"],
                              "start_date": last_cycle_start.isoformat(), "end_date": None,
                              "note": "installed"})
        elif s["station_id"] in dead_stations:
            died_at = last_cycle_start + timedelta(days=RNG.randint(3, 10))
            activity.append({"activity_id": hid("act", s["station_id"]), "station_id": s["station_id"],
                              "start_date": start_all.isoformat(), "end_date": died_at.isoformat(),
                              "note": "battery dead"})
        else:
            activity.append({"activity_id": hid("act", s["station_id"]), "station_id": s["station_id"],
                              "start_date": start_all.isoformat(), "end_date": None, "note": "installed"})
    repo.insert_many("station_activity", activity)

    # ── individuals, each with a stable home cluster of nearby stations ──
    names = build_names(N_INDIVIDUALS)
    inds, homes, first_seen_cycle = [], {}, {}
    cycles = [(f"Cycle {i + 1}", start_all + timedelta(days=40 + i * CYCLE_SPACING_DAYS))
              for i in range(N_CYCLES)]
    for i in range(N_INDIVIDUALS):
        provisional = RNG.random() < PROVISIONAL_FRACTION
        fsc = 0 if not provisional else RNG.randint(0, N_CYCLES - 1)
        ind_id = f"PENCH-{i + 1:03d}" if not provisional else f"PENCH-P-{i + 1:03d}"
        first_seen_cycle[ind_id] = fsc
        inds.append({
            "ind_id": ind_id, "reserve_id": RESERVE,
            "label": names[i] if not provisional else None, "provisional": int(provisional),
            "sex": RNG.choice(["F", "M"]), "age_class": RNG.choice(["adult", "adult", "sub-adult"]),
            "first_seen": cycles[fsc][1].isoformat(),
            "last_seen": cycles[-1][1].isoformat(), "notes": None,
        })
        anchor = RNG.choice(core)
        homes[ind_id] = sorted(
            core, key=lambda s: (s["lat"] - anchor["lat"]) ** 2 + (s["lon"] - anchor["lon"]) ** 2
        )[:RNG.randint(4, 7)]
    repo.insert_many("individuals", inds)

    # ── captures, cycle by cycle, real pipeline postprocess after each ───
    runs, images, dets, crops, assigns, events, image_events, quarantine = [], [], [], [], [], [], [], []
    for ci, (label, start) in enumerate(cycles):
        run_id = f"run_bulk_{ci + 1:02d}"
        runs.append({
            "run_id": run_id, "reserve_id": RESERVE, "cycle_label": label,
            "started_at": start.isoformat(),
            "finished_at": (start + timedelta(hours=1, minutes=5)).isoformat(),
            "root_path": f"E:/CAMERA_TRAP/{start:%Y_%m}/RAW",
            "image_count": 0, "stage": "complete",
            "model_versions": json.dumps({"detector": "MDV6-mit-yolov9-c@1.0.0"}),
            "config": config.CONFIG.to_json(), "schema_version": repo.schema_version(),
        })
        cycle_images = []
        for ind in inds:
            if ind["first_seen"] > start.isoformat():
                continue
            if RNG.random() < 0.12:      # absent this cycle -- real absence signal
                continue
            patch = list(homes[ind["ind_id"]])
            if RNG.random() < 0.15:      # drifts: pick up one unfamiliar station
                patch = patch[:-1] + [RNG.choice(core)]
            n_visits = RNG.randint(1, 3)
            for v in range(n_visits):
                st = RNG.choice(patch)
                if st["station_id"] in dead_stations and start >= last_cycle_start:
                    continue
                n_bursts = RNG.randint(1, 3)
                for burst in range(n_bursts):
                    when = start + timedelta(days=RNG.randint(1, CYCLE_SPACING_DAYS - 2),
                                             hours=RNG.randint(0, 23))
                    ev_id = hid("ev", run_id, ind["ind_id"], st["station_id"], v, burst)
                    events.append({"event_id": ev_id, "station_id": st["station_id"],
                                   "started_at": when.isoformat(),
                                   "ended_at": (when + timedelta(seconds=6)).isoformat()})
                    for frame in range(3):
                        img_id = hid("im", ev_id, frame)
                        det_id = hid("dt", img_id)
                        row = {
                            "image_id": img_id, "reserve_id": RESERVE, "run_id": run_id,
                            "station_id": st["station_id"],
                            "orig_path": f"E:/CAMERA_TRAP/{st['folder_hint']}/IMG_{img_id[:6]}.JPG",
                            "sha256": img_id, "dhash": img_id[:12],
                            "captured_at": (when + timedelta(seconds=frame * 2)).isoformat(),
                            "captured_at_raw": None, "captured_at_source": "exif",
                            "drift_applied_s": 0, "is_night": int(RNG.random() < 0.6),
                            "width": 4000, "height": 3000, "bytes": RNG.randint(1_800_000, 3_400_000),
                            "status": "subject", "triage_stage": "B", "flags": "[]",
                        }
                        images.append(row)
                        cycle_images.append(row)
                        image_events.append({"image_id": img_id, "event_id": ev_id})
                        dets.append({
                            "det_id": det_id, "image_id": img_id, "model": "MDV6-mit-yolov9-c",
                            "model_version": "1.0.0", "label": "animal", "species": "tiger",
                            "conf": round(RNG.uniform(0.75, 0.99), 3),
                            "x": 0.27, "y": 0.33, "w": 0.45, "h": 0.4,
                        })
                        if frame != 1:
                            continue
                        side = RNG.choice(["L", "R"])
                        crop_id = hid("cr", det_id)
                        crops.append({
                            "crop_id": crop_id, "det_id": det_id, "side": side, "rect_ok": 1,
                            "quality": round(RNG.uniform(0.5, 0.95), 3),
                            "path": None, "embedding": None,
                            "embed_model_version": "trihard-resnet50@1.0.0",
                        })
                        score = round(RNG.uniform(0.85, 0.98), 3)
                        decision = "enrolled" if ind["provisional"] and ci == first_seen_cycle[ind["ind_id"]] else "auto"
                        assigns.append({
                            "assign_id": hid("as", crop_id), "crop_id": crop_id,
                            "ind_id": ind["ind_id"], "score": score, "method": "ensemble",
                            "decision": decision, "confidence": score, "superseded_by": None,
                            "decided_at": when.isoformat(), "actor": "system",
                        })
        # ── Blank / Quarantined frames for triage evaluation ────────────────
        BLANK_REASONS = [
            ("wind_blown_foliage", 0.94),
            ("empty_grassland", 0.98),
            ("shadow_movement", 0.88),
            ("lens_flare_glare", 0.91),
            ("night_thermal_false_trigger", 0.85),
            ("motion_blur_distant", 0.76),
            ("partial_grass_obstruction", 0.68),
            ("falling_leaf_trigger", 0.82),
        ]
        for bi in range(RNG.randint(45, 65)):
            st = RNG.choice(stations)
            img_id = hid("bk", run_id, bi)
            reason, base_conf = RNG.choice(BLANK_REASONS)
            conf = round(max(0.60, min(0.99, base_conf + RNG.uniform(-0.06, 0.05))), 3)
            when = start + timedelta(days=RNG.randint(1, CYCLE_SPACING_DAYS - 2), hours=RNG.randint(0, 23))
            row_bytes = RNG.randint(1_500_000, 2_800_000)
            row = {
                "image_id": img_id, "reserve_id": RESERVE, "run_id": run_id,
                "station_id": st["station_id"],
                "orig_path": f"E:/CAMERA_TRAP/{st['folder_hint']}/BLANK_{img_id[:6]}.JPG",
                "sha256": img_id, "dhash": img_id[:12],
                "captured_at": when.isoformat(),
                "captured_at_raw": None, "captured_at_source": "exif",
                "drift_applied_s": 0, "is_night": int(RNG.random() < 0.5),
                "width": 4000, "height": 3000, "bytes": row_bytes,
                "status": "quarantined", "triage_stage": "A" if conf > 0.85 else "B", "flags": "[]",
            }
            images.append(row)
            cycle_images.append(row)
            quarantine.append({
                "q_id": hid("q", img_id),
                "run_id": run_id,
                "image_id": img_id,
                "orig_path": row["orig_path"],
                "quarantine_path": f"quarantine/{run_id}/{img_id}.JPG",
                "reason": reason.replace("_", " "),
                "conf": conf,
                "model_version": "camtrap-detector-compact@1.0.0",
                "threshold": 0.85,
                "bytes": row_bytes,
                "restored_at": None,
            })
        runs[ci]["image_count"] = len(cycle_images)

    repo.insert_many("runs", runs)
    repo.insert_many("images", images)
    repo.insert_many("quarantine", quarantine)
    repo.insert_many("events", events)
    repo.insert_many("image_event", image_events)
    repo.insert_many("detections", dets)
    repo.insert_many("flank_crops", crops)
    repo.insert_many("assignments", assigns)
    repo.rebuild_entities(RESERVE)

    if existing_users:
        for u in existing_users:
            repo.insert("users", dict(u))
        for s in existing_sessions:
            repo.insert("sessions", dict(s))
    else:
        adm = repo.ensure_admin()
        if adm["created"]:
            print(f"admin account created — temp password: {adm['temp_password']} · recovery code: {adm['recovery_code']}")

    total_alerts = 0
    for run in runs:
        result = postprocess.run(run["run_id"], actor="system")
        total_alerts += result["alerts"]["total"]

    repo.audit("seed.bulk", actor="system", note="large synthetic reserve for visual evaluation")
    print(f"seeded {len(runs)} runs (bulk) · {len(images)} images · "
          f"{len(inds)} individuals · {len(stations)} stations · {total_alerts} alerts across all cycles")


if __name__ == "__main__":
    main()
