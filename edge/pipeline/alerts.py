"""Stage 5 -- the alert engine. See blueprint §9.

Four rules, each with its confound, computed from data already on disk:
occupancy history (Stage 4's output), station_activity (the effort model,
edge/effort.py), and the identity confidence behind each capture. Nothing
here is hardcoded per individual or per reserve.

tools/seed_demo.py plants eight scenarios -- four that must fire, four
that must be suppressed. They are the specification: this module is
correct when generate_for_run() reproduces all eight from the data those
scenarios plant, not from any alert text written by the seed script.

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import hashlib
import json
import math

from edge import config, effort
from edge.db import repo

_SEVERITY = {
    "buffer_ward": "act",       # precedes conflict; see blueprint §9.2 rule 3
    "centroid_shift": "watch",
    "absence": "watch",
    "new_station": "info",
}


def generate_for_run(run_id: str) -> list[dict]:
    """Every alert -- fired or suppressed -- for one run, derived entirely
    from occupancy history, station activity and assignment confidence."""
    run = repo.run(run_id)
    if not run:
        return []
    reserve_id = run["reserve_id"]

    runs_sorted = sorted(repo.runs(reserve_id, limit=10_000), key=lambda r: r["started_at"])
    idx = next((i for i, r in enumerate(runs_sorted) if r["run_id"] == run_id), None)
    if idx is None or idx == 0:
        return []  # first cycle on record: nothing to compare against
    prior_runs = runs_sorted[:idx]

    periods = effort.cycle_periods(runs_sorted, config.CONFIG.alerts.default_cycle_days)
    current_period = periods[run_id]
    prior_periods = [periods[r["run_id"]] for r in prior_runs]

    stations = {s["station_id"]: s for s in repo.stations(reserve_id)}
    individuals = {i["ind_id"]: i for i in repo.individuals(reserve_id)}
    occ_hist = repo.occupancy_history(reserve_id)

    buffer_ids = [sid for sid, s in stations.items() if s["zone"] == "buffer"]
    buffer_ratio = effort.coverage(buffer_ids, current_period, prior_periods)

    out: list[dict] = []
    for ind_id, by_run in occ_hist.items():
        if ind_id not in individuals:
            continue
        current_row = by_run.get(run_id)
        prior_rows = [by_run[r["run_id"]] for r in prior_runs if r["run_id"] in by_run]
        has_history = bool(prior_rows)

        hist_stations = sorted({sid for row in prior_rows for sid in row["station_set"]})
        cov = effort.coverage(hist_stations, current_period, prior_periods)

        id_conf_now = repo.mean_assignment_confidence(run_id, ind_id)
        last_present = next((r["run_id"] for r in reversed(prior_runs)
                              if by_run.get(r["run_id"], {}).get("event_count", 0) > 0), None)
        id_conf_last = repo.mean_assignment_confidence(last_present, ind_id) if last_present else None

        candidates: list[dict] = []
        if has_history:
            candidates += _centroid_shift(ind_id, current_row, prior_rows, stations, id_conf_now)
            candidates += _new_station(ind_id, current_row, hist_stations, stations,
                                        periods, prior_runs, by_run, id_conf_now)
        candidates += _buffer_ward(ind_id, current_row, hist_stations, stations,
                                    buffer_ratio, id_conf_now)
        candidates += _absence(ind_id, current_row, prior_rows, cov, id_conf_last)

        out += _finalize(run_id, candidates, cov)
    return out


# ── rules ──────────────────────────────────────────────────────────────

def _centroid_shift(ind_id, current_row, prior_rows, stations, id_conf) -> list[dict]:
    """Rule 1. Confound: a centroid from too few captures is noise, not
    movement -- require a minimum event count on both sides (§9.2 rule 1)."""
    if not current_row or current_row["event_count"] <= 0:
        return []
    prev_row = prior_rows[-1]
    if prev_row["event_count"] <= 0:
        return []
    coords = (prev_row["centroid_lat"], prev_row["centroid_lon"],
              current_row["centroid_lat"], current_row["centroid_lon"])
    if any(v is None for v in coords):
        return []

    shift_km = _distance_km(*coords)
    zone = _dominant_zone(prev_row["station_set"], stations)
    threshold = (config.CONFIG.alerts.core_shift_km if zone == "core"
                 else config.CONFIG.alerts.buffer_shift_km)
    if shift_km <= threshold:
        return []

    events_now, events_prior = current_row["event_count"], prev_row["event_count"]
    over = (shift_km - threshold) / max(threshold, 0.1)
    strength = min(0.95, 0.55 + 0.25 * over)
    what = (f"Activity centroid moved {shift_km:.1f} km, past the {threshold:.2f} km "
            f"{zone} threshold. Based on {events_now} events this cycle and "
            f"{events_prior} last cycle.")
    a = _candidate("centroid_shift", ind_id, what,
                    {"shift_km": round(shift_km, 2), "threshold_km": round(threshold, 2),
                     "zone": zone, "events_now": events_now, "events_prior": events_prior},
                    strength, id_conf, key="")

    floor = config.CONFIG.alerts.min_events_for_centroid
    low = min(events_now, events_prior)
    if low < floor:
        a["suppressed"], a["suppress_reason"] = 1, (
            f"Only {low} events this cycle, below the minimum of {floor}. A centroid from "
            "too few captures is noise, not movement.")
    return [a]


def _new_station(ind_id, current_row, hist_stations, stations, periods, prior_runs,
                  by_run, id_conf) -> list[dict]:
    """Rule 2. Confound: a station installed this cycle cannot produce this
    alert -- the tiger did not move, the camera arrived. Suppress unless
    the station was already active, during a cycle the individual was
    itself detected elsewhere (§9.2 rule 2)."""
    if not current_row:
        return []
    hist = set(hist_stations)
    out = []
    for sid in current_row["station_set"]:
        if sid in hist:
            continue
        st = stations.get(sid)
        if not st or st["zone"] == "buffer":
            continue  # buffer/village-adjacent stations are rule 3's alone
        active_prior = [r for r in prior_runs
                        if repo.station_effort_days(sid, *periods[r["run_id"]]) > 0]
        what = (f"First capture at {st['name']} ({st['zone']}), a station this individual "
                f"had not used before.")
        evidence = {"station_id": sid, "station_name": st["name"], "zone": st["zone"],
                    "village_dist_km": st["village_dist_km"],
                    "prior_active_cycles": len(active_prior)}
        a = _candidate("new_station", ind_id, what, evidence, 0.7, id_conf, key=sid)

        if len(active_prior) < config.CONFIG.alerts.new_station_requires_prior_cycles:
            a["suppressed"], a["suppress_reason"] = 1, (
                f"Station {st['name']} was installed this cycle. The tiger did not move; "
                "the camera arrived.")
        elif not any(by_run.get(r["run_id"], {}).get("event_count", 0) > 0 for r in active_prior):
            a["suppressed"], a["suppress_reason"] = 1, (
                "This individual was not detected anywhere while the station was already "
                "active, so its absence from this station cannot be attributed to a range "
                "choice.")
        out.append(a)
    return out


def _buffer_ward(ind_id, current_row, hist_stations, stations, buffer_ratio, id_conf) -> list[dict]:
    """Rule 3. Highest-consequence alert type: precedes conflict, ranked
    'act'. Confound: buffer stations come and go opportunistically, so a
    capture during a buffer-effort spike is weaker evidence (§9.2 rule 3)."""
    if not current_row:
        return []
    hist = set(hist_stations)
    new_buffer = [sid for sid in current_row["station_set"]
                  if sid not in hist and stations.get(sid, {}).get("zone") == "buffer"]
    if not new_buffer:
        return []
    sid = new_buffer[0]
    st = stations[sid]
    strength = 0.85
    if buffer_ratio is not None and buffer_ratio > config.CONFIG.alerts.buffer_effort_spike_ratio:
        strength *= config.CONFIG.alerts.buffer_effort_ratio_damping
    what = (f"First capture at {st['name']} (buffer, {st['village_dist_km']} km from the "
            "nearest village). Not previously seen at a buffer or village-adjacent station.")
    evidence = {"station_id": sid, "station_name": st["name"],
                "village_dist_km": st["village_dist_km"],
                "buffer_effort_ratio_vs_prior": buffer_ratio,
                "additional_new_buffer_stations": new_buffer[1:]}
    return [_candidate("buffer_ward", ind_id, what, evidence, strength, id_conf, key=sid)]


def _absence(ind_id, current_row, prior_rows, cov, id_conf_last) -> list[dict]:
    """Rule 4. Confound, the dangerous one: below the effort-coverage
    floor, do not report absence at all -- say 'I could not see' rather
    than 'it is not there' (§9.2 rule 4)."""
    k = config.CONFIG.alerts.absence_cycles
    if len(prior_rows) < k:
        return []
    if not all(row["event_count"] > 0 for row in prior_rows[-k:]):
        return []
    if current_row and current_row["event_count"] > 0:
        return []

    cov_val = cov if cov is not None else 0.0
    what = f"Not captured this cycle after appearing in each of the previous {k}. "
    what += ("Survey effort in its range was insufficient this cycle."
             if cov_val < config.CONFIG.alerts.absence_min_effort_coverage
             else "Cameras covering its range were active throughout.")
    a = _candidate("absence", ind_id, what,
                    {"prior_cycles_present": k}, 0.9, id_conf_last, key="")
    if cov_val < config.CONFIG.alerts.absence_min_effort_coverage:
        a["suppressed"], a["suppress_reason"] = 1, (
            f"Insufficient survey effort in this individual's range this cycle "
            f"(coverage {cov_val:.2f}). Absence cannot be assessed.")
    return [a]


# ── shared machinery ─────────────────────────────────────────────────────

def _candidate(typ, ind_id, what, evidence, strength, id_conf, *, key) -> dict:
    return {"type": typ, "ind_id": ind_id, "what_changed": what, "evidence": evidence,
            "rule_strength": strength, "id_conf": id_conf, "key": key,
            "suppressed": 0, "suppress_reason": None}


def _finalize(run_id, candidates, cov) -> list[dict]:
    out = []
    cov_factor = 1.0 if cov is None else min(1.0, cov)
    stored_cov = 1.0 if cov is None else round(cov, 3)
    t_low = config.CONFIG.identify.t_low
    for a in candidates:
        id_conf = a["id_conf"]
        conf = round(min(id_conf if id_conf is not None else 1.0, a["rule_strength"])
                     * cov_factor, 3)
        suppressed, reason = a["suppressed"], a["suppress_reason"]
        if not suppressed and id_conf is not None and id_conf < t_low:
            suppressed, reason = 1, (
                f"Identity confidence behind this capture is {id_conf:.2f}, below the "
                f"review threshold of {t_low}. An alert cannot be more confident than the "
                "identification beneath it.")
        out.append({
            "alert_id": _alert_id(run_id, a["ind_id"], a["type"], a["key"]),
            "run_id": run_id, "ind_id": a["ind_id"], "type": a["type"],
            "severity": _SEVERITY[a["type"]], "what_changed": a["what_changed"],
            "evidence": json.dumps(a["evidence"]), "confidence": conf,
            "effort_coverage": stored_cov, "suppressed": suppressed,
            "suppress_reason": reason, "acknowledged_by": None, "acknowledged_at": None,
            "created_at": repo.now(),
        })
    return out


def _dominant_zone(station_ids, stations) -> str:
    zones = [stations[sid]["zone"] for sid in station_ids if sid in stations]
    if not zones:
        return "core"
    return "buffer" if zones.count("buffer") > len(zones) / 2 else "core"


def _distance_km(lat1, lon1, lat2, lon2) -> float:
    """Flat-earth approximation: adequate for a point-to-point distance at
    reserve scale (a few tens of km). NOT adequate for an area -- that
    needs the reserve's UTM projection first (blueprint §8), which Stage 4
    does not yet do in this build (still a bounding-span placeholder; see
    build_occupancy() in tools/seed_demo.py). Do not reuse this helper for
    area math."""
    mean_lat = math.radians((lat1 + lat2) / 2)
    dlat_km = (lat2 - lat1) * 111.0
    dlon_km = (lon2 - lon1) * 111.320 * math.cos(mean_lat)
    return math.hypot(dlat_km, dlon_km)


def _alert_id(*parts) -> str:
    return "al_" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]
