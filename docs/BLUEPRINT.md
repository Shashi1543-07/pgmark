# PUGMARK — Engineering Blueprint v2

**Automated Camera Trap Triage and Individual Tiger Movement Intelligence**

Viksit Nagpur Hackathon 2026 · Forest & Wildlife · Build window 10:00 IST 17 Aug → 10:00 IST 18 Aug 2026
Reference deployment: Pench Tiger Reserve · Design target: any tiger reserve in India

> **What this document is.** A clean-room build blueprint. Nothing here reuses code from any prior project — every component is specified to be written from scratch. It is written so an engineer who has never seen this project, or a future session with no memory of the conversation that produced it, can build the system end to end.
>
> **What changed from v1.** v1 assumed a single-reserve field tool that could borrow from an existing codebase. v2 assumes neither. It is designed as a standalone product deployable across Indian tiger reserves, which forces four additions v1 did not have: a **competitive landscape** (§1 — read this first, it changes the pitch), a **two-tier edge/central architecture** (§3), **security and data governance** for location data that has poaching value (§10), and **interoperability with the standards and systems that already exist** (§11).

---

## 0. TL;DR

A forest officer at a range office plugs in a camera-trap SD card, points the software at the folder, and walks away. No internet. No GPU. When it finishes:

1. Blank frames are gone — into **quarantine, reversibly**, never deleted.
2. Every tiger is found, its flank cropped and rectified, its stripe pattern matched against a catalogue. Confident matches auto-applied, ambiguous ones queued for a human, genuinely new tigers enrolled under a provisional ID.
3. Per individual: where it was seen, its home range, activity centroid, occupied area, and overlap with other tigers — on an offline map of the reserve.
4. This cycle compared against that individual's history, raising alerts for real change while **suppressing alerts that are only artefacts of uneven survey effort**.

Every automated decision is visible, explained, reversible, and logged. When connectivity appears — or when someone carries a USB drive to a place that has it — the reserve's results merge into a central tier that can hold every reserve in the country.

**Five things decide whether this project succeeds:**

| # | Thing | Why |
|---|---|---|
| 1 | **Honest positioning against CaTRAT / ExtractCompare / M-STrIPES** | These exist nationally. Not knowing that is fatal in Q&A. Knowing it, and naming the actual gap, is the strongest thing you can say. (§1) |
| 2 | **Motion pre-filter cascade** | The difference between 3 hours and 30 minutes on CPU. Throughput is an explicit evaluation criterion. (§6) |
| 3 | **Effort normalisation in the alert engine** | The PS names this trap directly. Miss it and your alerts are noise. (§9) |
| 4 | **Left flank ≠ right flank** | Different patterns, not mirror images. Match them to each other and accuracy collapses invisibly. (§7.3) |
| 5 | **Open-set evaluation** | The field is full of tigers not in your catalogue. Closed-set top-1 is the wrong number. (§13) |

**What is honestly buildable in 24 hours:** the complete edge node, working end to end, with real measured numbers. The central tier is *designed*, its interfaces are *stubbed*, and the schema is multi-reserve from line one — so the scale story is architecturally credible rather than aspirational. Say exactly that. A jury forgives "designed, not yet built" and does not forgive a claim that collapses under one question.

---

## 1. The landscape — what already exists, and where the gap actually is

**Read this before writing any code or any slide.**

India runs the largest camera-trap wildlife survey on earth. In the 2018-19 national assessment cycle, cameras were deployed at 26,838 locations covering 121,337 km², producing **34,858,623 photographs, of which 76,651 were of tigers**. India now holds over 3,600 tigers across 58 tiger reserves.

Sit with that ratio for a second: **roughly 0.2% of the images contain a tiger.** That is the problem statement's "large majority are false triggers", quantified at national scale. It is also the single best number in your pitch.

The state of the art already deployed nationally:

| System | What it does | Who |
|---|---|---|
| **M-STrIPES** | Field patrol and monitoring — GPS tracks, geotagged observations, central database, web dashboards. Live since 2010, used at Pench. | NTCA + WII |
| **CaTRAT** | Automatically classifies camera-trap images to species using neural networks. | NTCA + WII |
| **ExtractCompare** | Identifies individual tigers from stripe patterns; used to count individuals in the national estimation. | Used in AITE |
| **WILD-ID** | Stripe extraction and comparison, used at some reserves. | Reserve level |

### So what is left to build?

Everything above is oriented to **the census**: a four-yearly national count, processed centrally, producing population estimates. That is a different job from **the management cycle**, and the problem statement says so in its own words — that identifying which tiger appears in a photograph is slow and error-prone, that knowing where each tiger ranges depends on institutional memory rather than systematic analysis, and that **by the time a shift is recognised, the window for a management response has often passed**.

The gap, stated precisely, is four things:

1. **One integrated pipeline instead of four disconnected tools.** Today a range office has manual sorting, then possibly a species classifier, then possibly a separate stripe tool, with human handoffs between each. Pugmark is raw SD card → alerts in one uninterrupted run.
2. **Movement intelligence, not population counting.** Nothing in the national stack tells a range officer *this tigress has shifted 4 km toward Sillari since last cycle*. That is the entire fourth requirement of the PS and it is what makes the output actionable rather than archival.
3. **Effort-normalised alerting.** No deployed system distinguishes "the tiger is gone" from "the cameras were dead". §9 is built entirely around this.
4. **It runs at the range office, offline, on the officer's own laptop, on the same day the card comes out of the camera.** Not uploaded and waited on.

### How to say this in the pitch

> *"India already has CaTRAT for species and ExtractCompare for individual IDs, and they work — they built the world's largest camera-trap survey on them. But those are census tools: centralised, four-yearly, built to produce a number. A range officer at Pench, holding an SD card today, still has no way to find out that one of his tigresses has moved toward a village since last cycle. That's what we built, and it runs offline on his own laptop."*

Naming the incumbents is not a weakness. It is the fastest way to prove you understand the domain rather than the technology.

**One thing to verify if you get access to a domain expert before the 17th:** how much manual work ExtractCompare requires per image (flank marking, region selection). If it is substantially manual, full automation is an additional differentiator you can claim. If you cannot verify it, **do not claim it** — say "integrated and automated end-to-end" and leave the comparison implicit.

---

## 2. Requirements restated, with their traps

### R1 · Blank filtering
Ingest raw image directories exactly as they come off field SD cards. Classify each frame blank vs contains-subject. Remove blanks. **Deletion must be safe and reversible** — quarantine or staged delete with a confidence threshold, because irreversible deletion of a misclassified frame destroys irreplaceable field data. Report frames removed, space and time saved.

**Trap — the asymmetry.** A blank kept costs seconds of review. An animal discarded is unrecoverable science. Tune the operating point for **recall on contains-subject** and report the false-negative rate explicitly, not accuracy.

### R2 · Individual identification
Detect the animal, isolate the flank, extract the stripe pattern, match against a growing catalogue. New individuals **automatically enrolled** with a new identifier. Confident matches applied automatically. **Ambiguous matches surfaced to a human reviewer rather than silently guessed.** Result: a persistent, queryable database linking each individual to every image, station, timestamp and GPS location.

**Trap — this is a three-way decision.** Auto-accept / review / auto-enrol. Wrong in one direction merges two tigers into one; wrong in the other floods the review queue and inflates the population.

### R3 · Occupancy, regenerated every run
Per individual: capture locations, mapped home range estimate, activity centroid, estimated area occupied. Visualised on a reserve map, exported for forest staff. **Overlap between individuals must be visible** — territorial overlap is itself a management signal.

**Trap — projection.** Area in km² requires projecting lat/lon into a metric CRS first. Shoelace on raw degrees gives a confidently wrong number.

### R4 · Deviation and trend alerting
Compare each run to the individual's established history: centroid shift beyond threshold (15–20 sq km core, 5 km buffer), first capture at a never-used station, movement into or toward buffer or village-adjacent stations, prolonged absence of a previously regular individual. Alerts must **distinguish genuine behavioural deviation from artefacts of uneven survey effort**. Each alert states what changed, the supporting evidence, and a confidence level.

**Trap 1 — the units are inconsistent in the source.** "15–20 sq km" is an area; "5 kms" is a distance. Do not silently pick one. Implement both, make it configurable, default the core threshold to an area-equivalent radius (√(17.5/π) ≈ **2.36 km**), and state the interpretation on the slide. Ask the organisers if you can. Noticing the ambiguity reads as more careful than guessing right.

**Trap 2 — this is the hardest requirement in the PS.** §9 exists for it.

### R5 · Constraints
Ordinary field hardware — standard laptop, **no dedicated GPU, no internet**, because processing happens at a forest rest house or range office. Tens of thousands of images per batch in a practical timeframe. Clock drift, reset timestamps, inconsistent folder naming and mixed-up SD cards are normal — handle or flag, never fail. Privacy safeguards for images capturing humans. Usable by staff who are not data scientists. Every automated decision auditable and correctable.

**Critical distinction to state in the pitch:** the offline/CPU constraint governs the **deployed application**. Model *training* happens beforehand on a machine with a GPU and internet; only *inference* runs in the field. Nobody is training a network on a rest-house laptop, and you should be the one to say so first.

### R6 · The evaluation criteria — this is the jury's rubric
Blank-detection accuracy **with particular attention to false negatives** · individual identification accuracy **against a held-out reference set** · quality and interpretability of the occupancy output · **whether alerts are genuinely actionable rather than noisy** · processing throughput on constrained hardware · robustness against messy real-world input · usability by the intended end user.

Seven criteria, every one of them a number or a demo. Produce all seven.

---

## 3. Architecture

### 3.1 Two tiers, offline-first

```
╔══════════════════ EDGE NODE ═══════════════════╗   ╔══════ CENTRAL TIER ═══════╗
║ Range office laptop · offline · CPU only       ║   ║ Reserve HQ / state / NTCA ║
║                                                ║   ║                           ║
║  Local web UI (browser @ 127.0.0.1)            ║   ║  Web console (multi-user) ║
║        │                                       ║   ║        │                  ║
║  Application service (Python)                  ║   ║  API service              ║
║   ingest → triage → identify → occupancy →     ║   ║   cross-reserve catalogue ║
║   alerts                                       ║   ║   national rollups        ║
║        │                                       ║   ║   corridor / dispersal    ║
║  Model runtime (lazy load, idle unload)        ║   ║        │                  ║
║        │                                       ║   ║  PostgreSQL + PostGIS     ║
║  SQLite (WAL) + content-addressed image store  ║   ║  + ANN index (pgvector)   ║
╚════════════════════════╤═══════════════════════╝   ╚════════════▲══════════════╝
                         │                                        │
                         └──────── SYNC BUNDLE ───────────────────┘
                   signed, resumable, append-only, USB-carriable
```

**Why offline-first and not cloud-first.** A range office in Vidarbha does not have reliable connectivity, and the PS says so. Any design where the officer waits on an upload fails on day one. The edge node is fully functional alone, forever, with no central tier existing at all. Sync is an enhancement, never a dependency.

**Why sync must survive sneakernet.** In a genuinely disconnected range office the realistic transport is a USB drive carried to the divisional office. The sync bundle is therefore a **file**, not a socket: a signed archive that can travel over HTTP, over a USB stick, or over a phone hotspot at 2 a.m., and that applies idempotently no matter how many times it arrives.

### 3.2 Edge node internals

```
Browser (vanilla HTML/CSS/JS, no framework, no build step)
   │  JSON over HTTP + Server-Sent Events for progress
Application service — FastAPI + uvicorn, single process
   ├── Pipeline orchestrator: staged, resumable, content-hash cached
   ├── Stage modules: ingest · triage · identify · occupancy · alerts
   ├── Model runtime: every model behind a lazy handle that loads on
   │   first use and unloads after idle (an 8 GB laptop cannot hold the
   │   detector, the embedder and the OCR model resident at once)
   └── Sync agent: builds and applies bundles
Storage
   ├── SQLite in WAL mode — pugmark.db
   └── Filesystem — images/ crops/ quarantine/ restricted/ basemap/ weights/
```

**Why a local web UI and not a native desktop app.** The heavy work is Python (torch, OpenCV); the UI is a document with tables, images and a map. Serving a page on localhost gets a real layout engine, real image rendering and real map libraries for free, with no packaging toolchain that can break at 3 a.m. The officer double-clicks one launcher and a browser opens. No framework and no build step is a deliberate choice: a 24-hour project with a node toolchain in it spends some of those hours on the toolchain.

**Why not a notebook.** "Usable by forest department staff who are not data scientists" rules it out categorically.

### 3.3 Why SQLite at the edge and Postgres at the centre

At a single reserve the catalogue is on the order of 100–300 individuals × ~50 crops each × 2 flanks ≈ **30,000 vectors of 512 floats ≈ 60 MB**. A brute-force numpy cosine against all of them takes **under 20 ms**. An ANN index at that size costs a dependency, a service, an index-staleness bug class and an install failure mode, and buys nothing.

At national scale the arithmetic flips: ~3,600 tigers × 50 crops × 2 flanks ≈ **360,000 vectors**, growing every cycle, queried concurrently by many users. That is where an index earns its place — pgvector with HNSW alongside PostGIS, in the same database as the spatial data.

**Know both numbers.** "Why no vector database?" is a likely Q&A question, and *"at reserve scale brute force beats the index build; at national scale we use pgvector — here is the crossover"* is a much better answer than either "we didn't need one" or "we used one because that's what you use."

### 3.4 Repository layout

```
pugmark/
├── launcher/                  # run.bat / run.sh / installer scripts
├── edge/
│   ├── app.py                 # FastAPI application, routes, SSE
│   ├── ui/                    # index.html, app.js, app.css, offline map assets
│   ├── db/
│   │   ├── schema.sql
│   │   ├── migrations/        # 0001_init.sql, 0002_*.sql — versioned from day one
│   │   └── repo.py            # every query lives here; no SQL elsewhere
│   ├── config.py              # all thresholds, user-editable, surfaced in the UI
│   ├── pipeline/
│   │   ├── orchestrator.py
│   │   ├── ingest.py          # walk, EXIF, OCR timestamps, drift, stations, bursts
│   │   ├── triage.py          # motion prefilter + detector cascade + quarantine
│   │   ├── identify.py        # crop, rectify, embed, keypoint verify, match
│   │   ├── occupancy.py       # hull, centroid, area, overlap, projection
│   │   └── alerts.py          # four rules + effort normalisation
│   ├── effort.py              # station activity / camera-day model
│   ├── audit.py               # append-only log, quarantine manifest, restore
│   ├── privacy.py             # person routing, blur, access gating
│   ├── models/                # lazy handles: detector, classifier, embedder, ocr
│   ├── exports/               # camtrapdp.py, geojson.py, csv.py, mstripes.py
│   └── sync/                  # bundle build/apply, signing, conflict policy
├── central/                   # designed in v2, stubbed for the hackathon
│   ├── api/                   # FastAPI, auth, RBAC
│   ├── db/                    # Postgres + PostGIS + pgvector schema
│   └── jobs/                  # rollups, cross-reserve dispersal matching
├── tests/
│   ├── unit/                  # pure logic, no models, no I/O
│   ├── messy/                 # the fuzz corpus
│   ├── scenarios/             # synthetic deviation + confound suite
│   ├── live/                  # real server, stubbed models
│   └── fixtures/
├── data/                      # pugmark.db, stations.csv, basemap/, weights/
└── docs/
    ├── SETUP.md
    ├── MODEL_CHOICES.md       # including weight licences
    ├── SECURITY.md            # §10
    └── LIMITATIONS.md         # write this one honestly
```

---

## 4. Data model

Freeze this in the first two hours. Every module codes against it.

Two structural decisions distinguish v2 from a single-reserve tool: **`reserve_id` is on every domain table from the start**, and **every row carries sync metadata**. Retrofitting either later is a rewrite.

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Tenancy ───────────────────────────────────────────────────────────
CREATE TABLE reserves (
  reserve_id TEXT PRIMARY KEY,          -- 'PENCH-MH'
  name TEXT NOT NULL,
  state TEXT,
  utm_epsg INTEGER NOT NULL,            -- 32644 for Pench; per-reserve, never hardcoded
  boundary_geojson TEXT,
  created_at TEXT NOT NULL
);

-- ── Sync scaffolding, present on every domain table ───────────────────
--   origin_node : which edge node created the row
--   lamport     : monotonic counter for deterministic ordering
--   synced_at   : NULL until included in an accepted bundle
--   row_hash    : content hash, makes bundle application idempotent

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  reserve_id TEXT NOT NULL REFERENCES reserves(reserve_id),
  cycle_label TEXT,                     -- 'Phase-IV 2026 Block II'
  started_at TEXT NOT NULL, finished_at TEXT,
  root_path TEXT NOT NULL,
  image_count INTEGER DEFAULT 0,
  model_versions TEXT NOT NULL,         -- JSON: exact weights that produced this run
  config TEXT NOT NULL,                 -- JSON: thresholds in force
  schema_version INTEGER NOT NULL,
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);

CREATE TABLE stations (
  station_id TEXT PRIMARY KEY,
  reserve_id TEXT NOT NULL REFERENCES reserves(reserve_id),
  name TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL,
  zone TEXT CHECK(zone IN ('core','buffer','corridor')) NOT NULL,
  village_dist_km REAL,
  grid_cell TEXT,                       -- reserves deploy on a grid; keep it
  folder_hint TEXT,
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);

-- A station is not "on" forever. This table is what makes alert
-- suppression possible at all — without it §9 cannot be implemented.
CREATE TABLE station_activity (
  activity_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES stations(station_id),
  start_date TEXT NOT NULL,
  end_date TEXT,                        -- NULL = still active
  note TEXT                             -- 'installed' | 'battery dead' | 'stolen'
);

CREATE TABLE images (
  image_id TEXT PRIMARY KEY,            -- sha256 prefix: dedupes by construction
  reserve_id TEXT NOT NULL REFERENCES reserves(reserve_id),
  run_id TEXT REFERENCES runs(run_id),
  station_id TEXT REFERENCES stations(station_id),
  orig_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  dhash TEXT,                           -- perceptual, for near-duplicate bursts
  captured_at TEXT,
  captured_at_source TEXT CHECK(captured_at_source IN
      ('exif','ocr','filename','inferred','unknown')) NOT NULL,
  drift_applied_s INTEGER DEFAULT 0,
  captured_at_raw TEXT,                 -- ALWAYS keep the uncorrected original
  is_night INTEGER DEFAULT 0,
  width INTEGER, height INTEGER,
  status TEXT CHECK(status IN
      ('pending','blank','subject','person','quarantined','corrupt')) NOT NULL,
  flags TEXT,                           -- JSON array of ingest warnings
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);
CREATE INDEX ix_images_station_time ON images(station_id, captured_at);

CREATE TABLE detections (
  det_id TEXT PRIMARY KEY,
  image_id TEXT NOT NULL REFERENCES images(image_id),
  model TEXT NOT NULL, model_version TEXT NOT NULL,
  label TEXT NOT NULL,                  -- animal | person | vehicle
  species TEXT,                         -- tiger | leopard | boar | ... | unknown
  conf REAL NOT NULL,
  x REAL, y REAL, w REAL, h REAL        -- normalised
);

-- Burst grouping: a 3-shot trigger is ONE visit, not three captures.
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES stations(station_id),
  started_at TEXT NOT NULL, ended_at TEXT NOT NULL
);
CREATE TABLE image_event (
  image_id TEXT NOT NULL REFERENCES images(image_id),
  event_id TEXT NOT NULL REFERENCES events(event_id),
  PRIMARY KEY (image_id, event_id)
);

CREATE TABLE individuals (
  ind_id TEXT PRIMARY KEY,              -- 'PENCH-014' | 'PENCH-P-007' provisional
  reserve_id TEXT NOT NULL REFERENCES reserves(reserve_id),
  label TEXT,
  provisional INTEGER NOT NULL DEFAULT 1,  -- 1 until a human confirms
  sex TEXT, age_class TEXT,
  first_seen TEXT, last_seen TEXT,
  national_id TEXT,                     -- reserved: cross-reserve identity after sync
  notes TEXT,
  origin_node TEXT, lamport INTEGER, synced_at TEXT, row_hash TEXT
);

CREATE TABLE flank_crops (
  crop_id TEXT PRIMARY KEY,
  det_id TEXT NOT NULL REFERENCES detections(det_id),
  side TEXT CHECK(side IN ('L','R','unknown')) NOT NULL,
  rect_ok INTEGER NOT NULL,             -- rectification succeeded
  quality REAL NOT NULL,                -- blur / occlusion / pixel area, 0-1
  path TEXT NOT NULL,
  embedding BLOB,                       -- float32
  embed_model_version TEXT              -- required for §12 re-embedding
);

CREATE TABLE assignments (
  assign_id TEXT PRIMARY KEY,
  crop_id TEXT NOT NULL REFERENCES flank_crops(crop_id),
  ind_id TEXT NOT NULL REFERENCES individuals(ind_id),
  score REAL NOT NULL,
  method TEXT CHECK(method IN ('embed','keypoint','ensemble')) NOT NULL,
  decision TEXT CHECK(decision IN ('auto','review','human','enrolled')) NOT NULL,
  confidence REAL NOT NULL,
  superseded_by TEXT REFERENCES assignments(assign_id),  -- corrections, not deletions
  decided_at TEXT NOT NULL, actor TEXT NOT NULL
);

CREATE TABLE review_queue (
  queue_id TEXT PRIMARY KEY,
  crop_id TEXT NOT NULL REFERENCES flank_crops(crop_id),
  candidates TEXT NOT NULL,             -- JSON: top-K [{ind_id, score, evidence}]
  priority REAL NOT NULL,               -- ambiguity × images affected
  state TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE occupancy (
  run_id TEXT NOT NULL, ind_id TEXT NOT NULL,
  station_set TEXT NOT NULL,            -- JSON
  hull_wkt TEXT,
  centroid_lat REAL, centroid_lon REAL,
  area_km2 REAL,
  event_count INTEGER NOT NULL,         -- events, NOT images (see §5.5)
  effort_days REAL NOT NULL,
  insufficient_reason TEXT,             -- set when < 3 stations; hull is NULL
  PRIMARY KEY (run_id, ind_id)
);

CREATE TABLE alerts (
  alert_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL, ind_id TEXT NOT NULL,
  type TEXT NOT NULL,                   -- centroid_shift | new_station |
                                        -- buffer_ward | absence
  severity TEXT NOT NULL,               -- info | watch | act
  what_changed TEXT NOT NULL,           -- plain language, shown verbatim in the UI
  evidence TEXT NOT NULL,               -- JSON: image_ids, station_ids, dates
  confidence REAL NOT NULL,
  effort_coverage REAL NOT NULL,
  suppressed INTEGER NOT NULL DEFAULT 0,
  suppress_reason TEXT,
  acknowledged_by TEXT, acknowledged_at TEXT
);

CREATE TABLE quarantine (
  q_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL, image_id TEXT NOT NULL,
  orig_path TEXT NOT NULL, quarantine_path TEXT NOT NULL,
  reason TEXT NOT NULL, conf REAL NOT NULL,
  model_version TEXT NOT NULL, threshold REAL NOT NULL,
  restored_at TEXT
);

-- Append-only. Never UPDATEd, never DELETEd. Enforce with triggers.
CREATE TABLE audit_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,                  -- 'system' | username
  action TEXT NOT NULL,
  entity_type TEXT, entity_id TEXT,
  before TEXT, after TEXT,              -- JSON
  model_version TEXT, threshold REAL,
  note TEXT
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;

CREATE TABLE persons_restricted (
  image_id TEXT PRIMARY KEY REFERENCES images(image_id),
  blurred_path TEXT NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE users (
  username TEXT PRIMARY KEY,
  display_name TEXT,
  role TEXT CHECK(role IN
      ('field','biologist','director','analyst','admin')) NOT NULL,
  pwd_hash TEXT NOT NULL,               -- argon2id
  created_at TEXT NOT NULL, disabled INTEGER DEFAULT 0
);
```

**Three decisions worth defending in Q&A:**

`image_id` is a content hash. Ingest the same SD card twice and you get the same rows, not duplicates. Field staff *will* ingest the same card twice.

`assignments.superseded_by` means corrections **supersede** rather than overwrite. The history of who thought what, when, and on what evidence is never destroyed. This is what "auditable and correctable" actually requires.

`flank_crops.side` is a constrained column, not a soft attribute. §7.3 explains why this is load-bearing rather than cosmetic.

---

## 5. Stage 1 — Ingest: surviving real field data

Unglamorous, worth a large share of the "robustness against messy real-world input" criterion, and the stage most teams skip. Build it immediately after the schema.

### 5.1 The station manifest

Folder names are not a reliable source of station identity. Require `stations.csv`:

```csv
station_id,reserve_id,name,lat,lon,zone,village_dist_km,grid_cell,folder_hint
PN-C-014,PENCH-MH,Kolitmara North,21.6742,79.2981,core,7.4,G14,KOLIT_N
PN-B-031,PENCH-MH,Sillari Gate,21.6108,79.3327,buffer,1.2,G31,SILLARI
```

Match SD folder names against `folder_hint` with normalised fuzzy matching (case-folded, separators stripped, edit distance ≤ 2). **Never silently guess.** Unmatched folders appear in the preflight report and the run does not proceed until a human assigns or explicitly skips them.

If a reserve has no manifest, generate a skeleton from the folder tree with blank coordinates for the officer to fill in. A missing manifest must never be a dead end — most reserves will not have one in the format you want.

### 5.2 Timestamps — four tiers, and record which one fired

1. **EXIF `DateTimeOriginal`** — the happy path.
2. **OCR of the burned-in timestamp band.** Most camera traps print date and time into a strip along the bottom of the frame. When EXIF is missing or reset, crop the bottom ~8% and read it with a lightweight offline OCR engine. **Very few teams will build this, and it is exactly the field reality the PS is pointing at.**
3. **Filename parsing** — `IMG_20250714_213304.JPG` and its many cousins.
4. **Inferred** — interpolate from neighbouring files in sequence order.

All four fail → mark `unknown` and flag. Never invent a timestamp silently. Always keep `captured_at_raw` alongside any correction.

### 5.3 Clock drift and resets

Detect: implausible EXIF year (< 2005 or > now + 1) → reset camera; filename sequence order disagreeing with timestamp order → drift or reset; timestamps jumping backwards mid-folder → battery-change reset.

Correct by anchoring to the station's deployment date from `station_activity`, apply the offset, record it in `drift_applied_s`, show the correction in the UI, and let a human reject it.

### 5.4 Mixed-up SD cards

EXIF usually carries camera make, model and sometimes serial. One folder containing two distinct camera bodies means the cards got mixed. **Flag loudly; do not split automatically.** Guessing wrong here silently poisons every downstream location claim, which is the worst class of bug this system can have.

### 5.5 Burst grouping

Camera traps fire 2–5 frame bursts per trigger. Group images at the same station within `BURST_WINDOW_S` (default 10 s) into one `event`.

This matters more than it looks. Without it, one tiger walking past becomes three "captures", inflating counts, dragging the activity centroid toward busy stations, and skewing every occupancy statistic. **All occupancy and alert statistics are computed over events, never over images.** Saying this in the pitch signals domain understanding rather than CV competence.

### 5.6 Preflight report

Before any model runs, show the officer: images found by station · unmatched folders · timestamp-source breakdown · resets and drift detected · mixed-card warnings · duplicates · corrupt files · estimated processing time. Then a Confirm button.

**Nothing irreversible happens before this screen.** It is a large part of "usable by staff who are not data scientists" — it makes the machine's understanding visible before the machine acts on it.

---

## 6. Stage 2 — Triage: the cascade that buys your throughput

### 6.1 The insight

A general-purpose camera-trap detector on CPU runs roughly **0.3–1.0 s per image**. Twenty thousand images is 1.5–5.5 hours single-threaded; six workers gets you to 20–50 minutes. Acceptable, not impressive.

But **camera traps are static cameras**. A blank frame is definitionally a frame that looks like the empty scene. You do not need a neural network to notice that.

### 6.2 Three-stage cascade

```
   all frames
       │
  ┌────▼──────────────────────────────────────────┐
  │ STAGE A · Motion prefilter (classical CV, ~3ms)│
  │ per-station running MEDIAN background;         │
  │ mean absolute difference over a 16×16 cell grid│
  └────┬───────────────────────────────┬───────────┘
  clearly empty                   anything else
  (below LOW threshold)                │
       │                     ┌─────────▼───────────────┐
       │                     │ STAGE B · Detector       │
       │                     │ animal / person / vehicle│
       │                     └────┬───────────────┬─────┘
       │                       animal          person
       │                          │               │
  ┌────▼─────┐          ┌─────────▼──────┐  ┌─────▼──────┐
  │QUARANTINE│          │ STAGE C        │  │ PRIVACY    │
  │ (blank)  │          │ species gate   │  │ restricted │
  └──────────┘          │ tiger? crop?   │  │ (§10)      │
                        └────────────────┘  └────────────┘
```

Stage A typically removes **40–60% of frames at ~3 ms each**. That is the single largest throughput win available and it costs about 60 lines of classical computer vision.

### 6.3 Tuning Stage A safely

Stage A is the one place where a mistake destroys data. Bias it hard:

- Background is a **per-station running median** over a rolling window of that station's own frames. Median, not mean — a passing animal in a few frames does not move a median.
- Compare on a **cell grid**, not a global mean. A tiger filling 4% of the frame barely shifts a global average but lights up several cells.
- **Separate day and night backgrounds.** IR frames are a completely different image statistic.
- Set the threshold so that on your labelled validation set **Stage A's false-negative rate is zero**, then move it further toward caution. Anything remotely ambiguous goes to Stage B.
- Mask known nuisance regions: the timestamp band, and optionally a user-drawn mask over waving vegetation.

Report Stage A's own precision and recall separately in the benchmark. *"We removed half the frames before the network ever loaded, with zero animal loss on validation"* is a strong twenty seconds of a three-minute pitch.

### 6.4 Stage B — the detector

Use a purpose-built, openly available camera-trap detector (animal / person / vehicle) with a CPU-capable compact variant. Detectors of this family are in production across dozens of conservation organisations and exist specifically to filter blanks — reinventing one is the wrong engineering call and the jury will know it.

**Check the licence of the exact weight variant you ship**, because code licence and weight licence frequently differ. Record the answer in `MODEL_CHOICES.md`. When a forest officer asks whether they can legally deploy this, "we checked, here's the clause" beats a shrug.

### 6.5 Quarantine, never deletion

```
data/quarantine/<run_id>/
    manifest.json   # [{image_id, orig_path, quarantine_path, reason,
                    #   conf, model_version, threshold}]
    <files…>
```

Restore reads the manifest and puts everything back. It must be **idempotent** and must work **even if the database is lost** — the manifest alone is sufficient to reverse the operation. Every quarantine and restore writes to `audit_log`.

**Demo this.** Quarantine 8,000 frames, show the count, click Restore, show them back. A visible working undo is worth more to a jury than a percentage point of accuracy.

### 6.6 The savings report

The PS asks for space and time saved. Show:

- frames removed, MB freed
- **person-hours saved** = frames_removed × seconds_per_manual_review ÷ 3600

with `seconds_per_manual_review` a stated, editable assumption (2–4 s is defensible — say which you used). That person-hours figure is your headline impact metric: it is the number a range officer repeats to his DFO.

---

## 7. Stage 3 — Identification: stripes into identities

```
animal detection box
  → species gate: is this a tiger?
  → orientation: which way is it facing; is a flank visible at all?
  → side: LEFT or RIGHT flank
  → rectification: warp the flank to a canonical rectangle
  → quality gate: blur / occlusion / pixel area — refuse below threshold
  → embedding (deep) + keypoints (classical)
  → match against the side-specific catalogue
  → three-way decision
```

### 7.1 Rectification is the step teams skip

**Stripe patterns deform with posture.** A tiger stretching, turning or mid-stride presents the same stripes at different geometry. Matching raw crops means asking the model to absorb that deformation, and it will not.

**Tier 1 — no training, ~2 hours.** Segment the animal (GrabCut seeded by the detection box, or threshold on the Stage A background difference you already computed). Take the mask, run PCA to find the body's principal axis, rotate so the axis is horizontal, crop the torso between shoulder and hip as a fraction of body length, resize to a fixed rectangle (256×128).

**Tier 2 — stretch goal only.** Train a small keypoint model on pose annotations, use shoulder/hip/spine points to warp with a thin-plate spline. Better, but attempt it only once tier 1 is finished and tested. A half-finished tier 2 is worse than a working tier 1.

### 7.2 Quality gate — refusing is a correct answer

Before matching, score the crop on blur (variance of Laplacian), occlusion (mask coverage of the expected torso region), and pixel area. Below threshold, **do not match**. Record `rect_ok=0` and route to a "cannot assess" bucket.

A refusal costs nothing. A confident wrong match corrupts the catalogue permanently and inflates the reserve's population count — the exact failure mode that discredited the pugmark census.

### 7.3 Left flank is not right flank

**A tiger's left and right flank patterns are different and unrelated. They are not mirror images.** Reserves state this directly in their own monitoring documentation — the two flanks are described as being like separate fingerprints of the same animal.

Consequences, all mandatory:

- Determine side from head/tail orientation and store it in `flank_crops.side`.
- Maintain **separate catalogues per side**, linked by `ind_id`.
- **Never score an L crop against an R catalogue.** Enforce it in the query, not in a comment.
- When only one side has ever been seen for an individual, an unmatched opposite-side crop is **not** evidence of a new tiger. Record "possible same individual, opposite flank, unresolvable" rather than enrolling a phantom.

That last case is a genuine, unavoidable limitation of stripe-based re-ID that every system in this domain shares. Put it in `LIMITATIONS.md` and say it out loud in the pitch. Naming a real limitation makes every other claim you make more credible.

### 7.4 The matcher — two methods, ensembled

**Method A · deep embedding.** A backbone producing a 512-d vector, trained with a metric-learning objective (ArcFace or triplet) on labelled tiger identities. Cosine similarity against the side-specific catalogue. Fast, tolerant of moderate variation.

**Method B · keypoint matching with geometric verification.** Extract classical keypoints on the rectified flank, match against the top-K embedding candidates only, verify with RANSAC homography, score by inlier count. Slower per pair, but K≈5 means it costs nothing overall.

Method B earns its place for two reasons beyond accuracy:

1. **It is precise where embeddings are fuzzy.** Stripes are a rigid, high-frequency texture — exactly what keypoint matching was built for, and the basis of established individual-ID tooling for striped and spotted species. It also works with no training run at all, which makes it your fallback if the embedder underperforms.
2. **It draws its own explanation.** Matched inlier keypoints render as lines connecting the two flanks. Put that in the review screen and the reviewer can *see* why the machine proposed the match. That is interpretability a non-data-scientist can actually use — and it is the screenshot that goes on your slide.

Final score: `w · cosine + (1 − w) · normalised_inlier_ratio`, with `w` in config.

### 7.5 The three-way decision

```python
if score >= T_HIGH:            # default 0.82
    assign(best_candidate, decision='auto')
elif score >= T_LOW:           # default 0.55
    enqueue_review(top_k_candidates)
else:
    ind = enrol_new(provisional=True)      # 'PENCH-P-###'
    enqueue_review(reason='confirm new individual')
```

Note the asymmetry: **auto-enrolment still creates a review item.** The PS requires new individuals be enrolled automatically, but a wrongly enrolled phantom corrupts the catalogue permanently and inflates the population. Provisional IDs satisfy the requirement without accepting that risk; a human promotes a provisional to permanent.

Both thresholds live in config, are shown in the UI, and are written into `runs.config` so any historical result can be explained years later.

### 7.6 Review queue prioritisation

Sort by `ambiguity × images_affected`, where ambiguity is `1 − (top1_score − top2_score)`. A close call on a crop that would relabel 40 images matters far more than one affecting a single frame. A reviewer with twenty minutes should spend them where they change the most.

Keyboard-first: `J`/`K` to move, `1`–`5` to pick a candidate, `N` for new individual, `Enter` to confirm, `U` to undo. Nobody doing 200 reviews uses a mouse.

### 7.7 Corrections feed back

Every human decision writes to `audit_log`, supersedes the prior assignment, and **adds the confirmed crop's embedding to that individual's catalogue set**. The catalogue improves with use. Show a running "auto-accepted this session" counter climbing — the system visibly getting better is a good demo beat.

---

## 8. Stage 4 — Occupancy

Computed per individual, per run, **over events not images** (§5.5).

**Capture set** — stations where the individual appeared, with event counts and dates.

**Home range** — Minimum Convex Polygon over capture coordinates. MCP is the standard first-line estimate in the camera-trap literature and is defensible in Q&A; kernel density estimation is a stretch goal, not the MVP. **With fewer than three distinct stations an MCP is undefined** — write `insufficient_reason` and report it as such rather than emitting a degenerate polygon. This case will occur in your demo data; an unhandled one crashes the demo.

**Centroid** — mean of capture coordinates weighted by event count.

**Area in km²** — project to the reserve's metric CRS *first*, using `reserves.utm_epsg` (Pench: UTM 44N, EPSG:32644), then shoelace on the hull. Never hardcode the zone — a national system spans several. Computing area on raw degrees produces a silently, confidently wrong number.

**Overlap** — pairwise polygon intersection between individuals' hulls; report intersection area and the percentage of each range. Render overlapping hulls with transparency so overlap is visible at a glance, since the PS calls territorial overlap a management signal in itself.

**Export** — GeoJSON (loads directly in QGIS, which forest departments actually use) and CSV. The map must never be the only way to get data out.

### The offline map trap

**Web map libraries backed by online tile servers require internet.** At a rest house, and quite possibly at your demo venue, there is none. Your map will be a grey rectangle in front of the jury.

Ship instead: a **static basemap raster of the reserve with known corner coordinates**, rendered as an image overlay (or plain canvas with an affine transform), plus reserve boundary and core/buffer zones as **local GeoJSON**.

Test it with the network adapter **physically disabled** — not "wifi off in settings". Twice: once on day one, once the night before.

---

## 9. Stage 5 — The alert engine

This is the intellectual centre of the project. Everything else is competent assembly; this is where you either understand the problem or you do not.

### 9.1 The effort model

Detection is not observation. A tiger not photographed may be absent — or may simply have had no working camera near it.

```
effort(station, period) = camera-days the station was active in that period
                          (derived from station_activity)

effort_coverage(individual, period) =
    Σ effort over stations inside the individual's historical home range
  ─────────────────────────────────────────────────────────────────────
    the same sum, averaged over that individual's previous cycles
```

`effort_coverage ≈ 1.0` means this cycle watched the tiger's range about as well as previous cycles did. `0.3` means you barely looked. **Every alert carries this number, and it gates every alert.**

### 9.2 The four rules, each with its confound

**Rule 1 · Centroid shift.** Fire when displacement between this run's centroid and the historical centroid exceeds threshold — 2.36 km equivalent in core, 5 km in buffer (§2 R4).
*Confound:* a centroid from two captures is noise. Require a **minimum event count in both cycles** (default 5), and weight by the spatial distribution of effort — if the cameras on the east side died, the centroid moves west for free.

**Rule 2 · First capture at a never-used station.**
*Confound, and it is the obvious one:* **a station installed this cycle cannot produce a "new station" alert.** The tiger did not move; the camera arrived. Suppress unless the station was active in ≥1 prior cycle **during which the individual was detected somewhere else** — which proves it was detectable and simply did not use that station.

**Rule 3 · Movement toward buffer or village-adjacent stations.** Fire on first capture at a buffer or village-adjacent station, or on a decreasing trend in mean distance-to-nearest-village across cycles.
*Confound:* buffer stations are added and removed opportunistically. Normalise by buffer-zone effort — a tiger appearing in the buffer when buffer effort tripled is weaker evidence than the same capture at constant effort.
This is the highest-consequence alert type because it precedes conflict. Rank it above the others and give it `severity='act'`.

**Rule 4 · Prolonged absence.** Fire when an individual regular across the previous K cycles (default 3) is absent in this one.
*Confound, and it is the dangerous one:* if `effort_coverage < 0.6`, **do not fire an absence alert.** Emit instead:

> *"Insufficient survey effort in PENCH-014's range this cycle (coverage 0.31) — absence cannot be assessed."*

That behaviour deserves its own sentence in the pitch. **The pugmark census's fatal flaw was reporting confident conclusions where it had no information** — Sariska's tigers were declared present when they were already gone. A system that says *"I could not see"* instead of *"it is not there"* is the direct answer to that history, and it costs about fifteen lines of code.

### 9.3 Alert record

```json
{
  "type": "buffer_ward",
  "severity": "act",
  "ind_id": "PENCH-014",
  "what_changed": "First capture at Sillari Gate (buffer, 1.2 km from village). Core-only across the previous 4 cycles.",
  "evidence": {
    "image_ids": ["a19f…", "a19g…"],
    "station_ids": ["PN-B-031"],
    "dates": ["2026-07-14"],
    "prior_cycles_core_only": 4,
    "buffer_effort_ratio_vs_prior": 1.05
  },
  "confidence": 0.71,
  "effort_coverage": 0.94,
  "suppressed": false
}
```

### 9.4 Confidence must propagate

An alert cannot be more confident than the identity assignment beneath it. If the crops placing PENCH-014 at that station matched at 0.62, the alert's confidence is capped near 0.62 no matter how clean the movement signal looks.

```
alert_confidence = min(mean_assignment_confidence, rule_strength)
                   × effort_coverage_factor
```

Uncertainty propagation is a small amount of code and it is the entire difference between a system a scientist trusts and a system that shouts.

### 9.5 Show the suppressions

Give the alerts screen a **Suppressed tab** listing what the system chose *not* to raise, and why:

> *"PENCH-021 — new station PN-C-047: suppressed, station installed this cycle."*

Most systems hide their negative decisions. Showing them proves the effort normalisation exists and works, and it is the single most persuasive click in the live demo. When the jury asks how you avoid noisy alerts, you open this tab instead of answering.

---

## 10. Security and data governance

**Precise, current tiger locations are exactly what a poacher wants.** A system that aggregates every individual's coordinates, home range and movement trend into one queryable database is, if built carelessly, the most useful thing a poaching network could steal. Any claim to be deployable across India has to answer for this, and no other team will have thought about it.

**Roles and what each sees**

| Role | Locations | Person images | Exports | Config |
|---|---|---|---|---|
| `field` | own station only | no | no | no |
| `biologist` | full precision, own reserve | blurred only | yes | no |
| `director` | full precision, own reserve | on confirm, logged | yes | yes |
| `analyst` (state / national) | **generalised to grid cell** | no | aggregate only | no |
| `admin` | full | on confirm, logged | yes | yes |

**Coordinate generalisation.** Anything above reserve level sees grid cells, not points. National analysis needs distribution, not the tree the tigress sleeps under. This is standard practice for sensitive-species data and it costs one function.

**At rest.** Encrypt the database and image store on the edge node — a range office laptop is a stealable object. Argon2id for passwords. No default credentials, ever; first launch forces creation of an admin.

**In transit.** Sync bundles are signed and encrypted; the central tier rejects unsigned bundles. A USB stick is an untrusted channel by definition.

**Audit reads, not just writes.** Every access to precise coordinates or to a restricted person image writes to `audit_log` with the actor. In a poaching investigation, "who looked at PENCH-014's locations last month" is the question that matters.

**Person images.** Route them out of the tiger pipeline entirely (`persons_restricted`), store a blurred derivative for any UI display, gate the originals behind explicit confirmation, increment `access_count`, log every view. Report human presence to the officer as **counts by station and date**, not browsable images — that is the operationally useful form anyway, and it is the form that does not accidentally build a surveillance tool aimed at forest-dwelling communities.

**Retention.** Quarantined blanks are purgeable after a configured window with explicit sign-off. Restricted person images have a shorter default retention than wildlife images. Write both defaults into `SECURITY.md` and surface them in the UI.

One slide bullet and one Q&A answer. Very few hackathon projects can answer "what happens when this database leaks", and being able to is disproportionately convincing.

---

## 11. Interoperability — the actual argument for national deployment

A tool that cannot exchange data with the systems a reserve already runs will not be adopted, whatever its accuracy. Three integration surfaces, in priority order:

**1 · Camtrap DP export.** The Camera Trap Data Package is a community-developed exchange format for camera-trap data, developed under Biodiversity Information Standards (TDWG) in consultation with GBIF and the main existing camera-trap platforms. It structures data into three tables — **Deployments, Media and Observations** — and supports both human and AI classification and both media-based and event-based observations, which is precisely the shape of your output. It builds on Frictionless Data Package, so open tooling exists to read and validate what you emit.

Export to Camtrap DP and your output is readable by the wider ecosystem on day one. This is a few hundred lines and it converts "our tool" into "a citizen of the standard". Map your entities: `stations` + `station_activity` → Deployments, `images` → Media, `detections` + `assignments` → Observations.

**2 · Alignment with the national field system.** M-STrIPES already carries geotagged field observations, patrol tracks and reserve structure. Pugmark should consume its station and patrol data where available rather than asking staff to re-enter it, and emit observations in a form it can ingest. Treat this as an **adapter behind an interface**, not a hard dependency — you cannot verify the schema before the 17th, so define `exports/mstripes.py` with a clean interface and a documented "requires access to specify" note. Claiming an integration you have not built is the one thing that will cost you the room.

**3 · GIS and GBIF.** GeoJSON export for QGIS (what forest departments actually use), and a Darwin Core occurrence export path via Camtrap DP for biodiversity data publication.

**Say it this way:** *"We don't ask a reserve to replace anything. We read what M-STrIPES already knows, we do the part nobody does, and we hand results back in the community standard."* That is what deployable across India means in practice — not that the software is impressive, but that it fits between things that already exist.

---

## 12. Model lifecycle

Industry grade means a model you can replace without invalidating history.

**Version everything.** `runs.model_versions` records the exact weight hashes that produced a run; `flank_crops.embed_model_version` records which embedder produced each vector. Without the second column you can never safely upgrade the embedder.

**Upgrading the embedder.** Embeddings from different models are not comparable — a mixed catalogue silently degrades every match. On upgrade: re-embed the entire catalogue in the background, keep both vector sets until re-embedding completes, then flip atomically. **Human decisions are preserved and never re-run** — the identity a reviewer confirmed stays confirmed; only the vectors change.

**Reproducibility.** Any past run is re-derivable from `runs.config` + `runs.model_versions` + the content-addressed images. This is what lets a scientist defend a five-year-old conclusion.

**Distribution without internet.** Model updates ship as a **signed offline bundle** installable from USB, with the version, hashes and licence in the manifest. Sneakernet is the real update channel for a range office and designing for it is not a workaround.

**Drift monitoring.** Track, per run: share of crops rejected by the quality gate, review-queue rate, mean top-1 score, share of auto-enrolments. A rising review rate or a falling mean score is the earliest signal that the model no longer fits the data — new camera hardware, a new season, a new reserve. Surface these on a small ops panel; they cost nothing to compute and they are what turns a demo into a system.

---

## 13. Testing

The evaluation criteria are mostly numbers, and numbers come from tests. Build the harness alongside the code.

**One rule that governs all six layers:** a test that asserts a string exists in a source file proves you *wrote* something, not that it *works*. Anything a static check can fake gets a live counterpart.

### Layer 1 · Unit — pure logic, no models, no I/O

| Target | Assertion |
|---|---|
| EXIF parsing | missing / malformed / 1970 / future dates each land in the correct tier |
| Clock drift inference | a known injected offset is recovered within tolerance |
| Burst grouping | 3 frames 2 s apart = 1 event; 3 frames 5 min apart = 3 events |
| MCP area | a square of known side in UTM returns the correct km² |
| Polygon intersection | two half-overlapping squares give exactly 50% |
| Centroid shift | known coordinates, known displacement |
| Effort coverage | hand-built `station_activity` yields the expected fraction |
| Alert suppression | each confound suppresses; each true positive fires |
| Threshold routing | scores either side of `T_HIGH`/`T_LOW` route correctly |
| Quarantine round-trip | quarantine → restore returns byte-identical files to original paths |
| Side enforcement | an L crop scored against an R catalogue raises, never returns a match |
| Audit immutability | UPDATE and DELETE on `audit_log` both raise |
| Coordinate generalisation | an `analyst` query never returns sub-grid precision |

### Layer 2 · Messy-input fuzz corpus

Commit fixtures containing, deliberately: a zero-byte file · a truncated JPEG · a PNG named `.jpg` · EXIF year 1970 · no EXIF · duplicate filenames across folders · one folder with two camera serials · unicode and 200-character paths · an empty nested directory · a `.txt` among the images · a manifest CSV with CRLF endings and a UTF-8 BOM.

**Uniform assertion: the run completes, nothing crashes, every problem appears in the preflight report.** "Handle or flag rather than fail" is a direct requirement.

### Layer 3 · Model evaluation — the numbers for the slide

**Blank detection.** Labelled validation set from public camera-trap collections. Report precision, recall, and a **threshold sweep** with the operating point marked. Lead with **false-negative rate**, because the PS singles it out — and explain the asymmetry rather than just quoting the number. Report Stage A and Stage B separately as well as end-to-end; the Stage A figure is your throughput story.

**Re-identification.** Held-out split **by identity, not by image** — the same tiger must never appear in both train and test. Report **both**:

- *Closed-set:* top-1 and top-5 over known individuals.
- *Open-set:* can max-similarity separate a known individual from a genuinely new one? Report AUC and the chosen thresholds.

Open-set is the honest framing; the field is full of tigers not in your catalogue. A team reporting only closed-set top-1 is one question from an awkward silence, and reporting both converts the vulnerability into a credibility point.

Also report accuracy **split day vs night**. Night will be worse. Say so.

**Throughput.** A table: image count, wall-clock, images/sec, per-stage breakdown, machine spec, cores, peak RAM. Run it on the **least powerful laptop on your team** and quote that machine.

### Layer 4 · The synthetic scenario suite — your strongest test

Generate a synthetic capture history: a station grid with activity intervals, individuals with home ranges, captures sampled from those ranges. Then inject known truths and known traps:

| Injected | Expected behaviour |
|---|---|
| Range centroid shifted 4 km | `centroid_shift` fires |
| Capture at a genuinely new station | `new_station` fires |
| Movement toward a village-adjacent station | `buffer_ward` fires, ranked top, severity `act` |
| Individual actually gone | `absence` fires, high `effort_coverage` |
| **New camera installed where the tiger already lived** | `new_station` **suppressed** |
| **Cameras in the tiger's range dead this cycle** | `absence` **suppressed**, insufficient-effort message emitted |
| **Buffer effort tripled this cycle** | `buffer_ward` confidence reduced |
| Individual with only 2 captures | `centroid_shift` **not** fired (min-count guard) |

Every row is an assertion and together they are a direct, quantitative answer to *"are your alerts genuinely actionable rather than noisy?"* Put the pass table on a slide.

Build this suite **early** — it lets you develop and validate the alert engine without waiting for the CV pipeline to work, which decouples your two hardest workstreams.

### Layer 5 · Live end-to-end

Boot the real application on a test port with model classes stubbed to deterministic fakes, drive the actual HTTP routes with the fixture images, assert final database state and file layout.

This layer exists because a function that is defined but never called passes every static check and fails in production. **Assert the call site and the effect, not the existence.**

### Layer 6 · The offline test

Physically disable the network adapter and run the whole flow: ingest → triage → identify → occupancy → alerts → map → export.

Anything reaching for the internet must fail **loudly during development**, not silently at the demo: map tiles, model weight auto-download, font CDNs, a CDN-hosted JS library, telemetry inside a dependency.

---

## 14. Frontend

Nine screens. Plain HTML, CSS and JavaScript; no framework, no build step.

| Screen | Contains |
|---|---|
| **Run** | folder picker → preflight report → confirm → live progress with per-stage counts |
| **Triage** | frames removed, MB freed, person-hours saved, sample grid by confidence, **Restore** |
| **Individuals** | catalogue grid; per-tiger page with every capture, timeline, mini-map, L/R flank pair |
| **Review** | side-by-side crops with **keypoint match lines drawn**, top-5 candidates, keyboard-first |
| **Map** | offline basemap, per-tiger hulls, overlap shading, station markers sized by effort |
| **Alerts** | ranked by severity with what-changed / evidence / confidence, plus the **Suppressed** tab |
| **Audit** | searchable append-only log; every automatic decision with threshold and model version |
| **Ops** | run history, drift indicators (§12), sync status, model versions installed |
| **Sync** | build a bundle to USB, apply an incoming bundle, show what merged |

### Design rules for this specific user

The user is a forest department staff member on a laptop in a range office, not a data scientist.

- **Light mode first, high contrast.** Field offices are bright; dark themes are a developer preference.
- **No jargon in the interface.** "Not sure — please check" beats "confidence below T_LOW". "Camera was off" beats "insufficient effort coverage". Keep the precise term in a tooltip for the biologist.
- **Every automatic number is clickable** and shows the evidence behind it.
- **Undo is visible, not buried.** It is a stated requirement and a demo asset.
- **Bilingual labels (English + Marathi)** on primary actions — cheap, and high-signal for this jury.
- Progress shows **stage and count**, never a bare spinner. A run takes thirty minutes; a spinner for thirty minutes reads as a hang.
- Design tokens defined once — spacing scale, radius by role, elevation levels, motion durations — rather than invented per screen at 2 a.m.

---

## 15. Deployment and operations

**Edge install.** A single installer per platform bundling the runtime, dependencies, weights and basemap. Fully offline — no package downloads at install time. First launch forces admin creation and reserve selection.

**Schema migrations from version one.** `migrations/0001_init.sql` on day one, applied in order at startup, version recorded in the database. Adding this later to deployed field machines is genuinely painful.

**Structured logging with an offline ring buffer.** JSON lines to a local rotating file. Errors accumulate locally and ride out on the next sync bundle. An offline node still needs observability; it just cannot stream it.

**Sync semantics.**
- Bundles are **append-only streams** of rows plus the audit log, ordered by `(lamport, origin_node)`.
- Application is **idempotent** — `row_hash` makes re-applying the same bundle a no-op. Assume every bundle arrives twice.
- Images are content-addressed, so image sync is deduplication by construction.
- **Conflict policy:** human decisions beat machine decisions; between two human decisions, later `decided_at` wins and the loser is retained via `superseded_by`. Nothing is destroyed by a merge, ever.
- Catalogue identity is **reserve-scoped**. Cross-reserve identity linking (a dispersing sub-adult appearing in the next reserve) is a **central-tier review workflow**, never an automatic merge. That is a genuinely valuable national capability and exactly the wrong thing to automate.

**Backup.** One button producing a single portable archive of database + images + config. Field staff will not run a backup script.

---

## 16. Risk register

**1 · No real Pench data, and you will not get any in time.** Use public tiger identity datasets for re-ID, public camera-trap collections for blanks, and a **synthetic station grid over real Pench geometry** for occupancy and alerts. Be transparent: *"validated on public datasets and synthetic field scenarios; not yet validated on Pench data"* is honest and complete. Claiming Pench validation you do not have is the one thing that can lose the room outright.

**2 · Night IR frames are greyscale.** A large share of tiger captures are nocturnal; colour features are useless and stripe contrast differs. Train and evaluate with greyscale conversion and IR-style augmentation, and **report day and night accuracy separately.**

**3 · Domain gap.** Public identity datasets are largely clean, well-framed footage. Camera-trap stills are motion-blurred, partial, badly lit, sometimes just a tail. Mitigate with aggressive augmentation and the quality gate that refuses rather than guesses (§7.2).

**4 · Throughput on the demo laptop.** The cascade (§6), plus a content-hash result cache so a re-run of the same folder is instant. **Pre-run the demo dataset the night before** so the cache is warm — and be ready to say that is what you did.

**5 · Weight licensing.** Code licence and weight licence often differ per variant. Read them; record the answer in `MODEL_CHOICES.md`.

**6 · Nothing downloads at the venue.** Weights, datasets, packages, map tiles, fonts. **Pre-stage all of it on every team laptop before the 17th.** This is setup, not coding, and skipping it is what actually kills teams at 2 a.m.

**7 · Scope inflation from "industry grade".** The central tier, RBAC, sync and Camtrap DP are all specified here; **you cannot build them all in 24 hours.** Build the edge node completely, stub the rest behind real interfaces, and say clearly which is which. A working edge node plus a credible architecture beats four half-built tiers.

**8 · Threshold ambiguity in the PS** (sq km vs km, §2 R4). Handle both, make it configurable, state your interpretation.

**9 · Five people, 24 hours, mixed skill levels.** §17 is built around this.

**10 · Three minutes, no recovery time.** **Record a screen capture of the full working demo the night before.** Run live if it is working; cut to the recording the instant anything hesitates.

**11 · Provenance.** Be upfront in the deck about what is pre-existing: the detector, the public datasets, the CV libraries. Judges respect standing on open-source shoulders and dislike discovering it themselves in Q&A.

---

## 17. Build plan — 24 hours, five people

Roles assume one strong systems engineer as lead and four others of mixed ability. The split minimises blocking, which matters more than optimal allocation.

| Who | Owns |
|---|---|
| **P1 — Lead** | Schema, orchestrator, **alert engine**, scenario suite, final integration |
| **P2** | Ingest: walk, EXIF, OCR timestamps, drift, stations, bursts, preflight |
| **P3** | Triage: motion prefilter, detector, quarantine/restore, throughput benchmark |
| **P4** | Identify: crop, rectify, quality gate, embed, keypoint verify, matcher, review backend |
| **P5** | Frontend: nine screens, offline map, exports |

### Before the 17th — setup, not coding

Install the runtime and dependencies on every laptop. Download detector weights and the identity dataset. **Confirm the detector runs on CPU on every machine.** Pull a Pench boundary GeoJSON and a basemap raster. Create the repo skeleton and fixtures folder. **Agree the schema.** Anything on this list that happens after 10:00 on the 17th costs hours you cannot recover.

### The 24 hours

| Hour | Milestone |
|---|---|
| **T+0–2** | Schema frozen and pushed — everyone codes against it. Config with all thresholds. Repo skeleton. |
| **T+2–5** | P2 walking real folders. P3 detector returning boxes. P4 crops out of boxes. P5 shell UI with routing. P1 effort model + first two alert rules against synthetic data. |
| **T+5–8** | Cascade in and measured. Quarantine + restore working. Embedder producing vectors. Preflight rendering. |
| **T+8–11** | Matcher end to end with the three-way decision. Review queue backend. Occupancy with correct projection. **First full pipeline run on fixtures.** |
| **T+11–14** | Alert engine complete, all four rules with suppressions. Scenario suite green. Offline map rendering. Review UI with match lines. |
| **T+14–17** | **Feature freeze.** Evaluation runs: blank P/R + sweep, re-ID closed and open set, throughput table. Numbers go straight into the deck. |
| **T+17–19** | Fuzz corpus, offline test with adapter disabled, live end-to-end. Fix only what those break. |
| **T+19–21** | Deck built. **Record the backup demo video.** Exports working (GeoJSON, CSV, Camtrap DP if time). |
| **T+21–23** | Rehearse the three minutes aloud, to a stopwatch, five times minimum. Q&A prep (§18). |
| **T+23–24** | Warm the cache on the demo dataset. Charge everything. Stop coding. |

**Feature freeze at T+14 is the most important line in this table.** The last third is evaluation, testing and rehearsal, because that is what the jury scores. A team that codes until T+23 arrives with more features and no numbers, and loses to one that stopped.

---

## 18. The pitch — three minutes

**0:00–0:30 · The hook.** India runs the world's largest camera-trap survey — in one national cycle, 26,838 locations produced nearly 35 million photographs, of which about 77,000 had a tiger in them. Two images in a thousand. Everything else is moving grass, and today somebody sorts it by hand.

**0:30–0:50 · What already exists, and what doesn't.** CaTRAT classifies species, ExtractCompare fingerprints stripes, and they built that record-breaking survey. But those are census tools — centralised, four-yearly, built to produce a number. A range officer holding an SD card today still cannot find out that one of his tigresses has moved toward a village since last cycle.

**0:50–1:40 · Live demo.** Point at a folder → preflight catches a reset camera clock → run → triage report with person-hours saved → **click Restore, show the undo** → individuals catalogue → review screen with the keypoint match lines → map with two overlapping home ranges.

**1:40–2:20 · The alert that matters.** PENCH-014, first capture at a buffer station 1.2 km from a village. Then open the **Suppressed tab**: *"we did not raise this absence alert, because the cameras in that tiger's range were dead this cycle."* Then the line: **a system that says "I could not see" instead of "it is not there" is the direct answer to what went wrong at Sariska.**

**2:20–2:45 · Numbers and honesty.** Blank detection precision/recall with false-negative rate. Re-ID closed-set and open-set. Throughput on a laptop with no GPU. Then one limitation, stated plainly: left and right flanks are different patterns and cannot be matched to each other; validated on public datasets, not yet on Pench data.

**2:45–3:00 · Close.** Runs offline on a range office laptop. Every decision reversible and logged. Exports in the community standard so it fits alongside what reserves already run. Ready for a Pench pilot.

### Q&A preparation

- *"Doesn't CaTRAT already do this?"* → §1. Census versus management cycle, and the four specific gaps.
- *"How do you know your alerts aren't noise?"* → open the Suppressed tab, then show the scenario suite table.
- *"What's your accuracy on real Pench images?"* → unknown, and say so; here is what we validated on and exactly what a pilot would need.
- *"Isn't the detector someone else's model?"* → yes, deliberately; it is the field standard and reinventing it would be the wrong call. Here is what we built on top.
- *"What happens when a new tiger appears?"* → provisional enrolment plus a review item, and here is our open-set separation number.
- *"Why no vector database?"* → the crossover number in §3.3.
- *"Can forest staff actually use this?"* → preflight, plain language, keyboard review, visible undo, Marathi labels.
- *"What if this database leaks?"* → §10. Role-based coordinate generalisation, encryption at rest, signed bundles, audited reads.
- *"How does this scale to other reserves?"* → §3.1 and §11. Multi-reserve schema from line one, offline-first sync that survives a USB stick, exports in the community standard.

---

## 19. Deliverables checklist

Mapped to what the problem statement explicitly asks for.

- [ ] Working end-to-end pipeline demonstrated on a sample raw image set
- [ ] Individual database **with its schema documented**
- [ ] Map-based occupancy visualisation, working offline
- [ ] Functioning alert output with evidence and confidence on every alert
- [ ] Documentation: setup, model choices, **known limitations**
- [ ] Blank-detection accuracy with false-negative rate and threshold sweep
- [ ] Re-ID accuracy on a held-out reference set, closed and open set
- [ ] Throughput table on stated constrained hardware
- [ ] Robustness evidence — the fuzz corpus passing
- [ ] Usability evidence — preflight, plain language, undo, keyboard review
- [ ] Quarantine restore demonstrated live
- [ ] Backup demo recording

---

## 20. If you are a future session picking this up

Read **§1 first** — it decides the positioning and it is the thing most likely to be missing from whatever you already assume. Then **§9**, the alert engine, because that is where the thinking is; everything else is assembly around it.

The five things that decide this project, again: **honest positioning against the incumbents**, the **motion cascade**, **effort normalisation**, **left ≠ right flank**, **open-set evaluation**.

Three failure modes to watch for in yourself:

1. **Building models before the schema.** Everything downstream depends on the data contract. Freeze it in the first two hours.
2. **Optimising accuracy while the pipeline is broken end to end.** Get one image all the way through to an alert before improving any single stage.
3. **Confusing "the code contains this feature" with "this feature works."** Static checks prove authorship; live tests prove behaviour. Both, always.

And one thing worth defending under pressure: **the parts of this system that refuse to answer** — the quality gate that will not match a bad crop, the absence alert that will not fire without effort coverage, the review queue that will not guess, the analyst role that cannot see precise coordinates — **are the parts that make everything else trustworthy.** Under time pressure they will look like the easiest things to cut. They are the reason the system is worth building at all.
