"""A synthetic history generator for edge/pipeline/alerts.py.

Builds the minimal database rows generate_for_run() actually reads --
reserve, stations, station_activity, runs, individuals, occupancy rows,
and (only where a scenario needs a specific identity confidence) a real
images -> detections -> flank_crops -> assignments chain. No image files
are written and none are needed: alerts.py never touches a file, only
rows Stage 4 (occupancy) and Stage 3 (assignments) already wrote.

Deliberately separate from tools/seed_demo.py, which drives the full
pipeline end to end with real files to prove the *product* works.
This module exists to prove edge/pipeline/alerts.py in isolation, one
rule and one confound at a time, independent of everything upstream of
it (AUDIT_AND_REVISED_PLAN.md's "the eight scenarios are the spec" --
Prompt 6 of the agent guide: drive them from generated data, not the
demo seed script).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from edge.db import repo

CYCLE_DAYS = 30
BASE = datetime(2026, 1, 1)


def day(n: float) -> str:
    return (BASE + timedelta(days=n)).isoformat()


class History:
    """Incrementally builds one synthetic reserve. Each add_* method
    inserts immediately, in FK-safe order (reserve first, then whatever
    references it) -- there is no separate flush step to forget."""

    def __init__(self, reserve_id: str):
        self.reserve_id = reserve_id
        repo.insert("reserves", dict(
            reserve_id=reserve_id, name="Synthetic scenario reserve", state=None,
            utm_epsg=32643, boundary_geojson=None, created_at=day(0)))
        self._individuals_seen: set[str] = set()
        self.run_ids: dict[int, str] = {}

    def station(self, station_id: str, zone: str, lat: float, lon: float,
                village_dist_km: float, *, active_from_day: float = 0,
                active_to_day: float | None = None) -> None:
        repo.insert("stations", dict(
            station_id=station_id, reserve_id=self.reserve_id, name=station_id,
            lat=lat, lon=lon, zone=zone, village_dist_km=village_dist_km,
            grid_cell=None, folder_hint=None))
        repo.insert("station_activity", dict(
            activity_id=repo.new_id("act_"), station_id=station_id,
            start_date=day(active_from_day),
            end_date=day(active_to_day) if active_to_day is not None else None,
            note="synthetic"))

    def run(self, cycle: int, label: str) -> str:
        run_id = repo.new_id(f"run_{label}_")
        repo.insert("runs", dict(
            run_id=run_id, reserve_id=self.reserve_id, cycle_label=label,
            started_at=day(cycle * CYCLE_DAYS), finished_at=day(cycle * CYCLE_DAYS),
            root_path="synthetic"))
        self.run_ids[cycle] = run_id
        return run_id

    def _individual(self, ind_id: str) -> None:
        if ind_id in self._individuals_seen:
            return
        self._individuals_seen.add(ind_id)
        repo.insert("individuals", dict(
            ind_id=ind_id, reserve_id=self.reserve_id, label=ind_id, provisional=0,
            sex=None, age_class=None, first_seen=None, last_seen=None,
            national_id=None, notes=None))

    def occupancy(self, run_id: str, ind_id: str, station_ids: list[str],
                  event_count: int, centroid_lat: float, centroid_lon: float) -> None:
        self._individual(ind_id)
        repo.insert("occupancy", dict(
            run_id=run_id, ind_id=ind_id, station_set=json.dumps(station_ids),
            hull_wkt=None, centroid_lat=centroid_lat, centroid_lon=centroid_lon,
            area_km2=None, event_count=event_count, effort_days=0.0,
            insufficient_reason=None))

    def identified_capture(self, run_id: str, ind_id: str, station_id: str,
                            confidence: float, captured_at: str | None = None) -> None:
        """A real images -> detections -> flank_crops -> assignments chain
        -- the one thing an occupancy row alone cannot carry: the identity
        confidence behind a capture (repo.mean_assignment_confidence)."""
        self._individual(ind_id)
        image_id = repo.new_id("img_")
        repo.insert("images", dict(
            image_id=image_id, reserve_id=self.reserve_id, run_id=run_id,
            station_id=station_id, orig_path=f"synthetic/{image_id}.jpg",
            sha256=image_id, dhash=None, captured_at=captured_at or day(0),
            captured_at_raw=None, captured_at_source="inferred", drift_applied_s=0,
            is_night=0, width=None, height=None, bytes=None, status="subject",
            triage_stage=None, flags="[]"))
        det_id = repo.new_id("det_")
        repo.insert("detections", dict(
            det_id=det_id, image_id=image_id, model="synthetic", model_version="0",
            label="animal", species="tiger", conf=0.9, x=None, y=None, w=None, h=None))
        crop_id = repo.new_id("crop_")
        repo.insert("flank_crops", dict(
            crop_id=crop_id, det_id=det_id, side="L", rect_ok=1, quality=0.9,
            path=None, embedding=None, embed_model_version=None))
        repo.insert("assignments", dict(
            assign_id=repo.new_id("as_"), crop_id=crop_id, ind_id=ind_id,
            score=confidence, method="embed", decision="auto", confidence=confidence,
            superseded_by=None, decided_at=day(0), actor="synthetic"))
        
