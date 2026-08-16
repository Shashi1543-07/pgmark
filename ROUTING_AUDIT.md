# PUGMARK — Routing & Wiring Audit

**Audited:** `pugmark-v0.1.1-clean-source` (post field-hardening pass)
**Method:** runtime route-table introspection + live `TestClient` calls against
`data/pugmark.db` + static cross-reference of every `@app.*` decorator against
every URL string in `edge/ui/`. Every claim below was executed, not inferred.

## Verdict

The hardening pass built the features. It did not wire them.

| | |
|---|---|
| API routes registered | **96** |
| Routes any UI screen actually calls | **62** |
| Routes unreachable from the UI | **34** |
| Server starts without `torch` installed | **No** |
| Reserve boundaries can reach the map | **No** (one missing import) |
| Non-admin can manage stations | **No** (role that doesn't exist) |
| `/api/jobs/{id}/resume` works for ingest jobs | **No** (function doesn't exist) |

Nothing here needs new architecture. It needs ~40 lines of wiring and one
decision about which of the two station APIs survives.

---

# P0 — blockers. Fix in this order.

## P0-1. The server will not start on a machine without torch

**Chain:** `edge/app.py:32` `from edge.pipeline import ingest`
→ `edge/pipeline/ingest.py:33` `from edge.pipeline.device import get_device_manager`
→ `edge/pipeline/device.py:13` `import torch` (module scope).

`edge/app.py:33` adds a second chain into `triage` → `numpy`, `PIL`.

Reproduce:

```bash
python3 -c "from edge.app import app"
# ModuleNotFoundError: No module named 'torch'
```

This is the exact failure the `/api/health/ready` docstring claims it fixed:

> *"in v0.1.1 this also stopped the whole server from starting, because
> edge/app.py imported the detector at module scope"*

It still does, one import deeper. A readiness endpoint that reports torch is
broken is worthless if the process cannot boot to serve it.

**Fix — `edge/pipeline/device.py`:** delete the module-scope `import torch`.
Move it inside `get_device_manager()` and any other function that touches it.
Keep `DevicePlan.device` annotated as `str` or use
`from __future__ import annotations` (already present) so the dataclass
annotation `torch.device` does not evaluate at import time.

**Fix — `edge/app.py:32-33`:** drop both module-scope pipeline imports. Every
route that uses them already re-imports locally (see `identify_run_image`,
which does `from edge.pipeline import identify_upload` inside the handler).
Do the same for `ingest` and `triage_pipeline`.

**Verify:**
```bash
pip uninstall -y torch && python3 -c "from edge.app import app; print('boots')"
curl -s localhost:7860/api/health/ready | jq '.checks[] | select(.check=="pytorch runtime")'
```

## P0-2. `json` is never imported in `routes_scale.py` — the map can never draw boundaries

`edge/routes_scale.py:302` calls `json.loads(...)`. The module imports
`importlib.util`, `shutil`, `Path`, fastapi bits, `config`, `jobs`, `repo`,
`repo_ext`. **No `json`.**

The `NameError` is eaten by `except Exception: pass` at lines 306-307, so
`/api/runs/{run_id}/map` silently returns `boundaries: {}` forever.

Proven live:

```
200 PUT  /api/reserves/PENCH-MH/boundaries   -> boundary_geojson saved
200 GET  /api/reserves/PENCH-MH/boundaries   -> returns the polygon
200 GET  /api/runs/{run_id}/map              -> boundaries: {}      <-- here
```

```python
>>> import edge.routes_scale as rs; hasattr(rs, "json")
False
```

This kills the entire P4 map requirement. `edge/ui/map.js` is fully built for
it — `boundaries.core_geojson` / `buffer_geojson` / `corridor_geojson` are
consumed at lines 169-171 and rendered as styled layers at 248-257, with a
working layer toggle. Backend storage works. Frontend rendering works. One
missing import in the middle.

**Fix:** add `import json` to the top of `edge/routes_scale.py`.

**Then fix the swallow that hid it** — replace `except Exception: pass` at
306-307 with a logged, surfaced failure:

```python
except (ValueError, TypeError, AttributeError) as exc:
    boundaries = {"error": f"boundary_geojson unparseable: {exc}"}
```

## P0-3. Station management returns 403 to every role except admin

`edge/app.py` lines **818, 826, 837**:

```python
user: dict = Depends(require_role("admin", "officer"))
```

`"officer"` is not a role. `config.CONFIG.auth.roles` is
`('field', 'biologist', 'director', 'analyst', 'admin')`.

Lines **848, 859** are worse:

```python
require_role(*config.PERMISSIONS.get("admin", ["admin", "officer"]))
```

`"admin"` is not a key in `PERMISSIONS` (keys are actions: `ingest_manage`,
`pipeline_trigger`, …), so this always falls through to the same broken
default.

Proven with a `director` account:

```
403 POST   /api/stations?reserve_id=PENCH-MH
403 PUT    /api/stations/PN-C-001
403 DELETE /api/stations/D-1
403 POST   /api/stations/import-csv
403 POST   /api/stations/import-geojson
--- identical operations on the dead twin routes ---
200 POST   /api/reserves/PENCH-MH/stations
200 POST   /api/reserves/PENCH-MH/stations/import/csv
```

A forest officer logged in as `director` clicks **Add Station** and gets
"insufficient permission". The working version of that endpoint exists and no
screen calls it.

**Fix:** replace all five with
`Depends(require_role(*config.PERMISSIONS["ingest_manage"]))`.

## P0-4. `ingest.resume_ingest` does not exist

`edge/routes_scale.py:150`:

```python
elif job.kind == "ingest":
    from edge.pipeline import ingest
    jobs.resume(job_id, lambda jid: ingest.resume_ingest(job.run_id, jid))
```

`edge/pipeline/ingest.py` defines: `resource_preflight`, `preflight_ingest`,
`confirm_ingest`, `match_station_multisignal`, `match_station`, and private
helpers. **No `resume_ingest`.** → `AttributeError` → 500. The UI calls
`/api/jobs/${j.job_id}/resume` (app.js), so this is reachable.

**Worse:** nothing anywhere creates a job of `kind="ingest"`.

```bash
grep -rn 'jobs.create("ingest"' edge/    # zero hits
```

Ingest is still fully synchronous inside the HTTP request:
`POST /api/runs` → `preflight_ingest()` inline;
`POST /api/runs/{id}/confirm` → `confirm_ingest()` inline.

That is the 1M-image blocker, unmoved. A 500k-frame card times out the browser
mid-scan with the run in a state nothing recorded.

**Fix, two parts:**
1. Wrap ingest in the job engine — `jobs.create("ingest", ...)` +
   `jobs.start(...)` in `POST /api/runs/{run_id}/confirm`, returning `job_id`
   like `/pipeline` does. Update the UI wizard to poll instead of awaiting.
2. Implement `ingest.resume_ingest(run_id, job_id)` reading `job.cursor`, or
   delete the `elif` branch and return 400. **Do not leave a route calling a
   function that isn't there.**

## P0-5. Two complete, parallel station APIs. The UI is wired to the broken one.

| | `edge/app.py` family | `edge/routes_scale.py` family |
|---|---|---|
| Paths | `/api/stations*` | `/api/reserves/{rid}/stations*` |
| Called by UI | **yes** | never |
| Permissions | admin only (broken, P0-3) | `ingest_manage` (correct) |
| CSV payload key | `csv_text` | `csv` |
| GeoJSON payload key | `geojson_text` | `geojson` |
| `reserve_id` passed as | query param | path param |

Both call the same `repo_ext.create_station` / `import_stations_csv`. This is
pure duplication with divergent contracts — the exact shape that produces
"works in Postman, 403 in the app" bug reports.

**Decision required. Recommended:** keep the reserve-scoped family (it is
consistent with `/api/reserves/{rid}/boundaries` and `/cross-flank`, which have
no flat twin), delete the five flat routes from `edge/app.py`, and update
`edge/ui/app.js` lines **2655, 2658, 2675, 2717, 2736, 2778** to the new paths
and payload keys.

Keep exactly one. Do not "fix both."

## P0-6. `/api/health/ready` reports a side classifier that exists as "not implemented"

`edge/routes_scale.py:406-411` hardcodes:

```python
add("side classifier", False,
    "not implemented. Every keypoint prediction is labelled 'right' ...")
```

But `edge/pipeline/classifiers.py` implements it:
`SIDE_LABELS = ("L", "R", "UNKNOWN")`, `classify_side()`,
`classify_side_many()`, and `config.SIDE_MODEL_PATH` →
`edge/models/side/flank_side_classifier.ts`.

Two further defects in the same block:

- `config.CONFIG.identify.require_side_classifier` **does not exist.** Actual
  fields: `t_high, t_low, ensemble_embed_weight, top_k_candidates, min_quality,
  min_crop_pixels, enforce_side_separation, target_species,
  rect_body_depth_ratio, rect_margin_ratio, cross_flank_window_s`.
  `getattr(..., True)` defaults True → `auto_assign_enabled` is **permanently
  `False`** regardless of configuration.
- Readiness never probes `SPECIES_MODEL_PATH` or `SIDE_MODEL_PATH` at all —
  the two P0 models from the hardening brief have no readiness check.

**Fix:**

```python
side_ok = config.SIDE_MODEL_PATH.exists()
add("side classifier", side_ok,
    "L/R/UNKNOWN classifier from edge/models/side" if side_ok else
    "model absent — automatic side assignment disabled, frames route to review",
    "install the signed offline model bundle", blocking=False)

sp_ok = config.SPECIES_MODEL_PATH.exists()
add("species classifier", sp_ok,
    "tiger/non-tiger gate from edge/models/species" if sp_ok else
    "model absent — every 'animal' detection would reach the identity pipeline",
    "install the signed offline model bundle")
```

and use `enforce_side_separation` (which exists) for `auto_assign_enabled`, or
add `require_side_classifier` to the `Identify` config dataclass. Pick one and
make the config and the reader agree.

---

# P1 — the 34 unreachable routes

Built, tested, registered, and callable by nothing. Grouped by what it costs.

### Cluster A — the whole Ops tab is decorative

An **Ops tab exists** in `index.html` and `RENDER.ops` calls exactly one route,
`/api/ops?reserve_id=`. These five are dead:

```
GET  /api/health              GET  /api/ops/integrity
GET  /api/health/ready        POST /api/ops/checkpoint
POST /api/ops/backup          GET  /api/ops/jobs
```

`/api/health/ready` is the single most valuable unwired route in the codebase.
It probes the database, torch, detector weights, embedder weights, keypoints,
disk space and stale jobs, and returns a `fix` string per failure. On a judge's
laptop with no model bundle, the app currently fails deep in Stage B with a
stack trace instead of saying "detector weights missing, run X".

**Wire it into `RENDER.ops` as a preflight panel, and gate the Scan button on
`ready === false`.**

`POST /api/ops/backup` is also dead. One laptop, one SQLite file, one season of
identifications, quarantine that physically moves originals — and no backup
button.

### Cluster B — cross-flank association has zero UI

```
GET  /api/reserves/{reserve_id}/cross-flank
POST /api/cross-flank/{assoc_id}/confirm
POST /api/cross-flank/{assoc_id}/reject
```

`repo_ext.create_cross_flank_candidate` writes candidates. Nothing reads them.
This is hard rule #4 from the hardening brief — "never auto-enrol a new
individual because the opposite flank has no match" — implemented on the write
side and unreviewable on the read side. Candidates accumulate invisibly.

**Needs a new `#crossflank` view, or a section in `#review`.**

### Cluster C — catalogue state has no screen

```
GET /api/catalogue/health          GET /api/individuals/provisional
POST /api/individuals/{id}/merge   POST /api/individuals/rebuild-entities
```

`/api/catalogue/health` returns `single_flank` / `no_flank` / `both_flanks`
buckets — CLAUDE.md rule 6's "first-class state". No screen asks.
`merge` is the only way to undo a double-enrolment and has no button.

### Cluster D — scale features present, UI still on the unscaled path

| Dead route | UI still calls |
|---|---|
| `GET /api/runs/{id}/images/page` | `GET /api/runs/{id}/images?status=subject` (unpaginated) |
| `GET /api/review/page` | `GET /api/review?limit=50` |
| `POST /api/review/{qid}/claim` | — (two-reviewer race still live) |
| `POST /api/review/{qid}/release` | — |
| `GET /api/runs/{id}/dead-letters` | — |
| `GET /api/runs/{id}/telemetry` | — |
| `GET /api/runs/{id}/status-counts` | — |
| `POST /api/runs/preflight-resources` | — (disk preflight never shown) |
| `GET /api/runs/{id}/preflight` | — (never re-fetched after Scan) |

Pagination was written specifically to stop ~4,000 DOM rows on a 50k import.
The UI never switched. Same for the claim/release locking that was written to
fix the two-tab race.

### Cluster E — still-unwired from the previous round

```
POST /api/alerts/{alert_id}/acknowledge
```

`routes_scale.py`'s own header docstring lists this as a route with "no button
anywhere" and says it matters most. **It still has no button.** `RENDER.alerts`
renders severity filters and no acknowledge control. Alerts can be read, never
cleared.

```
POST /api/runs/{id}/stage3     POST /api/runs/{id}/postprocess
GET  /api/reserves/{rid}/boundaries    PUT /api/reserves/{rid}/boundaries
GET  /api/reserves/{rid}/stations/export/geojson
```

`PUT /boundaries` is the only way to load reserve geometry, and there is no
screen for it — so even after P0-2 is fixed, the map has nothing to draw until
someone POSTs by hand. **Add a boundary import control to the Stations tab
(paste GeoJSON, same pattern as station import).**

---

# P2 — correctness and hygiene

## The map silently substitutes another run's data

`edge/routes_scale.py:268-280` and `327-336`:

```python
if not occ:
    for other_run in repo.runs(run["reserve_id"]):
        if other_run["run_id"] != run_id:
            other_occ = repo.occupancy(other_run["run_id"])
            if other_occ:
                occ = other_occ      # <-- silently shows a different cycle
                break
```

Same pattern for `prior` and `events`. A run with no occupancy renders another
run's tigers with no indication in the payload. This directly violates the
project's own rule — *the system may refuse an answer, it may not invent one* —
and it will produce a confidently wrong demo.

**Fix:** delete all three fallbacks. Return empty arrays plus
`"empty_reason": "occupancy has not been computed for this run"` and have
`map.js` render that string.

## `repo.py` star-import shadows 7 functions

`edge/db/repo.py:1520` — `from edge.db.repo_ext import *`, and `repo_ext.py`
declares **no `__all__`**. These exist in both files; the `repo_ext` version
silently wins because the import is at the bottom:

```
confirm_cross_flank        cross_flank_candidates     run_status_counts
create_cross_flank_candidate   create_station         run_telemetry
multi_signal_station_score
```

If the two implementations ever diverge, the `repo.py` copy is dead code that
still reads as live. **Add `__all__` to `repo_ext.py` and delete the duplicate
definitions from `repo.py`** — do not leave two.

Related: **18 call sites** in the route layer say `repo.X` for functions that
only exist in `repo_ext` (`repo.backup`, `repo.catalogue_health`,
`repo.merge_individual`, `repo.prior_centroids`, `repo.run_period`,
`repo.set_reserve_boundaries`, …). They resolve only through the star import.
Working, but one `__all__` away from a mass `AttributeError`.

## Silent-failure swallows

| Location | Hides |
|---|---|
| `routes_scale.py:306` | the `json` NameError (P0-2) |
| `routes_scale.py:323` | any failure of the map events SQL → `events: []` |
| `routes_scale.py:334` | the fallback query failure |
| `app.py:807-809` | `stations_with_state` failing → silently degrades to `repo.stations`, station status glyphs go blank with no error |
| `app.py:659-660` | `postprocess.after_review_decision` failing after a review decision |

The map events SQL is currently **valid** (verified against the live schema),
so :323 hides nothing today — but it will hide the next schema change.
Narrow every one of these to a specific exception type and surface it.

## Shipped database has no intelligence in it

```
images 46 | detections 25 | flank_crops 17
assignments 0 | individuals 0 | events 0 | occupancy 0 | alerts 0
```

First launch shows an empty Map, empty Alerts, empty Tigers with no explanation.
Either ship a fully-seeded `data/pugmark.db` or make the empty state on each of
those three screens say which pipeline stage has not run yet, with the button
to run it.

## `/api/identify/upload` drops two fields

`app.js:1098-1102` sends `file`, `reserve_id`, `actor`. The route signature is
`(file, reserve_id, station_id)`. So `actor` is **ignored** (actor comes from
the session — fine, remove it from the form) and `station_id` is **never sent**,
so every manual upload is unattributed to a station and cannot contribute to
occupancy. Add the station picker to the Identify tab.

## Minor

- `app.py:22` imports `Query`, never used.
- `@app.exception_handler(404)` returns `{"error": ...}` while `HTTPException`
  returns `{"detail": ...}`. `api()` handles both, but pick one shape.
- `@app.on_event("startup")` is deprecated in the installed FastAPI (0.141) —
  migrate to a `lifespan` handler.
- `POST /api/stations` takes `data: dict` body + `reserve_id` **query** param.
  Works, but is the only route in the codebase shaped that way.

---

# Fix order

```
1. edge/pipeline/device.py   remove module-scope `import torch`      [P0-1]
2. edge/app.py:32-33         remove module-scope pipeline imports    [P0-1]
3. edge/routes_scale.py      add `import json`                       [P0-2]
4. edge/app.py:818,826,837,848,859   -> PERMISSIONS["ingest_manage"] [P0-3]
5. routes_scale.py:150       implement or remove resume_ingest       [P0-4]
6. choose ONE station API, delete the other, repoint app.js          [P0-5]
7. routes_scale.py:406       probe SIDE/SPECIES paths, drop the lie  [P0-6]
8. routes_scale.py:268-336   delete the cross-run fallbacks          [P2]
9. repo_ext.py               add __all__, de-duplicate the 7         [P2]
10. wire Cluster A (readiness+ops), then E (acknowledge, boundaries),
    then D (pagination, claim/release), then B and C (new views)
```

# Verification — run all of these, they must all pass

```bash
# 1. boots with no ML stack at all
pip uninstall -y torch torchvision ultralytics
python3 -c "from edge.app import app; print('boots without torch')"
curl -s localhost:7860/api/health/ready | jq .ready

# 2. boundaries survive the round trip to the map
curl -X PUT localhost:7860/api/reserves/PENCH-MH/boundaries \
     -H 'Content-Type: application/json' \
     -d '{"core_geojson":{"type":"Polygon","coordinates":[[[79,21.6],[79.5,21.6],[79.5,22],[79,22],[79,21.6]]]}}'
curl -s localhost:7860/api/runs/<run_id>/map | jq .boundaries
# must NOT be {}

# 3. a director can manage stations
#    log in as a director account, then POST/PUT/DELETE /api/stations* -> 200, not 403

# 4. no route calls a function that does not exist
python3 - <<'EOF'
import re, pathlib, importlib
src = pathlib.Path("edge/routes_scale.py").read_text() + pathlib.Path("edge/app.py").read_text()
for mod in ["edge.pipeline.ingest","edge.pipeline.triage","edge.pipeline.stage3",
            "edge.pipeline.postprocess","edge.jobs"]:
    m = importlib.import_module(mod); short = mod.split(".")[-1]
    for name in set(re.findall(rf"\b{short}\.([a-z_]+)\s*\(", src)):
        assert hasattr(m, name), f"MISSING {mod}.{name}"
print("all route -> module references resolve")
EOF

# 5. dead-route count must fall
#    re-run the app.py/routes_scale vs edge/ui/ cross-reference; 34 must trend to 0
```

**Definition of done for this pass:** the server boots with no ML dependencies
installed and says so usefully; the map draws a reserve boundary; a director can
add a station; and no route in the table is unreachable from the UI without an
explicit `# intentionally API-only` comment on the line above it.
