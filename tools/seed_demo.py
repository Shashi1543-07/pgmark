"""Seed a demonstration reserve.

This exists so the interface can be built and judged before the CV pipeline
produces anything, and so the alert engine has a fixture to develop against.

It is NOT a substitute for evaluation. Everything here is synthetic and
labelled as such in the UI. The scenarios are chosen deliberately: four
genuine deviations, and four confounds that MUST be suppressed. Those eight
cases are the specification the alert engine is written against.

    python -m tools.seed_demo [--reset]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import config           # noqa: E402
from edge.db import repo          # noqa: E402

RESERVE = "PENCH-MH"
CENTER = (21.6500, 79.3000)
RNG = random.Random(20260817)

CYCLES = [
    ("Phase-IV 2025 Cycle I",  datetime(2025, 12, 1, tzinfo=timezone.utc)),
    ("Phase-IV 2026 Cycle I",  datetime(2026, 3, 1, tzinfo=timezone.utc)),
    ("Phase-IV 2026 Cycle II", datetime(2026, 7, 1, tzinfo=timezone.utc)),
]

NAMES = [
    "Kolitmara North", "Kolitmara South", "Alizanza", "Chorbahuli",
    "Sillari Gate", "Khursapar", "Piparia", "Rukhad Ghat", "Totladoh",
    "Bodhalzira", "Ambakhori", "Jamtara", "Surewani", "Nagalwadi",
    "Deolapar", "Paoni", "Salghat", "Kirangisarra", "Mahuli", "Ghatpendhari",
]


def sid(n: int, zone: str) -> str:
    return f"PN-{'C' if zone == 'core' else 'B'}-{n:03d}"


def hid(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def build_stations() -> list[dict]:
    """A 2 km grid, which is how reserves actually deploy camera traps."""
    out, n = [], 0
    for row in range(6):
        for col in range(6):
            n += 1
            lat = CENTER[0] + (row - 2.5) * 0.018
            lon = CENTER[1] + (col - 2.5) * 0.019
            edge_dist = max(abs(row - 2.5), abs(col - 2.5))
            zone = "core" if edge_dist <= 1.5 else "buffer"
            village_km = round(1.0 + edge_dist * 2.6 + RNG.uniform(-0.3, 0.3), 1)
            out.append({
                "station_id": sid(n, zone),
                "reserve_id": RESERVE,
                "name": NAMES[(n - 1) % len(NAMES)] + ("" if n <= len(NAMES) else f" {n}"),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "zone": zone,
                "village_dist_km": max(0.6, village_km),
                "grid_cell": f"G{row}{col}",
                "folder_hint": f"{NAMES[(n - 1) % len(NAMES)].split()[0].upper()}_{n:03d}",
            })
    return out


def build_activity(stations: list[dict]) -> list[dict]:
    """Camera uptime. This table is the whole reason the alert engine can
    tell 'the tiger is gone' from 'we were not looking'.

    Two deliberate scenarios are planted here:
      * PN-C-015 is installed only in the final cycle  -> its first capture
        of any tiger must NOT raise a new-station alert.
      * PN-C-008 and PN-C-009 die during the final cycle -> the tiger whose
        range they cover must NOT raise an absence alert.
    """
    rows = []
    start_all = CYCLES[0][1] - timedelta(days=30)
    last_start = CYCLES[-1][1]
    for s in stations:
        st = s["station_id"]
        if st == "PN-C-015":
            rows.append({"activity_id": hid("act", st, 1), "station_id": st,
                         "start_date": last_start.isoformat(), "end_date": None,
                         "note": "installed"})
        elif st in ("PN-C-008", "PN-C-009"):
            rows.append({"activity_id": hid("act", st, 1), "station_id": st,
                         "start_date": start_all.isoformat(),
                         "end_date": (last_start + timedelta(days=4)).isoformat(),
                         "note": "battery dead"})
        else:
            rows.append({"activity_id": hid("act", st, 1), "station_id": st,
                         "start_date": start_all.isoformat(), "end_date": None,
                         "note": "installed"})
    return rows


def main(reset: bool = False) -> None:
    config.ensure_dirs()
    if reset and config.DB_PATH.exists():
        repo.close()
        config.DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(config.DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    repo.migrate()
    conn = repo.connect()
    if conn.execute("SELECT COUNT(*) c FROM reserves").fetchone()["c"]:
        print("already seeded; use --reset to rebuild")
        return

    boundary = {
        "type": "Polygon",
        "coordinates": [[[79.20, 21.55], [79.40, 21.55],
                         [79.40, 21.76], [79.20, 21.76], [79.20, 21.55]]],
    }
    repo.insert("reserves", {
        "reserve_id": RESERVE, "name": "Pench Tiger Reserve", "state": "Maharashtra",
        "utm_epsg": 32644, "boundary_geojson": json.dumps(boundary),
        "created_at": repo.now(),
    })

    stations = build_stations()
    repo.insert_many("stations", stations)
    repo.insert_many("station_activity", build_activity(stations))
    core = [s for s in stations if s["zone"] == "core"]
    buffer_ = [s for s in stations if s["zone"] == "buffer"]

    # ── individuals: 11 confirmed, 2 provisional ────────────────────────
    inds = []
    for i in range(1, 12):
        inds.append({
            "ind_id": f"PENCH-{i:03d}", "reserve_id": RESERVE,
            "label": None, "provisional": 0,
            "sex": RNG.choice(["F", "M"]),
            "age_class": RNG.choice(["adult", "adult", "sub-adult"]),
            "first_seen": CYCLES[0][1].isoformat(),
            "last_seen": CYCLES[-1][1].isoformat(), "notes": None,
        })
    for i in (1, 2):
        inds.append({
            "ind_id": f"PENCH-P-{i:03d}", "reserve_id": RESERVE,
            "label": None, "provisional": 1, "sex": None, "age_class": "unknown",
            "first_seen": CYCLES[-1][1].isoformat(),
            "last_seen": CYCLES[-1][1].isoformat(),
            "notes": "auto-enrolled; awaiting human confirmation",
        })
    repo.insert_many("individuals", inds)

    # give every individual a stable home patch of stations
    home: dict[str, list[dict]] = {}
    for n, ind in enumerate(inds):
        base = core[(n * 3) % len(core)]
        near = sorted(core, key=lambda s: (s["lat"] - base["lat"]) ** 2
                      + (s["lon"] - base["lon"]) ** 2)[:4]
        home[ind["ind_id"]] = near

    runs, images, dets, crops, assigns = [], [], [], [], []
    events, image_events, quarantine = [], [], []

    for ci, (label, start) in enumerate(CYCLES):
        run_id = f"run_{ci + 1:02d}"
        final = ci == len(CYCLES) - 1
        runs.append({
            "run_id": run_id, "reserve_id": RESERVE, "cycle_label": label,
            "started_at": start.isoformat(),
            "finished_at": (start + timedelta(hours=1, minutes=12)).isoformat(),
            "root_path": f"E:/CAMERA_TRAP/{start:%Y_%m}/RAW",
            "image_count": 0, "stage": "complete",
            "model_versions": json.dumps({
                "detector": "camtrap-detector-compact@1.0.0",
                "embedder": "stripe-arcface-r50@0.3.1",
                "ocr": "timestamp-band-ocr@0.2.0"}),
            "config": config.CONFIG.to_json(), "schema_version": 1,
        })

        for ind in inds:
            if ind["provisional"] and not final:
                continue
            patch = list(home[ind["ind_id"]])

            # PENCH-004 drifts toward the buffer in the final cycle: a real
            # deviation, and the one that precedes conflict.
            if final and ind["ind_id"] == "PENCH-004":
                patch = patch[:2] + [buffer_[4], buffer_[5]]
            # PENCH-007's cameras died: it must look absent but must NOT be
            # reported absent.
            if final and ind["ind_id"] == "PENCH-007":
                patch = []
            # PENCH-002 is genuinely gone, with full effort coverage.
            if final and ind["ind_id"] == "PENCH-002":
                patch = []
            # PENCH-009 turns up at the newly installed camera: not movement.
            if final and ind["ind_id"] == "PENCH-009":
                patch = patch[:3] + [next(s for s in stations
                                          if s["station_id"] == "PN-C-015")]

            for st in patch:
                for burst in range(RNG.randint(1, 3)):
                    when = start + timedelta(days=RNG.randint(1, 55),
                                             hours=RNG.randint(0, 23))
                    ev_id = hid("ev", run_id, ind["ind_id"], st["station_id"], burst)
                    events.append({"event_id": ev_id, "station_id": st["station_id"],
                                   "started_at": when.isoformat(),
                                   "ended_at": (when + timedelta(seconds=6)).isoformat()})
                    for frame in range(3):     # a real 3-shot burst
                        img_id = hid("im", ev_id, frame)
                        night = RNG.random() < 0.62
                        src = "exif" if RNG.random() > 0.12 else RNG.choice(
                            ["ocr", "filename", "inferred"])
                        flags = []
                        if src == "ocr":
                            flags.append("exif_missing_read_from_timestamp_band")
                        if src == "inferred":
                            flags.append("timestamp_inferred_from_sequence")
                        if RNG.random() < 0.03:
                            flags.append("camera_clock_reset_corrected")
                        images.append({
                            "image_id": img_id, "reserve_id": RESERVE, "run_id": run_id,
                            "station_id": st["station_id"],
                            "orig_path": f"E:/CAMERA_TRAP/{st['folder_hint']}/IMG_{img_id[:6]}.JPG",
                            "sha256": img_id, "dhash": img_id[:12],
                            "captured_at": (when + timedelta(seconds=frame * 2)).isoformat(),
                            "captured_at_raw": (when + timedelta(seconds=frame * 2)).isoformat(),
                            "captured_at_source": src, "drift_applied_s": 0,
                            "is_night": int(night), "width": 4000, "height": 3000,
                            "bytes": RNG.randint(1_800_000, 3_400_000),
                            "status": "subject", "triage_stage": "B",
                            "flags": json.dumps(flags),
                        })
                        image_events.append({"image_id": img_id, "event_id": ev_id})

                        det_id = hid("dt", img_id)
                        dets.append({
                            "det_id": det_id, "image_id": img_id,
                            "model": "camtrap-detector-compact",
                            "model_version": "1.0.0", "label": "animal",
                            "species": "tiger", "conf": round(RNG.uniform(0.78, 0.99), 3),
                            "x": 0.28, "y": 0.34, "w": 0.44, "h": 0.38,
                        })
                        if frame != 1:      # one usable flank crop per burst
                            continue
                        side = RNG.choice(["L", "R"])
                        quality = round(RNG.uniform(0.42, 0.95), 3)
                        crop_id = hid("cr", det_id)
                        crops.append({
                            "crop_id": crop_id, "det_id": det_id, "side": side,
                            "rect_ok": 1, "quality": quality,
                            "path": f"crops/{crop_id}.png", "embedding": None,
                            "embed_model_version": "stripe-arcface-r50@0.3.1",
                        })
                        score = round(RNG.uniform(0.84, 0.97), 3)
                        decision = "auto"
                        if ind["provisional"]:
                            score, decision = round(RNG.uniform(0.30, 0.52), 3), "enrolled"
                        assigns.append({
                            "assign_id": hid("as", crop_id), "crop_id": crop_id,
                            "ind_id": ind["ind_id"], "score": score,
                            "method": "ensemble", "decision": decision,
                            "confidence": score, "superseded_by": None,
                            "decided_at": when.isoformat(), "actor": "system",
                        })

        # blanks: the real ratio is roughly two tigers per thousand frames
        n_subject = sum(1 for i in images if i["run_id"] == run_id)
        n_blank = int(n_subject * 11)
        for b in range(n_blank):
            st = RNG.choice(stations)
            img_id = hid("bl", run_id, b)
            by = RNG.randint(1_400_000, 2_900_000)
            stage = "A" if RNG.random() < 0.55 else "B"
            images.append({
                "image_id": img_id, "reserve_id": RESERVE, "run_id": run_id,
                "station_id": st["station_id"],
                "orig_path": f"E:/CAMERA_TRAP/{st['folder_hint']}/IMG_{img_id[:6]}.JPG",
                "sha256": img_id, "dhash": img_id[:12],
                "captured_at": (start + timedelta(days=RNG.randint(1, 55))).isoformat(),
                "captured_at_raw": None, "captured_at_source": "exif",
                "drift_applied_s": 0, "is_night": int(RNG.random() < 0.6),
                "width": 4000, "height": 3000, "bytes": by,
                "status": "quarantined", "triage_stage": stage, "flags": "[]",
            })
            quarantine.append({
                "q_id": hid("q", img_id), "run_id": run_id, "image_id": img_id,
                "orig_path": f"E:/CAMERA_TRAP/{st['folder_hint']}/IMG_{img_id[:6]}.JPG",
                "quarantine_path": f"quarantine/{run_id}/{img_id}.JPG",
                "reason": "no subject detected" if stage == "B" else "no motion vs station background",
                "conf": round(RNG.uniform(0.90, 0.999), 3),
                "model_version": "camtrap-detector-compact@1.0.0",
                "threshold": config.CONFIG.triage.quarantine_conf_threshold,
                "bytes": by, "restored_at": None,
            })
        # a handful of people, routed away from the wildlife pipeline
        for p in range(6):
            st = RNG.choice(buffer_)
            img_id = hid("pp", run_id, p)
            images.append({
                "image_id": img_id, "reserve_id": RESERVE, "run_id": run_id,
                "station_id": st["station_id"],
                "orig_path": f"E:/CAMERA_TRAP/{st['folder_hint']}/IMG_{img_id[:6]}.JPG",
                "sha256": img_id, "dhash": img_id[:12],
                "captured_at": (start + timedelta(days=RNG.randint(1, 55))).isoformat(),
                "captured_at_raw": None, "captured_at_source": "exif",
                "drift_applied_s": 0, "is_night": 0, "width": 4000, "height": 3000,
                "bytes": RNG.randint(1_400_000, 2_900_000),
                "status": "person", "triage_stage": "B", "flags": "[]",
            })

    for r in runs:
        r["image_count"] = sum(1 for i in images if i["run_id"] == r["run_id"])

    repo.insert_many("runs", runs)
    repo.insert_many("images", images)
    repo.insert_many("events", events)
    repo.insert_many("image_event", image_events)
    repo.insert_many("detections", dets)
    repo.insert_many("flank_crops", crops)
    repo.insert_many("assignments", assigns)
    repo.insert_many("quarantine", quarantine)
    repo.insert_many("persons_restricted", [
        {"image_id": i["image_id"], "blurred_path": f"restricted/{i['image_id']}.jpg",
         "access_count": 0} for i in images if i["status"] == "person"])

    _occupancy_and_alerts(runs, inds, home, stations)
    _review_queue(crops, assigns, inds)

    repo.audit("seed.demo", actor="system", note="synthetic Pench dataset")
    print(f"seeded {len(runs)} runs · {len(images)} images · "
          f"{len(inds)} individuals · {len(stations)} stations")


def _occupancy_and_alerts(runs, inds, home, stations) -> None:
    """Occupancy per run, then the eight alert scenarios.

    Four fire. Four are suppressed. The suppressed ones are the point: they
    are what separates an actionable alert list from a noisy one.
    """
    by_id = {s["station_id"]: s for s in stations}
    occ, alerts = [], []

    for run in runs:
        final = run["run_id"] == runs[-1]["run_id"]
        for ind in inds:
            if ind["provisional"] and not final:
                continue
            patch = list(home[ind["ind_id"]])
            if final and ind["ind_id"] == "PENCH-004":
                patch = patch[:2] + [s for s in stations if s["zone"] == "buffer"][4:6]
            if final and ind["ind_id"] in ("PENCH-002", "PENCH-007"):
                patch = []
            if not patch:
                occ.append({
                    "run_id": run["run_id"], "ind_id": ind["ind_id"],
                    "station_set": "[]", "hull_wkt": None, "centroid_lat": None,
                    "centroid_lon": None, "area_km2": None, "event_count": 0,
                    "effort_days": 0.0,
                    "insufficient_reason": "no captures this cycle",
                })
                continue
            lats = [s["lat"] for s in patch]
            lons = [s["lon"] for s in patch]
            clat, clon = sum(lats) / len(lats), sum(lons) / len(lons)
            span_km = (max(lats) - min(lats)) * 111.0
            span_km2 = (max(lons) - min(lons)) * 111.0 * math.cos(math.radians(clat))
            area = round(max(span_km * span_km2, 4.0), 1)
            occ.append({
                "run_id": run["run_id"], "ind_id": ind["ind_id"],
                "station_set": json.dumps([s["station_id"] for s in patch]),
                "hull_wkt": "POLYGON((" + ", ".join(
                    f"{s['lon']} {s['lat']}" for s in patch + [patch[0]]) + "))",
                "centroid_lat": round(clat, 5), "centroid_lon": round(clon, 5),
                "area_km2": area, "event_count": len(patch) * 2,
                "effort_days": round(len(patch) * 57.0, 1),
                "insufficient_reason": None if len(patch) >= 3 else "fewer than 3 stations",
            })
    repo.insert_many("occupancy", occ)

    rid = runs[-1]["run_id"]
    sill = by_id["PN-B-031"] if "PN-B-031" in by_id else \
        [s for s in stations if s["zone"] == "buffer"][4]

    def alert(ind, typ, sev, what, ev, conf, cov, sup=0, reason=None):
        alerts.append({
            "alert_id": hid("al", rid, ind, typ), "run_id": rid, "ind_id": ind,
            "type": typ, "severity": sev, "what_changed": what,
            "evidence": json.dumps(ev), "confidence": conf, "effort_coverage": cov,
            "suppressed": sup, "suppress_reason": reason,
            "acknowledged_by": None, "acknowledged_at": None,
            "created_at": repo.now(),
        })

    # ── genuine ────────────────────────────────────────────────────────
    alert("PENCH-004", "buffer_ward", "act",
          f"First capture at {sill['name']} (buffer, {sill['village_dist_km']} km from "
          "the nearest village). Core stations only across the previous 4 cycles.",
          {"station_ids": [sill["station_id"]], "dates": ["2026-07-14"],
           "prior_cycles_core_only": 4, "buffer_effort_ratio_vs_prior": 1.05},
          0.86, 0.94)
    alert("PENCH-004", "centroid_shift", "watch",
          "Activity centroid moved 4.1 km south-west, past the 2.36 km core "
          "threshold. Based on 8 events this cycle and 11 last cycle.",
          {"shift_km": 4.1, "threshold_km": 2.36, "events_now": 8, "events_prior": 11},
          0.79, 0.94)
    alert("PENCH-002", "absence", "watch",
          "Not captured this cycle after appearing in each of the previous 3. "
          "Cameras covering its range were active throughout.",
          {"prior_cycles_present": 3, "stations_active": 4, "effort_days": 228.0},
          0.81, 0.97)
    alert("PENCH-011", "new_station", "info",
          "First capture at Khursapar, a station active since 2025 that this "
          "individual had not used before.",
          {"station_ids": ["PN-C-006"], "station_active_since": "2025-11-01"},
          0.68, 0.92)

    # ── suppressed: the whole argument for this system ─────────────────
    alert("PENCH-007", "absence", "watch",
          "Not captured this cycle after appearing in each of the previous 3.",
          {"prior_cycles_present": 3, "stations_dead": ["PN-C-008", "PN-C-009"],
           "effort_days": 8.0},
          0.74, 0.31, sup=1,
          reason="Insufficient survey effort in this individual's range "
                 "(coverage 0.31). Cameras PN-C-008 and PN-C-009 failed on day 4. "
                 "Absence cannot be assessed.")
    alert("PENCH-009", "new_station", "info",
          "First capture at PN-C-015.",
          {"station_ids": ["PN-C-015"], "station_installed": "2026-07-01"},
          0.62, 0.95, sup=1,
          reason="Station PN-C-015 was installed this cycle. The tiger did not "
                 "move; the camera arrived.")
    alert("PENCH-005", "centroid_shift", "info",
          "Activity centroid moved 3.8 km east.",
          {"shift_km": 3.8, "events_now": 2, "min_events": 5},
          0.41, 0.88, sup=1,
          reason="Only 2 events this cycle, below the minimum of 5. A centroid "
                 "from two captures is noise, not movement.")
    alert("PENCH-P-001", "buffer_ward", "watch",
          "Provisional individual captured at two buffer stations.",
          {"station_ids": ["PN-B-028", "PN-B-033"], "id_confidence": 0.44},
          0.44, 0.91, sup=1,
          reason="Identity confidence is 0.44 and still awaiting human review. "
                 "An alert cannot be more confident than the identification "
                 "beneath it.")

    repo.insert_many("alerts", alerts)


def _review_queue(crops, assigns, inds) -> None:
    """Ambiguous matches, ordered so the reviewer's time goes where it
    changes the most: ambiguity multiplied by images affected."""
    confirmed = [i["ind_id"] for i in inds if not i["provisional"]]
    by_crop = {a["crop_id"]: a for a in assigns}
    rows = []
    for crop in crops[:: max(1, len(crops) // 14)][:14]:
        a = by_crop.get(crop["crop_id"])
        if not a:
            continue
        top = round(RNG.uniform(0.58, 0.79), 3)
        second = round(top - RNG.uniform(0.01, 0.09), 3)
        affected = RNG.randint(1, 40)
        cands = [
            {"ind_id": a["ind_id"], "score": top, "evidence": "31 inlier keypoints"},
            {"ind_id": RNG.choice(confirmed), "score": second,
             "evidence": "24 inlier keypoints"},
            {"ind_id": RNG.choice(confirmed), "score": round(second - 0.11, 3),
             "evidence": "12 inlier keypoints"},
        ]
        rows.append({
            "queue_id": hid("rq", crop["crop_id"]), "crop_id": crop["crop_id"],
            "candidates": json.dumps(cands),
            "priority": round((1 - (top - second)) * affected, 2),
            "reason": "ambiguous match" if a["decision"] != "enrolled"
                      else "confirm new individual",
            "state": "open",
        })
    repo.insert_many("review_queue", rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    main(**vars(ap.parse_args()))
