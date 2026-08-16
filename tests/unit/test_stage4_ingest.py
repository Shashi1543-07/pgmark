"""Unit tests for Stage 4: Streaming Ingest, Resource Preflight, and Field Robustness."""
import io
from datetime import datetime, timezone
from pathlib import Path
import pytest
from PIL import Image

from edge import config, imageio
from edge.db import repo, repo_ext
from edge.pipeline import ingest


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_stage4.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "QUARANTINE_DIR", tmp_path / "data" / "quarantine")
    monkeypatch.setattr(config, "CROPS_DIR", tmp_path / "data" / "crops")
    config.ensure_dirs()
    repo.migrate()
    yield db_path
    repo.close_all()


def _make_test_image(path: Path, width=640, height=480, color=(120, 150, 90), exif_dt=None):
    im = Image.new("RGB", (width, height), color)
    if exif_dt:
        exif = im.getexif()
        exif[36867] = exif_dt.strftime("%Y:%m:%d %H:%M:%S")
        im.save(path, "JPEG", exif=exif)
    else:
        im.save(path, "JPEG")


def test_resource_preflight_calculator(clean_db, tmp_path):
    reserve_id = repo.create_reserve({"name": "Test Reserve", "utm_epsg": 32644})
    sd_card = tmp_path / "sd_card"
    sd_card.mkdir()

    for i in range(5):
        _make_test_image(sd_card / f"IMG_{i:04d}.jpg")

    preflight = ingest.resource_preflight(reserve_id, str(sd_card))
    assert preflight["ready"] is True
    assert preflight["total_files"] == 5
    assert preflight["raw_size_mb"] >= 0.0
    assert preflight["estimated_needed_mb"] > 0
    assert "batch_size" in preflight


def test_multi_signal_station_scoring(clean_db, tmp_path):
    reserve_id = repo.create_reserve({"name": "Pench", "utm_epsg": 32644})
    stn_id = repo_ext.create_station(reserve_id, {
        "station_id": "PN-C-001",
        "name": "Waterhole North",
        "lat": 21.65,
        "lon": 79.25,
        "zone": "core",
        "folder_hint": "PNC001",
        "camera_make": "CUDDEBACK",
        "camera_model": "X-Change",
        "camera_serial": "CB88902",
        "active_from": "2026-01-01T00:00:00+00:00",
    })
    stations = repo.stations(reserve_id)

    # 1. Matching folder + matching serial
    rec = {
        "folder": "PNC001",
        "orig_path": "/card/PNC001/IMG_0001.JPG",
        "make": "Cuddeback",
        "model": "X-Change",
        "serial": "CB88902",
        "captured_at": datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    }
    score, signals = repo_ext.multi_signal_station_score(rec, stations[0])
    assert score >= 0.85
    assert "serial_match" in signals

    matched_stn, conf, _ = ingest.match_station_multisignal(rec, stations, config.CONFIG.ingest)
    assert matched_stn is not None
    assert matched_stn["station_id"] == "PN-C-001"


def test_four_tier_timestamp_hierarchy_and_conflicts(clean_db, tmp_path):
    reserve_id = repo.create_reserve({"name": "Pench", "utm_epsg": 32644})
    stn_id = repo_ext.create_station(reserve_id, {
        "station_id": "STN-A",
        "name": "Station A",
        "lat": 21.5,
        "lon": 79.5,
        "folder_hint": "FOLDER_A",
        "active_from": "2026-01-01T00:00:00+00:00",
    })

    records = [
        # 1. Good EXIF
        {
            "path": Path("FOLDER_A/IMG_0001.JPG"), "folder": "FOLDER_A",
            "exif_dt": datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
            "corrupt": False, "flags": [], "captured_at": None, "captured_at_raw": None,
            "captured_at_source": "unknown", "drift_applied_s": 0, "ts_confidence": 0.0,
            "ts_method": "", "ts_evidence": {}, "ts_offset_s": 0,
        },
        # 2. Clock reset to 1970 (implausible) -> fallback to filename
        {
            "path": Path("FOLDER_A/2026_04_02_140000.JPG"), "folder": "FOLDER_A",
            "exif_dt": datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "corrupt": False, "flags": [], "captured_at": None, "captured_at_raw": None,
            "captured_at_source": "unknown", "drift_applied_s": 0, "ts_confidence": 0.0,
            "ts_method": "", "ts_evidence": {}, "ts_offset_s": 0,
        },
        # 3. No EXIF, no filename date -> linear interpolation
        {
            "path": Path("FOLDER_A/IMG_0003.JPG"), "folder": "FOLDER_A",
            "exif_dt": None,
            "corrupt": False, "flags": [], "captured_at": None, "captured_at_raw": None,
            "captured_at_source": "unknown", "drift_applied_s": 0, "ts_confidence": 0.0,
            "ts_method": "", "ts_evidence": {}, "ts_offset_s": 0,
        },
        # 4. Subsequent good EXIF
        {
            "path": Path("FOLDER_A/IMG_0004.JPG"), "folder": "FOLDER_A",
            "exif_dt": datetime(2026, 4, 2, 16, 0, 0, tzinfo=timezone.utc),
            "corrupt": False, "flags": [], "captured_at": None, "captured_at_raw": None,
            "captured_at_source": "unknown", "drift_applied_s": 0, "ts_confidence": 0.0,
            "ts_method": "", "ts_evidence": {}, "ts_offset_s": 0,
        }
    ]

    ingest._resolve_group_timestamps(records, "2026-01-01T00:00:00+00:00", config.CONFIG.ingest)

    # Record 1: EXIF
    assert records[0]["captured_at_source"] == "exif"
    assert records[0]["ts_confidence"] == 1.0

    # Record 2: Clock reset -> Filename tier
    assert records[1]["captured_at_source"] == "filename"
    assert records[1]["captured_at"].year == 2026
    assert "camera_clock_reset_suspected" in records[1]["flags"]

    # Record 3: Inferred from sequence
    assert records[2]["captured_at_source"] == "inferred"
    assert records[2]["captured_at"] is not None


def test_perceptual_hash_and_streaming_ingest(clean_db, tmp_path):
    reserve_id = repo.create_reserve({"name": "Pench", "utm_epsg": 32644})
    repo_ext.create_station(reserve_id, {
        "station_id": "STN-001",
        "name": "Station 1",
        "lat": 21.6,
        "lon": 79.3,
        "folder_hint": "STN001",
    })

    sd = tmp_path / "card"
    fld = sd / "STN001"
    fld.mkdir(parents=True)

    # Create 3 unique files + 1 exact duplicate
    _make_test_image(fld / "A.JPG", color=(100, 150, 200), exif_dt=datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc))
    _make_test_image(fld / "B.JPG", color=(200, 100, 50), exif_dt=datetime(2026, 5, 1, 10, 0, 5, tzinfo=timezone.utc))
    # Exact duplicate of A
    (fld / "A_copy.JPG").write_bytes((fld / "A.JPG").read_bytes())
    # Corrupt zero-byte file
    (fld / "CORRUPT.JPG").write_bytes(b"")

    res = ingest.preflight_ingest(reserve_id, str(sd), "Cycle-1")

    assert res["files_found"] == 4
    assert res["images_ingested"] == 3   # A, B, CORRUPT (A_copy is duplicate)
    assert res["duplicate_count"] == 1
    assert res["corrupt_count"] == 1

    # Verify terminal status on corrupt frame in DB
    images = repo.images_for_run(res["run_id"])
    corrupt_rows = [i for i in images if i["status"] in ("CORRUPT", "corrupt")]
    assert len(corrupt_rows) == 1


if __name__ == "__main__":
    import tempfile, shutil
    tmp = Path(tempfile.mkdtemp())
    try:
        class MonkeyPatch:
            def setattr(self, obj, attr, val):
                setattr(obj, attr, val)
        mp = MonkeyPatch()
        db_path = tmp / "test_stage4.db"
        mp.setattr(config, "DB_PATH", db_path)
        mp.setattr(config, "DATA_DIR", tmp / "data")
        mp.setattr(config, "QUARANTINE_DIR", tmp / "data" / "quarantine")
        mp.setattr(config, "CROPS_DIR", tmp / "data" / "crops")
        config.ensure_dirs()
        repo.migrate()

        test_resource_preflight_calculator(db_path, tmp)
        print("  ok   resource preflight calculator")
        test_multi_signal_station_scoring(db_path, tmp)
        print("  ok   multi-signal station scoring")
        test_clock_drift_anchor_resolution(db_path, tmp)
        print("  ok   clock drift anchor resolution")
        test_streaming_ingest_duplicate_and_corrupt_recovery(db_path, tmp)
        print("  ok   streaming ingest duplicate and corrupt recovery")
        print("\n4 passed, 0 failed")
    finally:
        repo.close_all()
        shutil.rmtree(tmp, ignore_errors=True)
