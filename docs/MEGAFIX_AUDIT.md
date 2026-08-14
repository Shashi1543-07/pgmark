# PUGMARK — independent audit and mega-fix

**Method:** the code was run, not only read. A server was booted, the demo
was seeded, every route was called, and every claim below that carries a
number was measured on this codebase. Where a finding is reproduced, the
reproduction is given so you can check it yourself.

**Companion document:** `PUGMARK_50K_CODEBASE_AUDIT_AND_AGENT_SPEC.md` (the
GPT audit). It is a good document and most of its P0/P1 list is correct.
This one differs in three ways: it names several things that audit missed,
it corrects one place where that audit overstated a problem, and every
finding here is tied to a verification rather than an inspection.

---

## 0. The one-paragraph verdict

v0.1.1 is a genuinely well-built edge node with one structural problem and
several sharp edges. The structural problem is that **the last two stages of
the pipeline are not connected to the first three**. Everything else —
scale, restartability, the map, the review workflow — follows from that or
sits beside it. The engineering quality of the individual modules is high;
the wiring between them is where it fails, and wiring is invisible to code
review in a way that logic is not, which is exactly why hand-testing "felt
off" without pointing at anything nameable.

---

## 1. THE finding: Stages 4 and 5 have no caller

Run this against the unmodified source:

```
$ grep -rn "import occupancy\|import alerts" --include="*.py" edge/ tools/ tests/
edge/pipeline/alerts.py:21:from edge import config, effort
tools/seed_demo.py:40:from edge.pipeline import alerts
tools/seed_demo.py:41:from edge.pipeline import occupancy
tests/live/test_routes.py:42:from edge.pipeline import occupancy
```

`edge/app.py` imports neither. `edge/effort.py` likewise. **The only file in
the repository that ever writes an `occupancy` or `alerts` row is
`tools/seed_demo.py`.**

What this means in practice:

| | Seeded demo reserve | A folder you import yourself |
|---|---|---|
| Map | 35 home ranges | permanently empty |
| Alerts | 8 (4 raised, 4 suppressed) | permanently empty |
| Occupancy table | populated | permanently empty |

No error. No warning. No log line. The screens are simply empty for ever,
and nothing on them distinguishes "nothing happened this cycle" from "this
stage has never run and never will".

This is the single largest gap in the product, and it is why the app feels
like a demo when you drive it yourself. The GPT audit does identify it
(§23, §24) but files it as P1. It is P0: it is the difference between a
monitoring system and a screenshot.

**Fixed by** `edge/pipeline/postprocess.py` + `POST /api/runs/{id}/postprocess`.
Verified: wiping the occupancy and alerts tables and recomputing purely from
`assignments` reproduces all 35 occupancy rows and all 8 alert scenarios —
4 raised, 4 suppressed, each with the correct suppression reason.

```
PENCH-002    absence          watch  RAISED
PENCH-004    centroid_shift   watch  RAISED
PENCH-004    buffer_ward      act    RAISED
PENCH-011    new_station      info   RAISED
PENCH-005    centroid_shift   watch  suppressed  Only 2 events this cycle, below the minimum of 5…
PENCH-007    absence          watch  suppressed  Insufficient survey effort in this individual's range…
PENCH-009    new_station      info   suppressed  Station Deolapar was installed this cycle. The tiger…
PENCH-P-001  buffer_ward      act    suppressed  Identity confidence behind this capture is 0.39…
```

The alert engine was always right. It just never got called.

---

## 2. Data loss: the quarantine manifest is written after the files move

`edge/pipeline/triage.py`'s own module docstring:

> *"manifest.json is written before the DB row — restore() reverses it from
> that manifest alone, so it survives the database being lost"*

It does not. `_quarantine_file()` **moves the file** and appends to an
in-memory Python list. `manifest.json` is written only after the entire loop
over every station and every night-group has finished:

```python
for station_id, rows in by_station.items():
    ...
        for row in readable:
            if should_quarantine:
                _quarantine_file(...)      # ← moves the file NOW
if manifest:
    (quarantine_dir / "manifest.json").write_text(...)   # ← writes the map LATER
```

Crash, power cut, or lid-close at frame 30,000 of 50,000 and **30,000
original camera-trap frames have been physically moved into
`data/quarantine/<run_id>/` with no manifest on disk and no database rows.**
The mapping from quarantine path back to original path exists nowhere. That
is unrecoverable field data — a whole season of one range's images — and
the module promises the opposite in writing.

Stage B has the same structure with `manifest_stage_b.json`.

The GPT audit reaches this area (§42, "quarantine semantics need stronger
recovery guarantees") but does not identify that the current code already
fails its own stated guarantee.

**Fixed:** `_append_manifest()` writes and `fsync`s the manifest **before**
the batch of files it describes is moved, merging into any existing
manifest. The worst case inverts to a manifest entry for a file that was
never moved — which `restore()` already tolerates, because it checks
`src.exists()` first.

---

## 3. Data loss: re-scanning a card overwrites the earlier run

`images.image_id` is `sha256[:16]` — content-addressed, therefore *identical
across runs for the same photograph*. And `repo.insert_many()` uses
`INSERT OR REPLACE`.

Reproduced against the seeded database:

```
image f3fc9d14:  run_id  run_01 -> None
                 status  subject -> pending      == SILENT OVERWRITE
```

Scan the same SD card into a second run and the first run's rows are
replaced: `run_id` and `status` lost, and every `detection`, `flank_crop`
and `assignment` hanging off them orphaned. No error, no warning, no audit
entry. Dedupe in v0.1.1 is an in-memory `seen` dict scoped to a single scan
and never consults the database.

At a range office where the same cards get re-imported after a failed run —
which is exactly what happens when a 50,000-frame import times out — this is
the normal case, not the edge case.

**Fixed:** `repo.existing_image_ids()` checks the database, `insert_many_ignore()`
replaces `INSERT OR REPLACE`, and preflight reports `cross_run_duplicates`
with a plain sentence saying the earlier run's data was left alone.

---

## 4. The map is hardcoded to the demo, and 87% out of shape

Verbatim from `edge/ui/app.js`, inside the map renderer:

```js
const DEAD = new Set(['PN-C-008', 'PN-C-009']);
const NEW  = new Set(['PN-C-015']);
```

Those are station IDs from `tools/seed_demo.py`. On a real reserve **no
camera is ever drawn as failed and none as newly-installed**, because the
frontend reads a constant instead of `station_activity`.

It is worse than fragile — it was already wrong for the demo it was written
for. Deriving the same fact from `station_activity` finds **four** cameras
that stopped at day 4 of a 122-day cycle:

```
PN-C-008  Rukhad Ghat   4.0 days      ← in the hardcoded set
PN-C-009  Totladoh      4.0 days      ← in the hardcoded set
PN-C-010  Bodhalzira    4.0 days      ← NOT drawn, ever
PN-C-011  Ambakhori     4.0 days      ← NOT drawn, ever
```

Half the failed cameras were invisible on the map that was supposed to
explain the absence alerts.

**The geometry is also wrong.** Latitude and longitude were each stretched
independently to fill a fixed 900×520 box. Measured on the seeded reserve:

```
true extent:   9.8 km east-west × 10.0 km north-south   (aspect 0.98, near square)
drawn at:      832 × 452 px                              (aspect 1.84)
=> every home-range polygon rendered 87% out of shape
```

Directly beneath a table of areas that `edge/pipeline/occupancy.py` computes
by projecting into the reserve's own UTM zone specifically to avoid this
class of error. The care in the backend is discarded by the last twenty
lines of the frontend. There is also no scale bar, no north arrow, no
legend, no way to isolate one tiger, and the "CORE"/"BUFFER" zone rectangles
are hardcoded pixel offsets bearing no relation to which stations are in
which zone.

**Fixed:** `edge/ui/map.js` — one scale for both axes with longitude
corrected by `cos(mean latitude)`, zones drawn as hulls of the stations
actually in them, camera state derived from `station_activity`, a scale bar,
a north arrow, a legend, per-tiger focus, and arrows showing each
individual's centroid movement since the previous cycle (which is what the
`centroid_shift` alert is *about*, and which nothing drew).

Not in the GPT audit at all.

---

## 5. Five routes that exist and are unreachable; capabilities with no route

Cross-referencing every `@app.route` in `edge/app.py` against every URL in
`edge/ui/`:

| Route | Status |
|---|---|
| `POST /api/individuals/{id}/promote` | **no button anywhere** |
| `POST /api/alerts/{id}/acknowledge` | **no button anywhere** |
| `GET /api/stations/export.geojson` | no link anywhere |
| `GET /api/runs/{id}/preflight` | never re-fetched |
| `GET /api/health` | never displayed |

The first two matter and matter in the same way: the app **auto-enrols
provisional individuals** and **raises alerts**, and gives the officer no
way to confirm either. Provisional tigers accumulate for ever. Alerts can be
read but never marked handled.

Going the other way, these have no route at all:

- `repo.entities()`, `repo.single_flank()`, `repo.rebuild_entities()` — even
  though CLAUDE.md rule 6 calls single-flank *"a first-class state, not an
  absence"*. The UI cannot ask which individuals are in it.
- occupancy generation, alert generation (§1)
- bulk Stage 3 (§6)
- any job/progress/cancel/resume concept
- any pagination
- merging two provisional individuals that turn out to be the same tiger —
  a state the system creates on its own and cannot resolve
- backup, integrity check, WAL checkpoint

**Fixed:** 22 new routes in `edge/routes_scale.py`.

---

## 6. Stage 3 is a human clicking a button 4,000 times

There is no bulk Stage 3 in v0.1.1. `identify_upload.process_upload()` takes
exactly one photograph, and the only way to apply it to a run is
`POST /api/runs/{id}/images/{image_id}/identify`, driven from an
**unpaginated** table with one button per animal frame:

```js
const subjectImages = t.subject
  ? await api(`/api/runs/${nr.runId}/images?status=subject`) : [];
```

945 rows on the seeded demo; ~4,000 scaled to a 50,000-frame import. Each
click:

- loads a ~100 MB ResNet-50 from disk (`load_embedder()` is called per photo)
- re-reads and re-deserialises the **entire** side catalogue
- calls `rebuild_entities()` — a full regroup of every assignment in the
  reserve — after every single assignment
- processes only the highest-confidence animal box, **discarding every other
  animal in the frame** (two tigers at a waterhole is the normal case)

**Fixed:** `edge/pipeline/stage3.py` — warm models loaded once per job, the
catalogue held as one matrix with vectorised matching, `rebuild_entities()`
once at the end, every animal box treated as its own identity question, and
batched checkpointing so it resumes after a crash.

---

## 7. Two silent correctness holes the pipeline cannot see past

### 7a. `animal` is not `tiger`

MegaDetector answers `animal` / `person` / `vehicle`. `detections.species`
exists in the schema from migration 0001 and **every code path writes it as
`NULL`**. v0.1.1 sends every animal detection into the flank pipeline, so a
leopard, sambar, wild dog or langur is embedded, scored against the tiger
catalogue, and — below `t_low` — **enrolled as a brand-new tiger with a
provisional ID**. On a real reserve the overwhelming majority of animal
detections are not tigers, so this is the common failure, not the rare one.

### 7b. The keypoint model does not know left from right

`edge/pipeline/keypoints.py` labels every prediction `right_shoulder` /
`right_hip` *regardless of which flank is showing*. The repo documents this
honestly — `docs/RESULTS.md`'s wild evaluation found `side='R'` on 100% of
held-out images — but the consequence at scale is not drawn out: on a real
deployment roughly half of captures show the left flank and would be scored
against the **right-side catalogue**, silently.

There is a sharper edge underneath it that neither the code comments nor the
GPT audit call out. When the trained model finds nothing, `estimate_keypoints()`
falls back to `estimate_keypoints_stub()`, which returns a fixed geometric
guess — shoulder at 25% of the box, hip at 75% — **with COCO visibility = 2
("labelled and clearly visible") on both points**. `quality_gate()` scores
visibility, so a pure geometric guess reports `quality = 1.0` and sails
through the gate that exists to reject exactly this. The refusal architecture
the whole system is built around is bypassed by its own fallback.

**Fixed:** `Identify.species_gate` (`strict` / `review` / `off`, default
`review`) and `Identify.require_side_classifier` (default `True`). With the
defaults, bulk Stage 3 computes crops, quality and embeddings — the work is
not thrown away — but writes **no assignment and no enrolment**; everything
goes to the review queue with the reason stated in plain language. This is
CLAUDE.md rule 8 applied where it matters most. `/api/health/ready` reports
both gates so nobody discovers them by surprise.

---

## 8. The whole server dies if PyTorch is missing

```
$ python3 -c "import edge.app"
ModuleNotFoundError: No module named 'torch'
```

`edge/app.py` → `identify_upload` → `detector` → `import torch`, all at
module scope, plus `triage.py` importing `detector` the same way. One
optional dependency for one stage takes down triage stats, the map, alerts,
the audit log, the catalogue and the review queue — every screen that needs
no model at all.

Stage B already refuses gracefully when the *weights* are missing (rule 8).
It should do the same when the *runtime* is. **Fixed:** lazy imports; the
server now boots and serves 63 routes with torch absent, and
`/api/health/ready` names the missing runtime and the command that installs
it.

---

## 9. Scale: measured, not assumed

Measured on a 4000×3000 camera-trap JPEG, this codebase's own workload:

| | per frame | × 50,000 |
|---|---|---|
| Stage A grid read, v0.1.1 | 143.7 ms | **120 min** |
| Stage A grid read, with `Image.draft()` | 38.4 ms | **32 min** |

`Image.draft()` asks libjpeg to decode DCT coefficients at 1/8 scale during
decompression. Stage A downsamples to a 16×16 grid, so v0.1.1 was building a
12-megapixel array in order to throw away 99.99% of it. One line, 3.7×.
Ingest does the same thing **three times per frame** (`verify()`, EXIF
re-open, then `_night_heuristic()` doing `convert("RGB").resize((32,32))` on
the full-resolution image).

All of it runs inside the HTTP request that asked for it, with no cursor
anywhere.

**One correction to the GPT audit.** It lists "DB commits are far too
frequent" as P0 (§9). Measured on the seeded database: `next_lamport()` is
0.120 ms/call and `set_image_status()` 0.054 ms/call — 6 s and 2.7 s
respectively across 50,000 frames. Real, worth batching, and roughly two
orders of magnitude smaller than the decode cost. **Fix the decode path
first.** Commit batching is included here anyway (`set_image_status_many()`,
`bump_lamport_for_run()`, `transaction()`) because the transactional
*correctness* matters even where the microseconds do not — a cursor
committed separately from the rows it describes can be lost independently
of them.

---

## 10. RBAC is a query parameter

```python
def _role(role: str = Query("director", pattern="^(field|biologist|director|analyst|admin)$")):
    return role
```

`?role=director` reads precise coordinates of individual tigers with no
credential of any kind. A `users` table with `role` and `pwd_hash` has
existed since migration 0001 and is never queried; the UI never sends a role
at all, so everything runs as `director` by default.

The module docstring is exactly right about the stakes — *"this process holds
precise locations of individual tigers, which is exactly what a poaching
network would want"* — and the control does not exist. Binding to 127.0.0.1
is the only thing standing between that data and anyone on the network, and
`PUGMARK_HOST` is an environment variable.

Migration 0005 adds the `sessions` table (hashed tokens, expiry). Session
issuance and the `Depends()` wiring are **specified but not implemented
here** — see `MEGAFIX_APPLY.md` §"What is not fixed". Doing it half way, so
that a route *looks* authenticated and is not, would be worse than leaving
it visibly open.

---

## 11. Everything else, briefly

| # | Finding | Fixed |
|---|---|---|
| 11.1 | Vehicle frames written as `status='subject'` — a jeep enters the tiger identification list | yes |
| 11.2 | Two reviewers in two tabs can both decide the same queue item; the second silently supersedes the first | yes — `claim`/`release` |
| 11.3 | A review correction changes both tigers' home ranges and can raise or silence an alert; nothing recomputed | yes — auto-recompute on decide |
| 11.4 | Duplicate provisional individuals (same tiger enrolled twice) — created by the system, unresolvable by it | yes — `merge`, supersedes not deletes |
| 11.5 | `runs.stage` is free text; triage sets `'triaged'` even when Stage B skipped every frame | yes — enforced transitions |
| 11.6 | `runs.model_versions` written as `{}` and never updated — a stored run cannot say which models produced it | yes |
| 11.7 | No index on `images(sha256)`, `image_event(event_id)`, `flank_crops(det_id)`, `assignments(crop_id)` | yes — 10 indexes |
| 11.8 | No backup path at all. One laptop, one file, one season | yes — online backup via SQLite's backup API |
| 11.9 | WAL never checkpointed; after a large import `-wal` can exceed the database | yes |
| 11.10 | `identify_upload` writes `sha256 = image_id` (a UUID, not a hash) and `run_id = NULL`, so those rows are undedupable and unsyncable | documented, see §"not fixed" |
| 11.11 | Thumbnails served at full resolution — a review screen pulls 50 full-size JPEGs to render 96 px squares | helper shipped (`imageio.thumbnail_bytes`), routes not yet switched |
| 11.12 | Failed frames counted and forgotten — no dead-letter record | yes — `job_items` + route |
| 11.13 | Time estimate was `count × a config constant nobody measured`, shown as if it were a forecast | yes — ETA from measured throughput |
| 11.14 | PyTorch defaults to every core; a 50K run makes the laptop unusable for hours | yes — `Triage.torch_threads` |

---

## 12. What was already good, and should not be "fixed"

Worth saying plainly, because a fix list reads like a verdict and this one
is not.

The data contract is frozen first and coded against. All SQL is in one
place, and the discipline holds. Every threshold lives in config, is written
into `runs.config`, and is rendered on screen. `audit_log` is append-only
with SQL triggers. Corrections supersede rather than overwrite. The entity
model — one side of one tiger, keyed `(ind_id, side)`, with
`catalogue_for_side()` *raising* rather than returning the wrong list — is
the correct reading of the ATRW result and is enforced structurally rather
than by convention. The alert engine's confounds (effort coverage, minimum
event counts, "the camera arrived, not the tiger") are the difference
between a useful system and a nuisance generator. `mstripes.py` refuses and
says why rather than faking an integration. The UI has no CDN, no webfont,
no map tiles, and a test that greps for them.

Most notably: `t_high` was raised from 0.82 to 0.95 *because a measurement
said 14.7% of novel tigers were being auto-accepted*, and the reasoning is
recorded in the config docstring. That is a real engineering culture, and
none of it is what needed fixing.

The gap is not judgement. It is that the last two stages were never plugged
in, and nothing in the test suite could notice, because both stages are
tested directly and neither test asks *"does the application ever call
this?"*

---

## 13. What is genuinely still broken after this fix

Stated plainly, because the repo's own working style asks for it.

1. **No side classifier.** Left and right flanks cannot be told apart. Auto-
   assignment is now gated off rather than silently wrong, which is the
   correct interim state, not a solution. Until this exists, Stage 3 at
   scale produces a review queue, not a catalogue.
2. **No species classifier.** Same shape. `species_gate='review'` is honest;
   it is not automation.
3. **Authentication is specified, not implemented.** §10.
4. **Ingest is not yet checkpointed.** The job framework and the fast decode
   path are in; `preflight_ingest()` still materialises the file list and
   scans it in one pass inside the request. It is the next thing to do and
   the pattern to copy (`stage3.run_stage3`) is in the tree.
5. **Exports still materialise in memory.** A Camtrap DP zip for 50,000
   frames is built entirely in RAM.
6. **Nothing here has been measured against real Pench data.** Every
   threshold in `config.py` traces back to ATRW zoo tigers. The repo already
   says so; the fix does not change it.

---

*Every measurement in this document was produced by running the code in this
repository against its own seeded demo database. The verification script is
reproduced in `MEGAFIX_APPLY.md`.*
