"""Unit tests for Stage 5: 10-type intelligence alert engine and cross-flank association tracking."""
import json
import pytest
from datetime import datetime, timezone

from edge import config
from edge.db import repo
from edge.db import repo_ext
from edge.pipeline import alerts, postprocess


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_stage5.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "QUARANTINE_DIR", tmp_path / "data" / "quarantine")
    monkeypatch.setattr(config, "CROPS_DIR", tmp_path / "data" / "crops")
    config.ensure_dirs()
    repo.migrate()
    yield db_path
    repo.close_all()


def test_cross_flank_candidate_lifecycle(test_env):
    reserve_id = repo.create_reserve({"name": "Pench", "utm_epsg": 32644})
    ind_l = repo.create_individual(reserve_id, "TIG-01-L", actor="field_officer")
    ind_r = repo.create_individual(reserve_id, "TIG-02-R", actor="field_officer")

    # Create candidate with UNKNOWN_RELATIONSHIP
    assoc_id = repo_ext.create_cross_flank_candidate(
        reserve_id=reserve_id,
        l_ind_id=ind_l,
        r_ind_id=ind_r,
        confidence=0.82,
        evidence={"station_id": "PN-01", "time_gap_s": 15})

    candidates = repo_ext.cross_flank_candidates(reserve_id)
    assert len(candidates) == 1
    assert candidates[0]["status"] == "UNKNOWN_RELATIONSHIP"

    # Confirm association (merges ind_r into ind_l)
    res = repo_ext.confirm_cross_flank(assoc_id, primary_ind_id=ind_l, actor="biologist")
    assert res["status"] == "CONFIRMED"

    updated = repo_ext.cross_flank_candidates(reserve_id, status="CONFIRMED")
    assert len(updated) == 1
    assert updated[0]["confirmed_by"] == "biologist"


def test_alert_directional_trend_and_village_distance(test_env):
    reserve_id = repo.create_reserve({"name": "Pench", "utm_epsg": 32644})
    ind_id = repo.create_individual(reserve_id, "T-100", actor="system")

    s1 = repo_ext.create_station(reserve_id, {"station_id": "S1", "lat": 21.60, "lon": 79.20, "zone": "core", "village_dist_km": 6.0})
    s2 = repo_ext.create_station(reserve_id, {"station_id": "S2", "lat": 21.65, "lon": 79.25, "zone": "core", "village_dist_km": 4.0})
    s3 = repo_ext.create_station(reserve_id, {"station_id": "S3", "lat": 21.70, "lon": 79.30, "zone": "buffer", "village_dist_km": 1.5})
    stations = {s["station_id"]: s for s in repo.stations(reserve_id)}

    # Prior cycle 1: at S1 (centroid 21.60, 79.20)
    # Prior cycle 2: at S2 (centroid 21.65, 79.25)
    # Current cycle: at S3 (centroid 21.70, 79.30, village dist 1.5km)
    prior_rows = [
        {"station_set": ["S1"], "centroid_lat": 21.60, "centroid_lon": 79.20, "event_count": 5},
        {"station_set": ["S2"], "centroid_lat": 21.65, "centroid_lon": 79.25, "event_count": 6},
    ]
    cur_row = {"station_set": ["S3"], "centroid_lat": 21.70, "centroid_lon": 79.30, "event_count": 7}

    # Test Rule 5: Directional Trend
    trend_alerts = alerts._directional_trend(ind_id, cur_row, prior_rows, id_conf=0.95)
    assert len(trend_alerts) == 1
    assert trend_alerts[0]["type"] == "directional_trend"
    assert trend_alerts[0]["suppressed"] == 0

    # Test Rule 6: Decreasing Village Distance
    village_alerts = alerts._decreasing_village_distance(ind_id, cur_row, prior_rows, stations, id_conf=0.95)
    assert len(village_alerts) == 1
    assert village_alerts[0]["type"] == "decreasing_village_distance"
    assert village_alerts[0]["severity"] == "act"
    assert village_alerts[0]["evidence"]["min_dist_now_km"] == 1.5


def test_alert_activity_collapse_and_suppression(test_env):
    ind_id = "T-200"
    prior_rows = [
        {"event_count": 12, "centroid_lat": 21.5, "centroid_lon": 79.5},
        {"event_count": 15, "centroid_lat": 21.5, "centroid_lon": 79.5},
        {"event_count": 14, "centroid_lat": 21.5, "centroid_lon": 79.5},
    ]
    # Current cycle collapsed to 2 events with high survey coverage
    cur_row = {"event_count": 2, "centroid_lat": 21.5, "centroid_lon": 79.5}

    a_list = alerts._activity_collapse(ind_id, cur_row, prior_rows, cov=0.90, id_conf=0.95)
    assert len(a_list) == 1
    assert a_list[0]["type"] == "activity_collapse"
    assert a_list[0]["suppressed"] == 0

    # With low survey coverage (0.30) -> should be suppressed
    a_suppressed = alerts._activity_collapse(ind_id, cur_row, prior_rows, cov=0.30, id_conf=0.95)
    assert len(a_suppressed) == 1
    assert a_suppressed[0]["suppressed"] == 1


if __name__ == "__main__":
    import tempfile, shutil
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    try:
        class MonkeyPatch:
            def setattr(self, obj, attr, val):
                setattr(obj, attr, val)
        mp = MonkeyPatch()
        db_path = tmp / "test_stage5.db"
        mp.setattr(config, "DB_PATH", db_path)
        mp.setattr(config, "DATA_DIR", tmp / "data")
        mp.setattr(config, "QUARANTINE_DIR", tmp / "data" / "quarantine")
        mp.setattr(config, "CROPS_DIR", tmp / "data" / "crops")
        config.ensure_dirs()
        repo.migrate()

        test_cross_flank_candidate_lifecycle(db_path)
        print("  ok   cross flank candidate lifecycle")
        test_alert_directional_trend_and_village_distance(db_path)
        print("  ok   alert directional trend and village distance")
        test_alert_activity_collapse_and_suppression(db_path)
        print("  ok   alert activity collapse and suppression")
        print("\n3 passed, 0 failed")
    finally:
        repo.close_all()
        shutil.rmtree(tmp, ignore_errors=True)
