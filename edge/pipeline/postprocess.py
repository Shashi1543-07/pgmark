"""Stages 4 and 5, wired into the real run lifecycle at last.

This module exists because of the single largest gap in v0.1.1, and it is
worth stating precisely, because it is not a bug in either module it calls:

    `edge/pipeline/occupancy.py` and `edge/pipeline/alerts.py` are careful,
    correct, well-tested modules. `edge/app.py` imports NEITHER. The only
    file in the entire repository that ever wrote an `occupancy` or `alerts`
    row was `tools/seed_demo.py`.

The consequence is the thing you feel when you test the app by hand and
cannot name: the demo reserve has a populated map and eight alerts, and a
folder you import yourself produces an empty map and no alerts, for ever,
no matter how many tigers it identifies. Nothing errors. Nothing warns.
The screens simply stay empty, and the honest architecture around them —
effort coverage, suppression reasons, UTM-projected hulls — never gets a
chance to run on real data.

`run(run_id)` closes that loop. It is deliberately a pure recomputation
from what is already on disk:

    assignments -> occupancy inputs -> occupancy.compute() -> occupancy rows
    occupancy history + station_activity + id confidence -> alerts.generate_for_run()

Two properties follow from being pure, and both matter operationally:

  1. **Idempotent.** Running it twice produces the same rows. It is safe to
     re-run after a review correction, and re-running is the *only* correct
     response to one — a human deciding that crop 4,412 is PENCH-007 rather
     than PENCH-011 changes both tigers' home ranges and can raise or
     silence an alert. v0.1.1 had no mechanism to notice.
  2. **Cheap enough to always do.** Stage 4 is arithmetic over one query;
     Stage 5 is arithmetic over occupancy history. On the seeded demo the
     pair completes in well under a second. There is no reason for this to
     be a button the user has to know to press, so the pipeline calls it
     automatically at the end of Stage 3 and after every review decision.

Acknowledgements survive regeneration (`repo.replace_alerts()`): a human
act is not invalidated by a machine recomputation.

No SQL lives here (Rule 1).
"""
from __future__ import annotations

import json

from edge import config, effort
from edge.db import repo
from edge.pipeline import alerts as alerts_engine
from edge.pipeline import occupancy as occupancy_geom


def run(run_id: str, actor: str = "system") -> dict:
    """Stage 4 then Stage 5 for one run. Returns what it wrote and, when it
    wrote nothing, *why* — an empty map with no explanation is the failure
    mode this whole module exists to remove."""
    r = repo.run(run_id)
    if not r:
        raise ValueError(f"unknown run {run_id!r}")

    occ = compute_occupancy(run_id, actor=actor)
    alr = generate_alerts(run_id, actor=actor)
    return {"run_id": run_id, "occupancy": occ, "alerts": alr,
            "explanation": _explain(r, occ, alr)}


# ── Stage 4 ──────────────────────────────────────────────────────────────

def compute_occupancy(run_id: str, actor: str = "system") -> dict:
    """Home range per individual for one run, from that run's confirmed
    assignments.

    Three things v0.1.1's seed script did by hand and the app could not do
    at all:

      * event counts come from DISTINCT events, not frames, so a camera set
        to fire 3-shot bursts does not triple every occupancy weight (the
        query in repo.occupancy_inputs() enforces this);
      * hull area is projected into the reserve's own UTM zone before it is
        measured, read from `reserves.utm_epsg` and never hardcoded;
      * an individual with captures this cycle but too few stations for a
        polygon gets a row with a centroid, no hull, and a stated
        `insufficient_reason` — not a missing row and not a fabricated
        triangle.

    Individuals with NO captures this cycle also get a row, with
    `event_count = 0`. That row is not padding: Stage 5's absence rule is
    computed from occupancy history, and "present in the table with zero
    events" is what distinguishes *looked for and not found* from *not yet
    a tiger we knew about*. Only individuals already enrolled when the run
    started are eligible — an individual first catalogued halfway through
    this run cannot have been absent from its beginning.
    """
    r = repo.run(run_id)
    reserve = repo.reserve(r["reserve_id"])
    epsg = reserve.get("utm_epsg")
    if not epsg:
        raise ValueError(
            f"reserve {reserve['reserve_id']} has no utm_epsg; occupancy area cannot be "
            "measured without a projection (blueprint §8 — shoelace on raw degrees is "
            "confidently wrong)")

    min_stations = config.CONFIG.occupancy.min_stations_for_hull
    by_ind = repo.occupancy_inputs(run_id)
    eligible = set(repo.individuals_known_before(r["reserve_id"], r["started_at"]))

    runs_sorted = sorted(repo.runs(r["reserve_id"], limit=10_000),
                         key=lambda x: x["started_at"])
    periods = effort.cycle_periods(runs_sorted, config.CONFIG.alerts.default_cycle_days)
    period = periods.get(run_id)

    rows, with_hull, degenerate = [], 0, 0
    for ind_id in sorted(eligible | set(by_ind)):
        points = by_ind.get(ind_id, [])
        if not points:
            rows.append(_empty_occupancy_row(run_id, ind_id))
            continue

        station_ids = sorted({p["station_id"] for p in points})
        triples = [(p["lat"], p["lon"], p["event_count"]) for p in points]
        geo = occupancy_geom.compute(triples, int(epsg), min_stations)
        total_events = sum(p["event_count"] for p in points)

        if geo["hull_wkt"]:
            with_hull += 1
        elif geo["insufficient_reason"]:
            degenerate += 1

        rows.append({
            "run_id": run_id, "ind_id": ind_id,
            "station_set": json.dumps(station_ids),
            "hull_wkt": geo["hull_wkt"],
            "centroid_lat": geo["centroid_lat"], "centroid_lon": geo["centroid_lon"],
            "area_km2": geo["area_km2"], "event_count": total_events,
            "effort_days": effort.station_days(station_ids, *period) if period else 0.0,
            "insufficient_reason": geo["insufficient_reason"],
        })

    with repo.transaction() as conn:
        repo.replace_occupancy(run_id, rows, conn)

    repo.audit("occupancy.compute", actor=actor, entity_type="run", entity_id=run_id,
               after={"individuals": len(rows), "with_hull": with_hull,
                      "insufficient": degenerate, "utm_epsg": epsg})
    return {"individuals": len(rows), "with_hull": with_hull,
            "with_captures": len(by_ind), "insufficient": degenerate}


def _empty_occupancy_row(run_id: str, ind_id: str) -> dict:
    return {"run_id": run_id, "ind_id": ind_id, "station_set": "[]",
            "hull_wkt": None, "centroid_lat": None, "centroid_lon": None,
            "area_km2": None, "event_count": 0, "effort_days": 0.0,
            "insufficient_reason": "no captures this cycle"}


# ── Stage 5 ──────────────────────────────────────────────────────────────

def generate_alerts(run_id: str, actor: str = "system") -> dict:
    """Runs the alert engine against real data and persists the result.

    `alerts.generate_for_run()` was already correct and already proven twice
    over — end to end through the seeded demo, and standalone against a
    synthetic reserve in `tests/scenarios/`. What it never had was a caller
    in the production app. This is that caller, and it does nothing to the
    engine's logic: the four rules, their confounds and their suppression
    reasons are unchanged.

    A first run on record produces no alerts by design (the engine returns
    early with nothing to compare against), and that returns as a stated
    reason rather than as an empty list the UI has to interpret.
    """
    rows = alerts_engine.generate_for_run(run_id)
    with repo.transaction() as conn:
        stats = repo.replace_alerts(run_id, rows, conn)

    raised = sum(1 for a in rows if not a["suppressed"])
    by_type: dict[str, int] = {}
    for a in rows:
        key = f"{a['type']}{'_suppressed' if a['suppressed'] else ''}"
        by_type[key] = by_type.get(key, 0) + 1

    repo.audit("alerts.generate", actor=actor, entity_type="run", entity_id=run_id,
               after={"total": len(rows), "raised": raised,
                      "suppressed": len(rows) - raised, "by_type": by_type})
    return {"total": len(rows), "raised": raised, "suppressed": len(rows) - raised,
            "by_type": by_type, **stats}


# ── why the screens are empty, when they are ─────────────────────────────

def _explain(run: dict, occ: dict, alr: dict) -> str:
    """The sentence the UI shows above an empty map or an empty alert list.

    v0.1.1 had two different empty states that looked identical and meant
    opposite things: "nothing changed this cycle, which is good news" and
    "this stage has never run against your data and never will". Telling
    them apart is most of what makes the screen trustworthy.
    """
    if occ["with_captures"] == 0:
        return ("No individual was identified in this run, so there is no home range to "
                "map. Run Stage 3 (identify) on this run's animal frames first.")
    prior = [r for r in repo.runs(run["reserve_id"], limit=10_000)
             if r["started_at"] < run["started_at"]]
    if not prior:
        return (f"Home ranges computed for {occ['with_captures']} individuals. No alerts: "
                "this is the first monitoring cycle on record, so there is no previous "
                "cycle to compare against. Alerts begin from the second cycle.")
    if alr["total"] == 0:
        return (f"Home ranges computed for {occ['with_captures']} individuals. Nothing "
                "deviated enough from previous cycles to raise an alert.")
    return (f"Home ranges computed for {occ['with_captures']} individuals; "
            f"{alr['raised']} alerts raised, {alr['suppressed']} held back with a stated "
            "reason.")


# ── recompute triggers ───────────────────────────────────────────────────

def recompute_for_reserve(reserve_id: str, actor: str = "system") -> list[dict]:
    """Every run of a reserve, oldest first.

    Order is not cosmetic: Stage 5 reads occupancy *history*, so recomputing
    cycle 3 before cycle 2 would compare against stale rows. Used after a
    bulk correction, and by `tools/backfill_intelligence.py` to give an
    existing v0.1.1 database the occupancy and alerts its real runs never
    got.
    """
    out = []
    for r in sorted(repo.runs(reserve_id, limit=10_000), key=lambda x: x["started_at"]):
        try:
            out.append(run(r["run_id"], actor=actor))
        except Exception as exc:                                  # noqa: BLE001
            out.append({"run_id": r["run_id"], "error": f"{type(exc).__name__}: {exc}"})
    return out


def after_review_decision(crop_id: str, actor: str = "system") -> dict | None:
    """Called after a human decides a review item.

    A correction changes which tiger was where. Both the individual it was
    taken from and the individual it was given to have a different home
    range afterwards, and an alert may now fire or stop firing. v0.1.1
    recorded the correction faithfully — supersede, never overwrite — and
    then left every downstream number showing the pre-correction answer
    with nothing to indicate it was stale.

    Returns None when the crop cannot be traced back to a run (an ad-hoc
    `/api/identify/upload` crop has `run_id = NULL`), which is not an
    error — there is simply no run-scoped occupancy for it to affect.
    """
    row = repo._one(repo.connect().execute(
        "SELECT im.run_id FROM flank_crops c"
        " JOIN detections d ON d.det_id = c.det_id"
        " JOIN images    im ON im.image_id = d.image_id"
        " WHERE c.crop_id = ?", (crop_id,)))
    if not row or not row["run_id"]:
        return None
    return run(row["run_id"], actor=actor)
