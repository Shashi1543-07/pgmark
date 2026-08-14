"""Seed a demonstration reserve.

This exists so the interface can be built and judged before the CV pipeline
produces anything, and so the alert engine has a fixture to develop against.

It is NOT a substitute for evaluation. Everything here is synthetic and
labelled as such in the UI. The scenarios are chosen deliberately: four
genuine deviations, and four confounds that MUST be suppressed. Those eight
cases are the specification the alert engine is written against -- this
script plants the underlying data (who was where, which cameras were
working), and edge/pipeline/alerts.py + edge/effort.py derive the alerts
from it. No alert text or number is written here.

A handful of individuals' station ranges are hand-assigned (SCENARIO_HOME)
rather than left to the generic nearest-station rule below. Two of them
(PENCH-002, PENCH-007) must behave oppositely -- one keeps full camera
coverage, the other loses its cameras entirely -- and the generic rule
placed both close enough together that their ranges overlapped, which would
have let one scenario corrupt the other. Pinning them to disjoint station
groups is what makes both demonstrable at once.

    python -m tools.seed_demo [--reset]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import config              # noqa: E402
from edge import effort              # noqa: E402
from edge.db import repo             # noqa: E402
from edge.pipeline import alerts     # noqa: E402
from edge.pipeline import occupancy  # noqa: E402

RESERVE = "PENCH-MH"
RESERVE_UTM_EPSG = 32644   # Pench: UTM 44N. Read by build_occupancy(); never hardcoded there.
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

# ── scenario wiring ──────────────────────────────────────────────────────
# Every other individual gets its station range from the generic nearest-
# neighbour rule in build_home(). These do not, because their scenarios
# need specific, mutually non-interfering geometry.
SCENARIO_HOME = {
    "PENCH-002": ["PN-C-026", "PN-C-027", "PN-C-028", "PN-C-029"],  # stays fully covered
    "PENCH-004": ["PN-C-014", "PN-C-016", "PN-C-017", "PN-C-020"],  # drifts to the buffer
    "PENCH-005": ["PN-C-008", "PN-C-011", "PN-C-026", "PN-C-029"],  # spread wide: see FAR_STATION_FOR
    "PENCH-007": ["PN-C-008", "PN-C-009", "PN-C-010", "PN-C-011"],  # cameras die, not the tiger
    "PENCH-009": ["PN-C-020", "PN-C-021", "PN-C-022", "PN-C-023"],  # gets the new camera
    "PENCH-011": ["PN-C-026", "PN-C-027", "PN-C-022", "PN-C-023"],  # finds a genuinely new station
}
NEW_STATION_FOR = {"PENCH-011": "PN-C-016"}   # pre-existing station, never in its own range
FAR_STATION_FOR = {"PENCH-005": "PN-C-029"}   # one corner of its own (wide) home range: a real
                                               # centroid shift with no station new to it, so this
                                               # scenario tests the event-count confound in isolation
INSTALLED_THIS_CYCLE = "PN-C-015"             # PENCH-009's "camera arrived, tiger didn't move"
DEAD_FROM_DAY = 4                             # PENCH-007's cameras fail this far into the cycle

# Single-flank individuals: the field-common case per the ATRW paper
# (docs/DATA.md §1), not the zoo-shot exception -- roughly six of thirteen,
# forced to one side consistently rather than left to an independent coin
# flip per crop, which would make every individual multi-flank by cycle 3
# almost by construction.
SINGLE_FLANK_SIDE = {
    "PENCH-001": "L", "PENCH-003": "R", "PENCH-005": "L",
    "PENCH-007": "R", "PENCH-009": "L", "PENCH-011": "R",
}


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


def build_home(stations: list[dict]) -> dict[str, list[dict]]:
    """Every individual's stable home patch of stations: hand-assigned for
    the scenario individuals, nearest-4-core-stations for everyone else.

    PN-C-015 is excluded from the nearest-neighbour pool. It is only
    installed for the final cycle (INSTALLED_THIS_CYCLE); a station that
    does not exist yet cannot be part of anyone's established range.
    """
    by_id = {s["station_id"]: s for s in stations}
    core = [s for s in stations if s["zone"] == "core" and s["station_id"] != INSTALLED_THIS_CYCLE]
    ids = [f"PENCH-{i:03d}" for i in range(1, 12)] + ["PENCH-P-001", "PENCH-P-002"]
    home: dict[str, list[dict]] = {}
    for n, ind_id in enumerate(ids):
        if ind_id in SCENARIO_HOME:
            home[ind_id] = [by_id[s] for s in SCENARIO_HOME[ind_id]]
            continue
        base = core[(n * 3) % len(core)]
        home[ind_id] = sorted(
            core, key=lambda s: (s["lat"] - base["lat"]) ** 2 + (s["lon"] - base["lon"]) ** 2
        )[:4]
    return home


def build_activity(stations: list[dict], killed_ids: set[str],
                    final_cycle_start: datetime) -> list[dict]:
    """Camera uptime. This table is the whole reason the alert engine can
    tell 'the tiger is gone' from 'we were not looking'.

    Two scenarios live here:
      * INSTALLED_THIS_CYCLE goes live only at the start of the final
        cycle -> a first capture there must NOT read as movement.
      * killed_ids (PENCH-007's entire range) go dark partway through the
        final cycle -> PENCH-007's absence must NOT be reported as such.
    """
    rows = []
    start_all = CYCLES[0][1] - timedelta(days=30)
    for s in stations:
        st = s["station_id"]
        if st == INSTALLED_THIS_CYCLE:
            rows.append({"activity_id": hid("act", st, 1), "station_id": st,
                         "start_date": final_cycle_start.isoformat(), "end_date": None,
                         "note": "installed"})
        elif st in killed_ids:
            rows.append({"activity_id": hid("act", st, 1), "station_id": st,
                         "start_date": start_all.isoformat(),
                         "end_date": (final_cycle_start + timedelta(days=DEAD_FROM_DAY)).isoformat(),
                         "note": "battery dead"})
        else:
            rows.append({"activity_id": hid("act", st, 1), "station_id": st,
                         "start_date": start_all.isoformat(), "end_date": None,
                         "note": "installed"})
    return rows


def patch_for(ind_id: str, home: dict, final: bool,
              by_id: dict[str, dict], buffer_: list[dict]) -> list[dict]:
    """The stations an individual is captured at this cycle. Cycles 1-2 use
    the stable home patch unchanged; the final cycle carries the eight
    scenarios, each a deliberate departure from that patch."""
    patch = list(home[ind_id])
    if not final:
        return patch
    if ind_id == "PENCH-004":                 # drifts toward the buffer: a real
        return patch[:2] + buffer_[4:6]        # deviation, and the one that precedes conflict
    if ind_id in ("PENCH-002", "PENCH-007"):   # one genuinely gone, one cameras-dead
        return []
    if ind_id == "PENCH-009":                  # turns up at the newly installed camera
        return patch[:3] + [by_id[INSTALLED_THIS_CYCLE]]
    if ind_id == "PENCH-011":                  # finds a station that was there all along
        return patch + [by_id[NEW_STATION_FOR["PENCH-011"]]]
    if ind_id == "PENCH-005":                  # a real-looking shift on too little data to trust
        return [by_id[FAR_STATION_FOR["PENCH-005"]]]
    if ind_id == "PENCH-P-001":                # newly enrolled, already in the buffer
        return buffer_[0:2]
    return patch


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
        "utm_epsg": RESERVE_UTM_EPSG, "boundary_geojson": json.dumps(boundary),
        "created_at": repo.now(),
    })

    stations = build_stations()
    by_id = {s["station_id"]: s for s in stations}
    repo.insert_many("stations", stations)

    home = build_home(stations)
    killed_ids = {s["station_id"] for s in home["PENCH-007"]}
    repo.insert_many("station_activity",
                     build_activity(stations, killed_ids, CYCLES[-1][1]))

    core = [s for s in stations if s["zone"] == "core"]
    buffer_ = [s for s in stations if s["zone"] == "buffer"]

    # ── individuals: 11 confirmed, 2 provisional ────────────────────────
    # Field names given by range staff once an individual is confirmed --
    # provisional individuals (below) stay nameless until a human promotes
    # them (repo.promote_individual()), same as PENCH-P-NNN never gets a
    # real ind_id until then.
    NAMES = ["Tara", "Maya", "Durga", "Sher", "Baghin", "Choti",
             "Wagdoh", "Neelam", "Kesar", "Sultana", "Raja"]
    inds = []
    for i in range(1, 12):
        inds.append({
            "ind_id": f"PENCH-{i:03d}", "reserve_id": RESERVE,
            "label": NAMES[i - 1], "provisional": 0,
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

    runs, images, dets, crops, assigns = [], [], [], [], []
    events, image_events, quarantine = [], [], []
    occ_acc: dict[tuple[str, str], Counter] = {}

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
            patch = patch_for(ind["ind_id"], home, final, by_id, buffer_)

            for st in patch:
                # 2-3 bursts per station-visit, never fewer: the scenario
                # individuals need a reliable minimum event count, and a
                # tiger that used a station at all realistically triggers
                # it more than once across a multi-week cycle.
                n_bursts = 2 if (final and ind["ind_id"] == "PENCH-005") else RNG.randint(2, 3)
                for burst in range(n_bursts):
                    when = start + timedelta(days=RNG.randint(1, 55),
                                             hours=RNG.randint(0, 23))
                    ev_id = hid("ev", run_id, ind["ind_id"], st["station_id"], burst)
                    events.append({"event_id": ev_id, "station_id": st["station_id"],
                                   "started_at": when.isoformat(),
                                   "ended_at": (when + timedelta(seconds=6)).isoformat()})
                    occ_acc.setdefault((run_id, ind["ind_id"]), Counter())[st["station_id"]] += 1
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
                        side = SINGLE_FLANK_SIDE.get(ind["ind_id"]) or RNG.choice(["L", "R"])
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
                "threshold": config.CONFIG.triage.detector_conf_threshold if stage == "B"
                            else config.CONFIG.triage.stage_a_blank_threshold,
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

    # Entities (one side of one tiger, blueprint §7.3) derive from
    # assignments, so this only makes sense once assignments exist.
    repo.rebuild_entities(RESERVE)

    periods = effort.cycle_periods(runs, config.CONFIG.alerts.default_cycle_days)
    repo.insert_many("occupancy", build_occupancy(runs, inds, occ_acc, by_id, periods))

    alert_rows = alerts.generate_for_run(runs[-1]["run_id"])
    repo.insert_many("alerts", alert_rows)

    _review_queue(crops, assigns, inds)

    repo.audit("seed.demo", actor="system", note="synthetic Pench dataset")
    print(f"seeded {len(runs)} runs · {len(images)} images · "
          f"{len(inds)} individuals · {len(stations)} stations · "
          f"{len(alert_rows)} alerts ({sum(1 for a in alert_rows if not a['suppressed'])} raised)")


def build_occupancy(runs: list[dict], inds: list[dict], occ_acc: dict, by_id: dict,
                    periods: dict) -> list[dict]:
    """Stage 4, computed from what was actually captured (occ_acc), never
    from a second, independently-imagined patch. That second computation
    is exactly what let PENCH-009's occupancy silently disagree with its
    own capture data in the original version of this script -- the new
    station never showed up as 'visited' because nothing here ever asked
    the capture loop what it had done.

    The hull and its area come from edge/pipeline/occupancy.py: a real
    minimum convex polygon, projected into the reserve's own UTM zone
    before its area is measured -- not the bounding-span approximation
    this used to be. station_set, centroid, event_count and effort_days
    (from station_activity) are unchanged.
    """
    min_hull = config.CONFIG.occupancy.min_stations_for_hull
    out = []
    for run in runs:
        for ind in inds:
            if ind["first_seen"] > run["started_at"]:
                continue  # not enrolled yet: no history, not even an absence
            counter = occ_acc.get((run["run_id"], ind["ind_id"]))
            if not counter:
                out.append({
                    "run_id": run["run_id"], "ind_id": ind["ind_id"],
                    "station_set": "[]", "hull_wkt": None, "centroid_lat": None,
                    "centroid_lon": None, "area_km2": None, "event_count": 0,
                    "effort_days": 0.0, "insufficient_reason": "no captures this cycle",
                })
                continue
            station_ids = sorted(counter)
            total = sum(counter.values())
            station_points = [(by_id[s]["lat"], by_id[s]["lon"], n) for s, n in counter.items()]
            geo = occupancy.compute(station_points, RESERVE_UTM_EPSG, min_hull)
            out.append({
                "run_id": run["run_id"], "ind_id": ind["ind_id"],
                "station_set": json.dumps(station_ids),
                "hull_wkt": geo["hull_wkt"],
                "centroid_lat": geo["centroid_lat"], "centroid_lon": geo["centroid_lon"],
                "area_km2": geo["area_km2"], "event_count": total,
                "effort_days": effort.station_days(station_ids, *periods[run["run_id"]]),
                "insufficient_reason": geo["insufficient_reason"],
            })
    return out


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

