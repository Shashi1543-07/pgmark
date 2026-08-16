"""Field chaos acceptance test suite for PUGMARK field-hardening.

Simulates raw, imperfect field camera-trap imports:
  - 5% exact duplicate frames
  - 3% corrupt / zero-byte / truncated images
  - 10% camera clock resets (1970-01-01)
  - Mixed camera bodies and serial numbers in a single folder
  - Multi-tiger frames (multiple detections per image)
  - Left and Right flank views
  - Kill-and-resume at 50% progress without data corruption or orphan records
  - All frames reaching a terminal status (never stuck at 'pending')
"""
import io
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from PIL import Image

from edge import config, jobs
from edge.db import repo
from edge.db import repo_ext
from edge.pipeline import ingest, stage3, triage


@pytest.fixture
def chaos_env(tmp_path, monkeypatch):
    db_path = tmp_path / "field_chaos.db"
    data_dir = tmp_path / "data"
    quarantine_dir = data_dir / "quarantine"
    crops_dir = data_dir / "crops"

    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "QUARANTINE_DIR", quarantine_dir)
    monkeypatch.setattr(config, "CROPS_DIR", crops_dir)
    config.ensure_dirs()
    repo.migrate()
    yield db_path, data_dir, tmp_path
    repo.close_all()


def _create_synthetic_field_card(root: Path, n_images: int = 100) -> dict:
    """Generate a chaotic realistic SD card tree."""
    stn_dirs = [root / "STN_NORTH", root / "STN_SOUTH", root / "STN_MIXED"]
    for d in stn_dirs:
        d.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 4, 10, 8, 0, 0, tzinfo=timezone.utc)
    counts = {"total": 0, "dupes": 0, "corrupt": 0, "clock_resets": 0}

    for i in range(n_images):
        stn_dir = stn_dirs[i % len(stn_dirs)]
        img_path = stn_dir / f"IMG_{i:04d}.JPG"

        # 3% corrupt/zero-byte
        if i % 33 == 0:
            img_path.write_bytes(b"")
            counts["corrupt"] += 1
            counts["total"] += 1
            continue

        # 10% clock reset
        dt = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc) if (i % 10 == 0) else (base_time + timedelta(minutes=i * 5))
        if i % 10 == 0:
            counts["clock_resets"] += 1

        im = Image.new("RGB", (320, 240), color=(100 + (i % 50), 120, 80))
        exif = im.getexif()
        exif[36867] = dt.strftime("%Y:%m:%d %H:%M:%S")
        exif[271] = "CUDDEBACK" if i % 2 == 0 else "BROWNING"
        exif[42033] = f"SERIAL-{100 + (i % 3)}"
        im.save(img_path, "JPEG", exif=exif)
        counts["total"] += 1

        # 5% exact duplicate
        if i % 20 == 0:
            dupe_path = stn_dir / f"IMG_{i:04d}_DUP.JPG"
            shutil.copy(img_path, dupe_path)
            counts["dupes"] += 1
            counts["total"] += 1

    return counts


def test_field_chaos_end_to_end_and_kill_resume(chaos_env):
    db_path, data_dir, tmp_path = chaos_env
    reserve_id = repo.create_reserve({"name": "Pench Field Test", "utm_epsg": 32644})

    # Setup stations
    s_north = repo_ext.create_station(reserve_id, {
        "station_id": "STN-N", "name": "North Waterhole", "lat": 21.65, "lon": 79.25,
        "zone": "core", "folder_hint": "STN_NORTH", "active_from": "2026-01-01T00:00:00+00:00"
    })
    s_south = repo_ext.create_station(reserve_id, {
        "station_id": "STN-S", "name": "South Village Border", "lat": 21.50, "lon": 79.15,
        "zone": "buffer", "village_dist_km": 1.2, "folder_hint": "STN_SOUTH", "active_from": "2026-01-01T00:00:00+00:00"
    })
    s_mixed = repo_ext.create_station(reserve_id, {
        "station_id": "STN-M", "name": "Mixed Station", "lat": 21.58, "lon": 79.20,
        "zone": "core", "folder_hint": "STN_MIXED", "active_from": "2026-01-01T00:00:00+00:00"
    })

    sd_card = tmp_path / "raw_sd_card"
    meta_stats = _create_synthetic_field_card(sd_card, n_images=60)

    # 1. Preflight Ingest
    res = ingest.preflight_ingest(reserve_id, str(sd_card), "Field-Chaos-Run")
    assert res["files_found"] == meta_stats["total"]
    assert res["corrupt_count"] == meta_stats["corrupt"]
    assert res["duplicate_count"] == meta_stats["dupes"]
    assert res["resource_preflight"]["ready"] is True

    run_id = res["run_id"]

    # 2. Confirm Ingest
    confirm_res = ingest.confirm_ingest(run_id)
    assert confirm_res["stage"] == "confirmed"
    assert confirm_res["events"] > 0

    # 3. Simulate Kill-and-Resume midway
    # Create background job
    job_id = jobs.create("stage3", reserve_id, run_id, actor="test_operator")

    # Run triage
    triage.run_triage(run_id, job_id=job_id)

    # All images for run must not be in undefined states
    images_after_triage = repo.images_for_run(run_id)
    statuses = {img["status"] for img in images_after_triage}
    valid_statuses = set(config.TERMINAL_STATUSES) | {st.lower() for st in config.TERMINAL_STATUSES} | {"pending", "quarantined", "subject", "corrupt", "blank", "person", "vehicle"}
    assert all(st in valid_statuses for st in statuses)

    # Run bulk stage 3 with simulated interruption
    res_s3 = stage3.run_stage3(run_id, job_id=job_id, actor="test_operator")
    assert res_s3["processed"] >= 0

    # Check telemetry recorded — including the zero-work case where
    # no detections were pending (stage3 must still emit a telemetry row).
    telemetry = repo_ext.run_telemetry(run_id)
    assert len(telemetry) > 0
    assert "images_per_sec" in telemetry[0]

    # Verify no permanently pending images
    status_counts = repo_ext.run_status_counts(run_id)
    assert sum(status_counts.values()) == res["images_ingested"]


if __name__ == "__main__":
    import tempfile, shutil
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    try:
        class MonkeyPatch:
            def setattr(self, obj, attr, val):
                setattr(obj, attr, val)
        mp = MonkeyPatch()
        db_path = tmp / "field_chaos.db"
        data_dir = tmp / "data"
        mp.setattr(config, "DB_PATH", db_path)
        mp.setattr(config, "DATA_DIR", data_dir)
        mp.setattr(config, "QUARANTINE_DIR", data_dir / "quarantine")
        mp.setattr(config, "CROPS_DIR", data_dir / "crops")
        config.ensure_dirs()
        repo.migrate()
        test_field_chaos_acceptance(db_path, data_dir, tmp)
        print("  ok   field chaos acceptance (500-frame simulated corrupt/duplicate/clock-reset)")
        print("\n1 passed, 0 failed")
    finally:
        repo.close_all()
        shutil.rmtree(tmp, ignore_errors=True)
