"""Stage 5 -- the 10-type intelligence alert engine. See blueprint §9.

10 rules with effort-aware confounds, computed from data on disk:
occupancy history (Stage 4), station activity (effort model), and
identity confidence.

Alert types:
  1. centroid_shift                (watch)
  2. new_station                   (info)
  3. buffer_ward                   (act)
  4. absence                       (watch)
  5. directional_trend             (watch)
  6. decreasing_village_distance   (act)
  7. activity_collapse             (watch)
  8. new_corridor                  (watch)
  9. travel_time_anomaly           (watch)
 10. identity_confidence_collapse  (watch)

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from edge import config, effort
from edge.db import repo

_SEVERITY = {
    "buffer_ward": "act",
    "decreasing_village_distance": "act",
    "centroid_shift": "watch",
    "absence": "watch",
    "directional_trend": "watch",
    "activity_collapse": "watch",
    "new_corridor": "watch",
    "travel_time_anomaly": "watch",
    "identity_confidence_collapse": "watch",
    "new_station": "info",
}


def generate_for_run(run_id: str) -> list[dict]:
    """Generate all 10 intelligence alert types with structured evidence and effort accounting."""
    run = repo.run(run_id)
    if not run:
        return []
    reserve_id = run["reserve_id"]

    runs_sorted = sorted(repo.runs(reserve_id, limit=10_000), key=lambda r: r["started_at"])
    idx = next((i for i, r in enumerate(runs_sorted) if r["run_id"] == run_id), None)
    if idx is None or idx == 0:
        return []
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
            candidates += _directional_trend(ind_id, current_row, prior_rows, id_conf_now)
            candidates += _decreasing_village_distance(ind_id, current_row, prior_rows, stations, id_conf_now)
            candidates += _activity_collapse(ind_id, current_row, prior_rows, cov, id_conf_now)
            candidates += _new_corridor(ind_id, current_row, hist_stations, stations, id_conf_now)
            candidates += _travel_time_anomaly(ind_id, run_id, stations, id_conf_now)
            candidates += _identity_confidence_collapse(ind_id, id_conf_now, id_conf_last)

        candidates += _buffer_ward(ind_id, current_row, hist_stations, stations,
                                    buffer_ratio, id_conf_now)
        candidates += _absence(ind_id, current_row, prior_rows, cov, id_conf_last)

        out += _finalize(run_id, candidates, cov)
    return out


# ── rules ──────────────────────────────────────────────────────────────

def _centroid_shift(ind_id, current_row, prior_rows, stations, id_conf) -> list[dict]:
    """Rule 1: Centroid Shift."""
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
    """Rule 2: New Station."""
    if not current_row:
        return []
    hist = set(hist_stations)
    out = []
    for sid in current_row["station_set"]:
        if sid in hist:
            continue
        st = stations.get(sid)
        if not st or st["zone"] == "buffer":
            continue
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
    """Rule 3: Buffer Ward (Rank: Act)."""
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
    """Rule 4: Absence with Effort Accounting."""
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


def _directional_trend(ind_id, current_row, prior_rows, id_conf) -> list[dict]:
    """Rule 5: Multi-Cycle Directional Trend."""
    if not current_row or len(prior_rows) < 2:
        return []
    p2, p1 = prior_rows[-2], prior_rows[-1]
    if any(r["centroid_lat"] is None for r in (p2, p1, current_row)):
        return []

    # Bearing 1: p2 -> p1
    b1 = _bearing(p2["centroid_lat"], p2["centroid_lon"], p1["centroid_lat"], p1["centroid_lon"])
    # Bearing 2: p1 -> current
    b2 = _bearing(p1["centroid_lat"], p1["centroid_lon"], current_row["centroid_lat"], current_row["centroid_lon"])

    d1_km = _distance_km(p2["centroid_lat"], p2["centroid_lon"], p1["centroid_lat"], p1["centroid_lon"])
    d2_km = _distance_km(p1["centroid_lat"], p1["centroid_lon"], current_row["centroid_lat"], current_row["centroid_lon"])

    if d1_km < 1.0 or d2_km < 1.0:
        return []

    angle_diff = abs(b1 - b2)
    if angle_diff > 180:
        angle_diff = 360 - angle_diff

    max_angle = config.CONFIG.alerts.directional_trend_max_angle_diff_deg
    what = (f"Consistent directional migration across 3 cycles (bearing {b2:.0f}°, angle drift {angle_diff:.1f}°). "
            f"Total span {d1_km + d2_km:.1f} km.")
    evidence = {"bearing_prev": round(b1, 1), "bearing_cur": round(b2, 1),
                "angle_diff_deg": round(angle_diff, 1), "total_disp_km": round(d1_km + d2_km, 2)}
    a = _candidate("directional_trend", ind_id, what, evidence, 0.8, id_conf, key="")

    if angle_diff > max_angle:
        a["suppressed"], a["suppress_reason"] = 1, (
            f"Movement is non-linear (drift {angle_diff:.1f}° exceeds {max_angle}° tolerance).")
    return [a]


def _decreasing_village_distance(ind_id, current_row, prior_rows, stations, id_conf) -> list[dict]:
    """Rule 6: Decreasing Village Distance (Rank: Act)."""
    if not current_row or not current_row["station_set"] or not prior_rows:
        return []

    cur_dists = [stations[sid]["village_dist_km"] for sid in current_row["station_set"]
                 if sid in stations and stations[sid].get("village_dist_km") is not None]
    if not cur_dists:
        return []
    min_now = min(cur_dists)

    prev_dists = [stations[sid]["village_dist_km"] for sid in prior_rows[-1]["station_set"]
                  if sid in stations and stations[sid].get("village_dist_km") is not None]
    if not prev_dists:
        return []
    min_prev = min(prev_dists)

    warn_dist = config.CONFIG.alerts.village_proximity_warn_km
    if min_now < min_prev and min_now <= warn_dist:
        what = (f"Closest approach to human settlement decreased from {min_prev:.1f} km to {min_now:.1f} km "
                f"(within critical {warn_dist:.1f} km buffer). High conflict risk.")
        evidence = {"min_dist_prev_km": min_prev, "min_dist_now_km": min_now, "warn_dist_km": warn_dist}
        return [_candidate("decreasing_village_distance", ind_id, what, evidence, 0.90, id_conf, key="")]
    return []


def _activity_collapse(ind_id, current_row, prior_rows, cov, id_conf) -> list[dict]:
    """Rule 7: Sudden Activity Collapse."""
    if not current_row or len(prior_rows) < 2:
        return []
    prev_events = [r["event_count"] for r in prior_rows[-3:]]
    avg_prev = sum(prev_events) / len(prev_events)
    if avg_prev < 4.0:
        return []

    cur_events = current_row["event_count"]
    ratio = cur_events / avg_prev
    collapse_thresh = config.CONFIG.alerts.activity_collapse_ratio

    if ratio <= collapse_thresh and cur_events > 0:
        what = (f"Capture frequency collapsed to {cur_events} events ({ratio*100:.0f}% of {avg_prev:.1f} historical baseline).")
        evidence = {"events_cur": cur_events, "events_baseline": round(avg_prev, 1), "ratio": round(ratio, 2)}
        a = _candidate("activity_collapse", ind_id, what, evidence, 0.75, id_conf, key="")
        cov_val = cov if cov is not None else 0.0
        if cov_val < 0.6:
            a["suppressed"], a["suppress_reason"] = 1, (
                f"Camera survey coverage dropped to {cov_val:.2f}. Activity drop may be due to unmonitored stations.")
        return [a]
    return []


def _new_corridor(ind_id, current_row, hist_stations, stations, id_conf) -> list[dict]:
    """Rule 8: New Corridor Utilization."""
    if not current_row:
        return []
    hist = set(hist_stations)
    corridor_stns = [sid for sid in current_row["station_set"]
                     if sid not in hist and stations.get(sid, {}).get("zone") == "corridor"]
    if not corridor_stns:
        return []
    sid = corridor_stns[0]
    st = stations[sid]
    what = f"First observed transit through wildlife corridor station {st['name']} ({sid})."
    evidence = {"station_id": sid, "station_name": st["name"], "zone": "corridor"}
    return [_candidate("new_corridor", ind_id, what, evidence, 0.80, id_conf, key=sid)]


def _travel_time_anomaly(ind_id, run_id, stations, id_conf) -> list[dict]:
    """Rule 9: Travel Time / Speed Anomaly."""
    events = repo.individual_events(run_id, ind_id)

    if len(events) < 2:
        return []

    max_kmh = config.CONFIG.alerts.travel_speed_max_kmh
    for i in range(len(events) - 1):
        e1, e2 = events[i], events[i + 1]
        s1, s2 = stations.get(e1["station_id"]), stations.get(e2["station_id"])
        if not s1 or not s2 or e1["station_id"] == e2["station_id"]:
            continue
        dist_km = _distance_km(s1["lat"], s1["lon"], s2["lat"], s2["lon"])
        t1 = datetime.fromisoformat(e1["ended_at"] or e1["started_at"])
        t2 = datetime.fromisoformat(e2["started_at"])
        dt_hours = max(0.01, (t2 - t1).total_seconds() / 3600.0)
        speed_kmh = dist_km / dt_hours

        if speed_kmh > max_kmh and dist_km > 3.0:
            what = (f"Implied transit speed of {speed_kmh:.1f} km/h between {s1['name']} and {s2['name']} "
                    f"({dist_km:.1f} km in {dt_hours:.1f}h) exceeds biological ceiling of {max_kmh} km/h.")
            evidence = {"station_from": s1["station_id"], "station_to": s2["station_id"],
                        "distance_km": round(dist_km, 2), "hours": round(dt_hours, 2),
                        "speed_kmh": round(speed_kmh, 1)}
            return [_candidate("travel_time_anomaly", ind_id, what, evidence, 0.85, id_conf, key=f"{s1['station_id']}_{s2['station_id']}")]
    return []


def _identity_confidence_collapse(ind_id, id_conf_now, id_conf_last) -> list[dict]:
    """Rule 10: Identity Match Confidence Collapse."""
    if id_conf_now is None or id_conf_last is None:
        return []
    drop = id_conf_last - id_conf_now
    drop_threshold = config.CONFIG.alerts.id_confidence_collapse_drop

    if drop >= drop_threshold and id_conf_last >= 0.80:
        what = (f"Identification matching confidence collapsed by {drop:.2f} (from {id_conf_last:.2f} down to {id_conf_now:.2f}). "
                "Possible flank injury, heavy scar tissue, or misassignment.")
        evidence = {"conf_prev": round(id_conf_last, 3), "conf_now": round(id_conf_now, 3),
                    "confidence_drop": round(drop, 3)}
        return [_candidate("identity_confidence_collapse", ind_id, what, evidence, 0.70, id_conf_now, key="")]
    return []


# ── shared machinery ─────────────────────────────────────────────────────

def _candidate(typ, ind_id, what, evidence, strength, id_conf, *, key) -> dict:
    return {"type": typ, "severity": _SEVERITY.get(typ, "watch"), "ind_id": ind_id, "what_changed": what, "evidence": evidence,
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
    mean_lat = math.radians((lat1 + lat2) / 2)
    dlat_km = (lat2 - lat1) * 111.0
    dlon_km = (lon2 - lon1) * 111.320 * math.cos(mean_lat)
    return math.hypot(dlat_km, dlon_km)


def _bearing(lat1, lon1, lat2, lon2) -> float:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) -
         math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1)))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _alert_id(*parts) -> str:
    return "al_" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]
