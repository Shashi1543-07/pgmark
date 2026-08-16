"""Tests for fail-safe quarantine manifest persistence and crash recovery."""
from __future__ import annotations

import json
from pathlib import Path
from PIL import Image
import pytest

from edge import config
from edge.db import repo
from edge.pipeline import triage


@pytest.fixture
def quarantine_env(tmp_path, monkeypatch):
    db_path = tmp_path / "quarantine_test.db"
    data_dir = tmp_path / "data"
    quarantine_dir = data_dir / "quarantine"
    crops_dir = data_dir / "crops"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "QUARANTINE_DIR", quarantine_dir)
    monkeypatch.setattr(config, "CROPS_DIR", crops_dir)
    config.ensure_dirs()
    repo.migrate()
    yield db_path, data_dir, quarantine_dir, tmp_path
    repo.close_all()


def test_quarantine_durable_manifest_and_restore(quarantine_env):
    """Proves that manifest is written to disk and restore() can put back all moved files."""
    db_path, data_dir, quarantine_dir, tmp_path = quarantine_env
    reserve_id = repo.create_reserve({"name": "Pench Triage Test", "utm_epsg": 32644})

    # Create a real run row in the DB so the quarantine FK (run_id → runs.run_id) is satisfied
    run_id = repo.new_id("run_")
    run_row = {
        "run_id": run_id,
        "reserve_id": reserve_id,
        "cycle_label": "test-cycle",
        "started_at": repo.now(),
        "finished_at": None,
        "root_path": str(tmp_path / "sd_card"),
        "image_count": 0,
        "stage": "preflight",
        "model_versions": "{}",
        "config": "{}",
        "schema_version": repo.schema_version(),
        "origin_node": repo.node_id(),
        "lamport": repo.next_lamport(),
        "synced_at": None,
    }
    run_row["row_hash"] = repo.compute_row_hash(run_row)
    repo.insert("runs", run_row)

    # Create dummy images on disk
    source_dir = tmp_path / "sd_card" / "STN_A"
    source_dir.mkdir(parents=True, exist_ok=True)

    img_paths = []
    image_ids = []
    for i in range(5):
        p = source_dir / f"FRAME_{i:03d}.JPG"
        im = Image.new("RGB", (64, 64), color=(50, 50, 50))
        im.save(p, "JPEG")
        img_paths.append(p)
        # Derive a stable image_id (matches what the manifest row will use)
        image_id = f"img_{i:04d}"
        image_ids.append(image_id)
        # Insert an image row so the FK quarantine.image_id → images.image_id is satisfied
        img_row = {
            "image_id": image_id,
            "reserve_id": reserve_id,
            "run_id": run_id,
            "station_id": None,
            "orig_path": str(p),
            "sha256": f"deadbeef{i:056x}",
            "dhash": None,
            "captured_at": repo.now(),
            "captured_at_raw": repo.now(),
            "captured_at_source": "unknown",
            "drift_applied_s": 0,
            "is_night": 0,
            "width": 64,
            "height": 64,
            "bytes": p.stat().st_size,
            "status": "pending",
            "triage_stage": None,
            "flags": "[]",
            "origin_node": repo.node_id(),
            "lamport": repo.next_lamport(),
            "synced_at": None,
            "row_hash": None,
        }
        repo.insert("images", img_row)

    run_dir = quarantine_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Manually trigger durable quarantine move for 3 items
    for i, p in enumerate(img_paths[:3]):
        row = {"image_id": f"img_{i:04d}", "orig_path": str(p)}
        triage._quarantine_move(
            run_id, row, "test blank", 1.0, "test@1.0", 0.05, run_dir, "manifest.json")

    # Verify manifest file exists on disk and contains 3 entries
    manifest_file = run_dir / "manifest.json"
    assert manifest_file.exists()
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert len(data) == 3

    # Verify original files were moved
    for p in img_paths[:3]:
        assert not p.exists()

    # Now simulate crash / restore without trusting DB
    triage.restore(run_id, actor="test_operator")

    # Verify all 3 original files are restored to their original paths
    for p in img_paths[:3]:
        assert p.exists()
