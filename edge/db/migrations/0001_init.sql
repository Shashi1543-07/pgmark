-- PUGMARK · migration 0001 · initial schema
-- FROZEN CONTRACT. Every module codes against this. Changes go in a new
-- migration file, never by editing this one.
--
-- Two structural rules that must never be relaxed:
--   1. reserve_id is on every domain table. Retrofitting tenancy is a rewrite.
--   2. origin_node / lamport / synced_at / row_hash are on every syncable
--      table. Retrofitting sync is also a rewrite.

-- ─── tenancy ────────────────────────────────────────────────────────────
CREATE TABLE reserves (
  reserve_id       TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  state            TEXT,
  utm_epsg         INTEGER NOT NULL,   -- per reserve. NEVER hardcode: a
                                       -- national deployment spans zones.
  boundary_geojson TEXT,
  created_at       TEXT NOT NULL
);

-- ─── runs ───────────────────────────────────────────────────────────────
CREATE TABLE runs (
  run_id         TEXT PRIMARY KEY,
  reserve_id     TEXT NOT NULL REFERENCES reserves(reserve_id),
  cycle_label    TEXT,                 -- 'Phase-IV 2026 Block II'
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  root_path      TEXT NOT NULL,
  image_count    INTEGER NOT NULL DEFAULT 0,
  stage          TEXT NOT NULL DEFAULT 'created',
  model_versions TEXT NOT NULL DEFAULT '{}',  -- exact weights for this run
  config         TEXT NOT NULL DEFAULT '{}',  -- thresholds in force
  schema_version INTEGER NOT NULL DEFAULT 1,
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);

-- ─── stations and survey effort ─────────────────────────────────────────
CREATE TABLE stations (
  station_id      TEXT PRIMARY KEY,
  reserve_id      TEXT NOT NULL REFERENCES reserves(reserve_id),
  name            TEXT,
  lat             REAL NOT NULL,
  lon             REAL NOT NULL,
  zone            TEXT NOT NULL CHECK(zone IN ('core','buffer','corridor')),
  village_dist_km REAL,
  grid_cell       TEXT,                -- reserves deploy on a grid; keep it
  folder_hint     TEXT,
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);

-- A station is not "on" forever. Without this table the alert engine
-- cannot distinguish "the tiger is gone" from "the camera was dead",
-- which is the single most important behaviour in the product.
CREATE TABLE station_activity (
  activity_id TEXT PRIMARY KEY,
  station_id  TEXT NOT NULL REFERENCES stations(station_id),
  start_date  TEXT NOT NULL,
  end_date    TEXT,                    -- NULL = still active
  note        TEXT                     -- installed | battery dead | stolen
);
CREATE INDEX ix_activity_station ON station_activity(station_id, start_date);

-- ─── images ─────────────────────────────────────────────────────────────
CREATE TABLE images (
  image_id           TEXT PRIMARY KEY,  -- sha256 prefix: dedupes by design
  reserve_id         TEXT NOT NULL REFERENCES reserves(reserve_id),
  run_id             TEXT REFERENCES runs(run_id),
  station_id         TEXT REFERENCES stations(station_id),
  orig_path          TEXT NOT NULL,
  sha256             TEXT NOT NULL,
  dhash              TEXT,
  captured_at        TEXT,
  captured_at_raw    TEXT,              -- ALWAYS keep the uncorrected value
  captured_at_source TEXT NOT NULL
      CHECK(captured_at_source IN ('exif','ocr','filename','inferred','unknown')),
  drift_applied_s    INTEGER NOT NULL DEFAULT 0,
  is_night           INTEGER NOT NULL DEFAULT 0,
  width INTEGER, height INTEGER, bytes INTEGER,
  status             TEXT NOT NULL
      CHECK(status IN ('pending','blank','subject','person','quarantined','corrupt')),
  triage_stage       TEXT,              -- which cascade stage decided: A | B
  flags              TEXT NOT NULL DEFAULT '[]',   -- JSON array of warnings
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);
CREATE INDEX ix_images_station_time ON images(station_id, captured_at);
CREATE INDEX ix_images_run_status   ON images(run_id, status);

CREATE TABLE detections (
  det_id        TEXT PRIMARY KEY,
  image_id      TEXT NOT NULL REFERENCES images(image_id),
  model         TEXT NOT NULL,
  model_version TEXT NOT NULL,
  label         TEXT NOT NULL,          -- animal | person | vehicle
  species       TEXT,                   -- tiger | leopard | boar | unknown
  conf          REAL NOT NULL,
  x REAL, y REAL, w REAL, h REAL        -- normalised 0-1
);
CREATE INDEX ix_det_image ON detections(image_id);

-- ─── events: a 3-shot burst is ONE visit, not three captures ────────────
CREATE TABLE events (
  event_id   TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES stations(station_id),
  started_at TEXT NOT NULL,
  ended_at   TEXT NOT NULL
);
CREATE TABLE image_event (
  image_id TEXT NOT NULL REFERENCES images(image_id),
  event_id TEXT NOT NULL REFERENCES events(event_id),
  PRIMARY KEY (image_id, event_id)
);

-- ─── individuals ────────────────────────────────────────────────────────
CREATE TABLE individuals (
  ind_id      TEXT PRIMARY KEY,         -- PENCH-014 | PENCH-P-007 provisional
  reserve_id  TEXT NOT NULL REFERENCES reserves(reserve_id),
  label       TEXT,
  provisional INTEGER NOT NULL DEFAULT 1,
  sex         TEXT,
  age_class   TEXT,
  first_seen  TEXT,
  last_seen   TEXT,
  national_id TEXT,                     -- reserved: cross-reserve identity
  notes       TEXT,
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);

CREATE TABLE flank_crops (
  crop_id             TEXT PRIMARY KEY,
  det_id              TEXT NOT NULL REFERENCES detections(det_id),
  side                TEXT NOT NULL CHECK(side IN ('L','R','unknown')),
  rect_ok             INTEGER NOT NULL DEFAULT 0,
  quality             REAL NOT NULL DEFAULT 0,
  path                TEXT,
  embedding           BLOB,
  embed_model_version TEXT              -- required to upgrade the embedder
                                        -- safely; see blueprint §12
);
CREATE INDEX ix_crop_side ON flank_crops(side);

CREATE TABLE assignments (
  assign_id     TEXT PRIMARY KEY,
  crop_id       TEXT NOT NULL REFERENCES flank_crops(crop_id),
  ind_id        TEXT NOT NULL REFERENCES individuals(ind_id),
  score         REAL NOT NULL,
  method        TEXT NOT NULL CHECK(method IN ('embed','keypoint','ensemble')),
  decision      TEXT NOT NULL CHECK(decision IN ('auto','review','human','enrolled')),
  confidence    REAL NOT NULL,
  superseded_by TEXT REFERENCES assignments(assign_id),  -- corrections
                                        -- supersede, never overwrite
  decided_at    TEXT NOT NULL,
  actor         TEXT NOT NULL
);
CREATE INDEX ix_assign_ind ON assignments(ind_id) WHERE superseded_by IS NULL;

CREATE TABLE review_queue (
  queue_id   TEXT PRIMARY KEY,
  crop_id    TEXT NOT NULL REFERENCES flank_crops(crop_id),
  candidates TEXT NOT NULL,             -- JSON [{ind_id, score, evidence}]
  priority   REAL NOT NULL,             -- ambiguity x images affected
  reason     TEXT,
  state      TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX ix_review_open ON review_queue(state, priority DESC);

-- ─── occupancy ──────────────────────────────────────────────────────────
CREATE TABLE occupancy (
  run_id              TEXT NOT NULL REFERENCES runs(run_id),
  ind_id              TEXT NOT NULL REFERENCES individuals(ind_id),
  station_set         TEXT NOT NULL,    -- JSON
  hull_wkt            TEXT,
  centroid_lat        REAL,
  centroid_lon        REAL,
  area_km2            REAL,
  event_count         INTEGER NOT NULL, -- EVENTS, not images
  effort_days         REAL NOT NULL,
  insufficient_reason TEXT,             -- set when < 3 stations; hull NULL
  PRIMARY KEY (run_id, ind_id)
);

-- ─── alerts ─────────────────────────────────────────────────────────────
CREATE TABLE alerts (
  alert_id        TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  ind_id          TEXT NOT NULL REFERENCES individuals(ind_id),
  type            TEXT NOT NULL
      CHECK(type IN ('centroid_shift','new_station','buffer_ward','absence')),
  severity        TEXT NOT NULL CHECK(severity IN ('info','watch','act')),
  what_changed    TEXT NOT NULL,        -- plain language, shown verbatim
  evidence        TEXT NOT NULL,        -- JSON
  confidence      REAL NOT NULL,
  effort_coverage REAL NOT NULL,
  suppressed      INTEGER NOT NULL DEFAULT 0,
  suppress_reason TEXT,
  acknowledged_by TEXT,
  acknowledged_at TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX ix_alerts_run ON alerts(run_id, suppressed, severity);

-- ─── quarantine: we never delete ────────────────────────────────────────
CREATE TABLE quarantine (
  q_id            TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  image_id        TEXT NOT NULL REFERENCES images(image_id),
  orig_path       TEXT NOT NULL,
  quarantine_path TEXT NOT NULL,
  reason          TEXT NOT NULL,
  conf            REAL NOT NULL,
  model_version   TEXT NOT NULL,
  threshold       REAL NOT NULL,
  bytes           INTEGER NOT NULL DEFAULT 0,
  restored_at     TEXT
);
CREATE INDEX ix_quar_run ON quarantine(run_id, restored_at);

-- ─── audit: append only, enforced by trigger ────────────────────────────
CREATE TABLE audit_log (
  log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  actor         TEXT NOT NULL,          -- 'system' | username
  action        TEXT NOT NULL,
  entity_type   TEXT,
  entity_id     TEXT,
  before        TEXT,
  after         TEXT,
  model_version TEXT,
  threshold     REAL,
  note          TEXT
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE INDEX ix_audit_ts ON audit_log(ts DESC);

-- ─── privacy ────────────────────────────────────────────────────────────
CREATE TABLE persons_restricted (
  image_id     TEXT PRIMARY KEY REFERENCES images(image_id),
  blurred_path TEXT NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0
);

-- ─── users and roles ────────────────────────────────────────────────────
CREATE TABLE users (
  username     TEXT PRIMARY KEY,
  display_name TEXT,
  role         TEXT NOT NULL
      CHECK(role IN ('field','biologist','director','analyst','admin')),
  pwd_hash     TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  disabled     INTEGER NOT NULL DEFAULT 0
);
