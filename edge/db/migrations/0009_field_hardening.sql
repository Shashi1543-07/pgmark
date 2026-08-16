-- ─────────────────────────────────────────────────────────────────────────
-- 0009 — Field production hardening: terminal statuses, station metadata,
-- GeoJSON reserve boundaries, cross-flank associations, 10 intelligence alert
-- types, and telemetry metrics.
-- ─────────────────────────────────────────────────────────────────────────

PRAGMA foreign_keys = OFF;

-- ─── 1. Rebuild images with widened status & timestamp metadata ───────────
CREATE TABLE images_v9 (
  image_id            TEXT PRIMARY KEY,
  reserve_id          TEXT NOT NULL REFERENCES reserves(reserve_id),
  run_id              TEXT REFERENCES runs(run_id),
  station_id          TEXT REFERENCES stations(station_id),
  orig_path           TEXT NOT NULL,
  sha256              TEXT NOT NULL,
  dhash               TEXT,
  captured_at         TEXT,
  captured_at_raw     TEXT,
  captured_at_source  TEXT NOT NULL
      CHECK(captured_at_source IN ('exif','ocr','filename','inferred','unknown','conflict')),
  drift_applied_s     INTEGER NOT NULL DEFAULT 0,
  is_night            INTEGER NOT NULL DEFAULT 0,
  width               INTEGER,
  height              INTEGER,
  bytes               INTEGER,
  status              TEXT NOT NULL
      CHECK(status IN ('pending','blank','subject','person','vehicle','quarantined','corrupt',
                       'INGESTED','CORRUPT','UNREADABLE','BLANK','PERSON','VEHICLE',
                       'NON_TARGET_SPECIES','UNKNOWN_SPECIES','SIDE_UNKNOWN','LOW_QUALITY',
                       'IDENTITY_REVIEW','IDENTIFIED','NEW_INDIVIDUAL','DUPLICATE',
                       'ERROR_RETRYABLE','ERROR_PERMANENT')),
  triage_stage        TEXT,
  flags               TEXT NOT NULL DEFAULT '[]',
  origin_node         TEXT,
  lamport             INTEGER,
  synced_at           TEXT,
  row_hash            TEXT,
  subject_species     TEXT,
  ingest_batch        INTEGER,
  first_seen_run_id   TEXT,
  ts_confidence       REAL,
  ts_method           TEXT,
  ts_evidence         TEXT,
  ts_offset_s         INTEGER,
  error_stage         TEXT,
  error_type          TEXT,
  retry_count         INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,
  orientation         INTEGER NOT NULL DEFAULT 1,
  phash               TEXT
);

INSERT INTO images_v9 (
  image_id, reserve_id, run_id, station_id, orig_path, sha256, dhash,
  captured_at, captured_at_raw, captured_at_source, drift_applied_s,
  is_night, width, height, bytes, status, triage_stage, flags,
  origin_node, lamport, synced_at, row_hash, subject_species,
  ingest_batch, first_seen_run_id
)
SELECT
  image_id, reserve_id, run_id, station_id, orig_path, sha256, dhash,
  captured_at, captured_at_raw, captured_at_source, drift_applied_s,
  is_night, width, height, bytes, status, triage_stage, flags,
  origin_node, lamport, synced_at, row_hash, subject_species,
  ingest_batch, first_seen_run_id
FROM images;

DROP TABLE images;
ALTER TABLE images_v9 RENAME TO images;

CREATE INDEX ix_images_station_time ON images(station_id, captured_at);
CREATE INDEX ix_images_run_status   ON images(run_id, status);
CREATE INDEX ix_images_sha          ON images(sha256);
CREATE INDEX ix_images_reserve      ON images(reserve_id, captured_at);
CREATE INDEX ix_images_run_stage    ON images(run_id, triage_stage, status);
CREATE INDEX ix_images_batch        ON images(run_id, ingest_batch);
CREATE INDEX ix_images_phash        ON images(phash);

-- ─── 2. Station metadata additions ─────────────────────────────────────────
ALTER TABLE stations ADD COLUMN camera_make   TEXT;
ALTER TABLE stations ADD COLUMN camera_model  TEXT;
ALTER TABLE stations ADD COLUMN camera_serial TEXT;
ALTER TABLE stations ADD COLUMN active_from   TEXT;
ALTER TABLE stations ADD COLUMN active_to     TEXT;
ALTER TABLE stations ADD COLUMN status        TEXT NOT NULL DEFAULT 'active';

-- ─── 3. Reserve boundary GeoJSON layers ────────────────────────────────────
ALTER TABLE reserves ADD COLUMN core_geojson     TEXT;
ALTER TABLE reserves ADD COLUMN buffer_geojson   TEXT;
ALTER TABLE reserves ADD COLUMN corridor_geojson TEXT;

-- ─── 4. Cross-flank association tracking ───────────────────────────────────
CREATE TABLE cross_flank_associations (
  assoc_id      TEXT PRIMARY KEY,
  reserve_id    TEXT NOT NULL REFERENCES reserves(reserve_id),
  l_ind_id      TEXT NOT NULL REFERENCES individuals(ind_id),
  r_ind_id      TEXT NOT NULL REFERENCES individuals(ind_id),
  status        TEXT NOT NULL CHECK(status IN ('UNKNOWN_RELATIONSHIP','CANDIDATE','CONFIRMED','REJECTED')),
  confidence    REAL NOT NULL,
  evidence      TEXT NOT NULL,
  confirmed_by  TEXT,
  confirmed_at  TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX ix_cross_flank_res ON cross_flank_associations(reserve_id, status);
CREATE INDEX ix_cross_flank_l   ON cross_flank_associations(l_ind_id);
CREATE INDEX ix_cross_flank_r   ON cross_flank_associations(r_ind_id);

-- ─── 5. Run telemetry metrics ──────────────────────────────────────────────
CREATE TABLE run_telemetry (
  telemetry_id       TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL REFERENCES runs(run_id),
  images_per_sec     REAL,
  gpu_util           REAL,
  vram_used_mb       REAL,
  vram_total_mb      REAL,
  cpu_util           REAL,
  ram_used_mb        REAL,
  disk_read_mb       REAL,
  disk_write_mb      REAL,
  timing_decode_s    REAL,
  timing_detect_s    REAL,
  timing_species_s   REAL,
  timing_side_s      REAL,
  timing_keypoints_s REAL,
  timing_identify_s  REAL,
  timing_db_s        REAL,
  status_counts      TEXT NOT NULL DEFAULT '{}',
  recorded_at        TEXT NOT NULL
);
CREATE INDEX ix_telemetry_run ON run_telemetry(run_id);

-- ─── 6. Rebuild alerts for 10 intelligence alert types ─────────────────────
CREATE TABLE alerts_v9 (
  alert_id        TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  ind_id          TEXT NOT NULL REFERENCES individuals(ind_id),
  type            TEXT NOT NULL
      CHECK(type IN ('centroid_shift','new_station','buffer_ward','absence',
                     'directional_trend','decreasing_village_distance',
                     'activity_collapse','new_corridor','travel_time_anomaly',
                     'identity_confidence_collapse')),
  severity        TEXT NOT NULL
      CHECK(severity IN ('info','watch','act','insufficient_data',
                         'INFO','WATCH','ACT','INSUFFICIENT DATA','INSUFFICIENT_DATA')),
  what_changed    TEXT NOT NULL,
  evidence        TEXT NOT NULL,
  confidence      REAL NOT NULL,
  effort_coverage REAL NOT NULL,
  suppressed      INTEGER NOT NULL DEFAULT 0,
  suppress_reason TEXT,
  acknowledged_by TEXT,
  acknowledged_at TEXT,
  created_at      TEXT NOT NULL
);

INSERT INTO alerts_v9
SELECT alert_id, run_id, ind_id, type, severity, what_changed, evidence,
       confidence, effort_coverage, suppressed, suppress_reason,
       acknowledged_by, acknowledged_at, created_at
FROM alerts;

DROP TABLE alerts;
ALTER TABLE alerts_v9 RENAME TO alerts;

CREATE INDEX ix_alerts_run ON alerts(run_id, suppressed, severity);

PRAGMA foreign_keys = ON;
