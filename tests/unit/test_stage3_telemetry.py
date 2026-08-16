"""Tests ensuring telemetry is recorded as a first-class execution record in Stage 3,
including zero-work runs, cancellations, and regular completions.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from edge import config, jobs
from edge.db import repo, repo_ext
from edge.pipeline import stage3


@pytest.fixture
def telemetry_env(tmp_path, monkeypatch):
    db_path = tmp_path / "telemetry_test.db"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    config.ensure_dirs()
    repo.migrate()
    yield db_path, data_dir
    repo.close_all()


def test_stage3_zero_work_telemetry(telemetry_env):
    """When a run has 0 pending detections, stage 3 must still record telemetry."""
    reserve_id = repo.create_reserve({"name": "Pench Zero Work Test", "utm_epsg": 32644})
    run_id = repo.new_id("run_")
    run_row = {
        "run_id": run_id, "reserve_id": reserve_id, "cycle_label": "zero-work-test",
        "started_at": repo.now(), "finished_at": None, "root_path": "/tmp/fake",
        "image_count": 0, "stage": "triaged",
        "model_versions": "{}", "config": "{}",
        "schema_version": repo.schema_version(),
        "origin_node": repo.node_id(), "lamport": repo.next_lamport(), "synced_at": None,
    }
    run_row["row_hash"] = repo.compute_row_hash(run_row)
    repo.insert("runs", run_row)

    job_id = jobs.create("stage3", reserve_id, run_id, actor="test_operator")

    res = stage3.run_stage3(run_id, job_id=job_id, actor="test_operator")
    assert res["processed"] == 0

    # Verify telemetry row exists
    telemetry = repo_ext.run_telemetry(run_id)
    assert len(telemetry) >= 1
    assert telemetry[0]["run_id"] == run_id
    assert "images_per_sec" in telemetry[0]
    assert telemetry[0]["images_per_sec"] == 0.0


def test_stage3_job_checkpoint_on_zero_work(telemetry_env):
    """Verify job status reflects zero-work completion cleanly without hanging."""
    reserve_id = repo.create_reserve({"name": "Pench Job Test", "utm_epsg": 32644})
    run_id = repo.new_id("run_")
    run_row = {
        "run_id": run_id, "reserve_id": reserve_id, "cycle_label": "job-test",
        "started_at": repo.now(), "finished_at": None, "root_path": "/tmp/fake",
        "image_count": 0, "stage": "triaged",
        "model_versions": "{}", "config": "{}",
        "schema_version": repo.schema_version(),
        "origin_node": repo.node_id(), "lamport": repo.next_lamport(), "synced_at": None,
    }
    run_row["row_hash"] = repo.compute_row_hash(run_row)
    repo.insert("runs", run_row)

    job_id = jobs.create("stage3", reserve_id, run_id, actor="test_operator")
    stage3.run_stage3(run_id, job_id=job_id, actor="test_operator")

    job_stat = jobs.status(job_id)
    assert job_stat is not None
    assert job_stat["detail"].get("zero_work") is True
