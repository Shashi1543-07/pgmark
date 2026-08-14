-- images.status needs 'vehicle' as a legal value. Migration 0005 +
-- edge/pipeline/triage.py's fix for a jeep landing in the tiger
-- identification list (audit finding 11.1) writes status='vehicle', but
-- the CHECK constraint from 0001_init.sql never allowed it, and SQLite
-- cannot widen a CHECK constraint with ALTER TABLE -- the table has to
-- be rebuilt. Discovered by running the real pipeline against a real
-- vehicle-carrying frame, not by reading the migration.
--
-- Every column added since 0001 (subject_species, ingest_batch,
-- first_seen_run_id) and every index on images is preserved. This is
-- SQLite's own documented 12-step "how to alter a table" procedure:
-- foreign keys off, create the replacement, copy, drop, rename, foreign
-- keys back on. Other tables' FK declarations reference `images` by
-- name, re-resolved at check time, so nothing pointing at this table
-- needs to change.

PRAGMA foreign_keys = OFF;

CREATE TABLE images_new (
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
      CHECK(captured_at_source IN ('exif','ocr','filename','inferred','unknown')),
  drift_applied_s     INTEGER NOT NULL DEFAULT 0,
  is_night            INTEGER NOT NULL DEFAULT 0,
  width INTEGER, height INTEGER, bytes INTEGER,
  status              TEXT NOT NULL
      CHECK(status IN ('pending','blank','subject','person','vehicle','quarantined','corrupt')),
  triage_stage        TEXT,
  flags               TEXT NOT NULL DEFAULT '[]',
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT,
  subject_species     TEXT,
  ingest_batch        INTEGER,
  first_seen_run_id   TEXT
);

INSERT INTO images_new
SELECT image_id, reserve_id, run_id, station_id, orig_path, sha256, dhash,
       captured_at, captured_at_raw, captured_at_source, drift_applied_s,
       is_night, width, height, bytes, status, triage_stage, flags,
       origin_node, lamport, synced_at, row_hash,
       subject_species, ingest_batch, first_seen_run_id
FROM images;

DROP TABLE images;
ALTER TABLE images_new RENAME TO images;

CREATE INDEX ix_images_station_time ON images(station_id, captured_at);
CREATE INDEX ix_images_run_status   ON images(run_id, status);
CREATE INDEX ix_images_sha          ON images(sha256);
CREATE INDEX ix_images_reserve      ON images(reserve_id, captured_at);
CREATE INDEX ix_images_run_stage    ON images(run_id, triage_stage, status);
CREATE INDEX ix_images_batch        ON images(run_id, ingest_batch);

PRAGMA foreign_keys = ON;
