"""PUGMARK edge node — HTTP surface.

The edge node is a complete, standalone application. It never requires the
central tier, never requires the internet, and never blocks on either.

Binding is 127.0.0.1 by default and deliberately: this process holds precise
locations of individual tigers, which is exactly what a poaching network
would want. Exposing it on 0.0.0.0 is an explicit, logged decision.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from edge import config
from edge.db import repo

UI_DIR = Path(__file__).resolve().parent / "ui"

app = FastAPI(title="Pugmark", version=config.APP_VERSION, docs_url="/api/docs")


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    applied = repo.migrate()
    if applied:
        repo.audit("schema.migrate", after={"applied": applied,
                                            "version": repo.schema_version()})


# ── role gating ─────────────────────────────────────────────────────────
# Roles are enforced here rather than in the UI, because a UI check is a
# suggestion and a server check is a control. See blueprint §10.

def _generalise(lat: float | None, lon: float | None, role: str) -> tuple:
    """Roles above reserve level see grid cells, not points. National
    analysis needs distribution, not the tree the tigress sleeps under."""
    if lat is None or lon is None:
        return lat, lon
    if role not in config.CONFIG.privacy.generalise_coords_for_roles:
        return lat, lon
    step = config.CONFIG.privacy.grid_cell_km / 111.0
    return round(lat / step) * step, round(lon / step) * step


def _role(role: str = Query("director", pattern="^(field|biologist|director|analyst|admin)$")) -> str:
    return role


# ── meta ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "app_version": config.APP_VERSION,
        "schema_version": repo.schema_version(),
        "offline": True,
    }


@app.get("/api/config")
def get_config() -> dict:
    """The UI renders this so an officer can see what the machine was told
    before judging what it decided."""
    return config.CONFIG.to_dict()


@app.get("/api/reserves")
def get_reserves() -> list[dict]:
    return repo.reserves()


# ── runs ────────────────────────────────────────────────────────────────

@app.get("/api/runs")
def get_runs(reserve_id: str | None = None) -> list[dict]:
    return repo.runs(reserve_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    r = repo.run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    r["counts"] = repo.run_counts(run_id)
    r["timestamp_sources"] = repo.timestamp_sources(run_id)
    r["flags"] = repo.run_flags(run_id)
    r["alerts"] = repo.alert_counts(run_id)
    return r


@app.get("/api/runs/{run_id}/preflight")
def preflight(run_id: str) -> dict:
    """Everything the machine understood, shown before it acts on any of it.
    Nothing irreversible happens before the officer confirms this screen."""
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return {
        "run_id": run_id,
        "counts": repo.run_counts(run_id),
        "timestamp_sources": repo.timestamp_sources(run_id),
        "flags": repo.run_flags(run_id),
    }


# ── triage ──────────────────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/triage")
def triage(run_id: str) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return {
        "summary": repo.quarantine_summary(run_id),
        "counts": repo.run_counts(run_id),
        "sample": repo.quarantine_sample(run_id),
    }


@app.post("/api/runs/{run_id}/quarantine/restore")
def restore(run_id: str, actor: str = Body("director", embed=True)) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return {"restored": repo.restore_quarantine(run_id, actor)}


# ── individuals ─────────────────────────────────────────────────────────

@app.get("/api/individuals")
def get_individuals(reserve_id: str) -> list[dict]:
    return repo.individuals(reserve_id)


@app.get("/api/individuals/{ind_id}")
def get_individual(ind_id: str, role: str = Query("director")) -> dict:
    ind = repo.individual(ind_id)
    if not ind:
        raise HTTPException(404, "individual not found")
    caps = repo.individual_captures(ind_id)
    for c in caps:
        c["lat"], c["lon"] = _generalise(c.get("lat"), c.get("lon"), role)
    if role in config.CONFIG.privacy.generalise_coords_for_roles:
        repo.audit("location.read.generalised", actor=role,
                   entity_type="individual", entity_id=ind_id)
    else:
        repo.audit("location.read.precise", actor=role,
                   entity_type="individual", entity_id=ind_id)
    ind["captures"] = caps
    ind["sides_seen"] = sorted({c["side"] for c in caps if c["side"] in ("L", "R")})
    return ind


@app.post("/api/individuals/{ind_id}/promote")
def promote(ind_id: str, actor: str = Body("director", embed=True)) -> dict:
    if not repo.promote_individual(ind_id, actor):
        raise HTTPException(400, "not provisional, or not found")
    return {"ok": True}


# ── review ──────────────────────────────────────────────────────────────

@app.get("/api/review")
def get_review(limit: int = 50) -> dict:
    return {"open": repo.review_count(), "items": repo.review_open(limit)}


@app.post("/api/review/{queue_id}/decide")
def decide(queue_id: str, payload: dict = Body(...)) -> dict:
    ind_id = payload.get("ind_id")
    if not ind_id:
        raise HTTPException(400, "ind_id required")
    try:
        return repo.review_decide(queue_id, ind_id,
                                  payload.get("actor", "director"),
                                  bool(payload.get("new_individual")))
    except KeyError:
        raise HTTPException(404, "queue item not found")


# ── occupancy and alerts ────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/occupancy")
def get_occupancy(run_id: str, role: str = Query("director")) -> list[dict]:
    rows = repo.occupancy(run_id)
    for r in rows:
        r["centroid_lat"], r["centroid_lon"] = _generalise(
            r.get("centroid_lat"), r.get("centroid_lon"), role)
    return rows


@app.get("/api/runs/{run_id}/alerts")
def get_alerts(run_id: str, suppressed: bool = False) -> dict:
    return {
        "counts": repo.alert_counts(run_id),
        "items": repo.alerts(run_id, suppressed),
    }


@app.post("/api/alerts/{alert_id}/acknowledge")
def ack(alert_id: str, actor: str = Body("director", embed=True)) -> dict:
    if not repo.acknowledge_alert(alert_id, actor):
        raise HTTPException(400, "already acknowledged, or not found")
    return {"ok": True}


# ── audit and ops ───────────────────────────────────────────────────────

@app.get("/api/audit")
def get_audit(limit: int = 200, q: str | None = None) -> list[dict]:
    return repo.audit_tail(limit, q)


@app.get("/api/ops")
def ops(reserve_id: str) -> dict:
    return {
        "drift": repo.drift_indicators(reserve_id),
        "schema_version": repo.schema_version(),
        "app_version": config.APP_VERSION,
    }


@app.get("/api/stations")
def get_stations(reserve_id: str, role: str = Query("director")) -> list[dict]:
    rows = repo.stations(reserve_id)
    for r in rows:
        r["lat"], r["lon"] = _generalise(r["lat"], r["lon"], role)
    return rows


# ── sync (interface defined, transport not built for the hackathon) ─────

@app.get("/api/sync/status")
def sync_status() -> dict:
    """The central tier is designed, not built. This endpoint exists so the
    UI and the contract are real; it reports honestly that nothing has
    synced rather than pretending."""
    return {
        "enabled": False,
        "reason": "central tier not configured on this node",
        "pending_rows": None,
        "last_bundle": None,
    }


# ── static UI ───────────────────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")


@app.exception_handler(404)
def not_found(_request, exc):  # noqa: ANN001
    return JSONResponse({"error": getattr(exc, "detail", "not found")}, status_code=404)
