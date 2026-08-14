-- ─────────────────────────────────────────────────────────────────────────
-- 0005 — what v0.1.1 needs before it can be pointed at 50,000 photographs.
--
-- Five things, each of which was a real failure at scale rather than a
-- theoretical one:
--
--   1. jobs / job_items — a run's work must survive the process dying.
--      v0.1.1 did every stage inside one synchronous HTTP request with no
--      cursor: a crash at frame 30,000 lost 30,000 frames of work AND
--      (Stage A) left them physically moved into quarantine with no
--      manifest on disk to move them back.
--   2. species — Stage B's `animal` class is not `tiger`. v0.1.1 sent
--      every animal detection into the tiger re-identification pipeline,
--      so a sambar or a leopard could be enrolled as a new individual.
--   3. sessions — `users` existed from 0001 and was never used. Role came
--      from a query parameter, so ?role=director read precise tiger
--      locations with no credential at all.
--   4. indexes — the joins the catalogue, review queue and Camtrap DP
--      export actually run had no supporting index.
--   5. images.ingest_batch / content_key — cross-run duplicate detection
--      and restartable ingest.
--
-- Additive only. Nothing here drops or rewrites an existing column, so an
-- existing data/pugmark.db upgrades in place on the next startup.
-- ─────────────────────────────────────────────────────────────────────────

-- ─── 1. durable jobs ──────────────────────────────────────────────────────
-- One row per stage of a run. `cursor` is the resume point; `checkpoint_at`
-- is how a stale/crashed job is detected on startup. A job is never deleted
-- and never rewritten in place beyond its own status columns, so "what
-- happened to that import" has an answer months later.

CREATE TABLE jobs (
  job_id        TEXT PRIMARY KEY,
  run_id        TEXT REFERENCES runs(run_id),
  reserve_id    TEXT NOT NULL REFERENCES reserves(reserve_id),
  kind          TEXT NOT NULL
      CHECK(kind IN ('ingest','triage','stage3','postprocess','export','restore')),
  state         TEXT NOT NULL DEFAULT 'queued'
      CHECK(state IN ('queued','running','paused','done','failed','cancelled')),

  total         INTEGER NOT NULL DEFAULT 0,   -- units of work known so far
  done_count    INTEGER NOT NULL DEFAULT 0,   -- units completed and committed
  failed_count  INTEGER NOT NULL DEFAULT 0,   -- units that landed in job_items
  cursor        TEXT,                          -- opaque resume point, stage-defined

  created_at    TEXT NOT NULL,
  started_at    TEXT,
  checkpoint_at TEXT,                          -- last heartbeat; staleness detector
  finished_at   TEXT,

  error         TEXT,                          -- terminal failure message
  detail        TEXT,                          -- JSON: stage-specific counters
  actor         TEXT NOT NULL DEFAULT 'system',
  config        TEXT,                          -- config frozen at job start
  model_versions TEXT
);

CREATE INDEX ix_jobs_run    ON jobs(run_id, kind);
CREATE INDEX ix_jobs_state  ON jobs(state, created_at DESC);

-- The dead-letter table. A frame that fails is recorded and skipped, never
-- retried forever and never silently dropped: at 50K a handful of corrupt
-- files must not stop the other 49,990.
CREATE TABLE job_items (
  job_id     TEXT NOT NULL REFERENCES jobs(job_id),
  item_id    TEXT NOT NULL,                   -- image_id, det_id, ind_id …
  state      TEXT NOT NULL CHECK(state IN ('failed','skipped')),
  attempts   INTEGER NOT NULL DEFAULT 1,
  error      TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, item_id)
);

CREATE INDEX ix_job_items_state ON job_items(job_id, state);

-- ─── 2. species: `animal` is not `tiger` ─────────────────────────────────
-- detections.species existed in 0001 and was written as NULL by every code
-- path. These two columns record what actually decided it, so a wrong
-- species call is traceable to a model and a threshold rather than being
-- indistinguishable from "nobody looked".

ALTER TABLE detections ADD COLUMN species_conf   REAL;
ALTER TABLE detections ADD COLUMN species_source TEXT;   -- classifier|rule|human|unknown

CREATE INDEX ix_det_species ON detections(species) WHERE species IS NOT NULL;

-- images.subject_species lets the run screen say "1,240 animal frames, of
-- which 214 tiger" without a join per row.
ALTER TABLE images ADD COLUMN subject_species TEXT;

-- ─── 3. real sessions ────────────────────────────────────────────────────
-- Server-side, opaque, expiring. The token is stored hashed: a stolen
-- database must not hand over live sessions.

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  username   TEXT NOT NULL REFERENCES users(username),
  role       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen  TEXT,
  user_agent TEXT
);

CREATE INDEX ix_sessions_user ON sessions(username, expires_at);

-- ─── 4. the indexes the real queries needed ──────────────────────────────
-- Each of these backs a join that v0.1.1 ran as a full scan. On the 11k-row
-- demo that is invisible; at 50K it is the difference between a screen and
-- a hang.

CREATE INDEX ix_images_sha        ON images(sha256);              -- cross-run dedupe
CREATE INDEX ix_images_reserve    ON images(reserve_id, captured_at);
CREATE INDEX ix_images_run_stage  ON images(run_id, triage_stage, status);
CREATE INDEX ix_image_event_ev    ON image_event(event_id);        -- reverse lookup
CREATE INDEX ix_crops_det         ON flank_crops(det_id);
CREATE INDEX ix_crops_side_embed  ON flank_crops(side) WHERE embedding IS NOT NULL;
CREATE INDEX ix_assign_crop       ON assignments(crop_id) WHERE superseded_by IS NULL;
CREATE INDEX ix_occ_ind           ON occupancy(ind_id);
CREATE INDEX ix_audit_entity      ON audit_log(entity_type, entity_id);
CREATE INDEX ix_quar_image        ON quarantine(image_id);

-- ─── 5. restartable, cross-run-aware ingest ──────────────────────────────
-- ingest_batch groups the rows written by one checkpointed batch so a
-- resumed ingest knows exactly where it stopped. first_seen_run_id records
-- which run first ingested this content — the thing v0.1.1's
-- INSERT OR REPLACE destroyed when the same card was scanned twice.

ALTER TABLE images ADD COLUMN ingest_batch      INTEGER;
ALTER TABLE images ADD COLUMN first_seen_run_id TEXT;

CREATE INDEX ix_images_batch ON images(run_id, ingest_batch);

-- ─── 6. run stage machine ────────────────────────────────────────────────
-- runs.stage was a free-text column any string could be written into.
-- A view is used rather than a CHECK constraint because SQLite cannot add
-- one to an existing table without a full rebuild, and a rebuild of `runs`
-- would break every foreign key pointing at it. edge/db/repo.py enforces
-- the transitions; this view is how you audit whether it ever failed to.

CREATE VIEW runs_with_bad_stage AS
SELECT run_id, stage FROM runs
WHERE stage NOT IN ('preflight','confirmed','triaged','identified','complete','failed');

-- Every individual whose catalogue entry rests on a single flank. Promoted
-- from a repo query to a view so the API can page it — CLAUDE.md rule 6
-- calls single-flank a first-class state, and v0.1.1 had no route to show it.
CREATE VIEW catalogue_health AS
SELECT
  i.ind_id,
  i.reserve_id,
  i.provisional,
  COUNT(DISTINCT e.side)                                   AS sides_known,
  COALESCE(SUM(e.crop_count), 0)                           AS crops,
  (SELECT COUNT(*) FROM assignments a
     WHERE a.ind_id = i.ind_id AND a.superseded_by IS NULL
       AND a.decision = 'auto')                            AS auto_assignments,
  (SELECT ROUND(AVG(a.confidence), 3) FROM assignments a
     WHERE a.ind_id = i.ind_id AND a.superseded_by IS NULL) AS mean_confidence
FROM individuals i
LEFT JOIN entities e ON e.ind_id = i.ind_id
GROUP BY i.ind_id;
