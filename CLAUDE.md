# CLAUDE.md

Context for any agent session working in this repo. Read this first.

## What this is

Pugmark — automated camera-trap triage and individual tiger movement
intelligence for Pench Tiger Reserve. Built for the Viksit Nagpur Hackathon
2026 (Forest & Wildlife theme, 17–18 August). Full design in
`docs/BLUEPRINT.md` — read §1 (landscape), §9 (alert engine) before making
architectural decisions. `docs/DATA.md` covers the three external data
sources (ATRW, LILA camera-trap collections, iNaturalist) and what each is
and is not for — read it before touching re-identification or Stage A
evaluation. `docs/AUDIT_AND_REVISED_PLAN.md` is the record of what was
found wrong in v0.1.1 and why the fixes look the way they do.

Deployment target: **an ordinary laptop at a forest range office, with no
GPU and no internet.** Every decision follows from that.

## Commands

```
python -m tools.seed_demo --reset          # rebuild the demo database
python -m uvicorn edge.app:app --port 7860 # run  → http://127.0.0.1:7860
python -m tests.live.test_routes           # verify: 42 checks, must be 42/42
```

Run from the repo root. These are `python -m` module invocations.

## Rules that must not be broken

These are not style preferences. Each one exists because breaking it causes a
specific failure that is hard to detect later.

1. **All SQL lives in `edge/db/repo.py`.** No SQL anywhere else. When a query
   is wrong there is one place to look.
2. **All thresholds live in `edge/config.py`.** No magic numbers in pipeline
   modules. The active config is written into `runs.config` so any result can
   be explained years later.
3. **Nothing in `edge/ui/` may reference a URL off this machine.** No CDN, no
   webfont fetch, no map tiles. `tests/live/test_routes.py` greps for this and
   fails the build. A grey map at the demo is a lost hackathon.
4. **`audit_log` is append-only**, enforced by SQL triggers. Never write an
   UPDATE or DELETE against it.
5. **Corrections supersede, never overwrite.** Set `assignments.superseded_by`
   and insert a new row. The record of who thought what, when, on what
   evidence, must survive being corrected.
6. **The unit of matching is the entity, not the individual.** A tiger's
   left and right flanks carry different, unrelated stripe patterns, and
   in the wild it is rare to capture both sides of the same animal — so
   one side of one tiger is an `entity` (`edge/db/migrations/0003_entities.sql`,
   `docs/DATA.md` §1), and the catalogue is keyed on `(ind_id, side)`.
   Match through `repo.catalogue_for_side()`, which raises on anything
   other than `'L'`/`'R'` — never write a raw query that could silently
   score an `L` crop against an `R` catalogue. A crop of a side never seen
   for an individual (`repo.single_flank()`) is unresolvable — neither a
   match nor evidence of a new tiger — and that is a first-class state,
   not an absence.
7. **Role gating is enforced server-side in `edge/app.py`**, never in the UI.
   A UI check is a suggestion; a server check is a control.
8. **Refusing to answer is a valid output.** The quality gate that won't match
   a bad crop, the absence alert that won't fire without effort coverage, the
   review queue that won't guess — these are features. Do not "fix" them into
   always producing a result.

## Built vs not built

Working end to end: data contract and migrations · run/preflight reporting ·
motion prefilter (Stage A, two-pass, order-independent) with reversible
quarantine · Stage B animal/person/vehicle detector (MegaDetector V6,
MDV6-mit-yolov9-c, MIT-licensed — `edge/pipeline/detector.py`, see
`docs/MODEL_CHOICES.md`), person frames blurred and routed to
`persons_restricted` rather than the tiger pipeline · a trained 2-keypoint
shoulder/hip regressor (Ultralytics YOLO11-pose, deliberately AGPL-3.0 —
`edge/pipeline/keypoints.py`, see `docs/MODEL_CHOICES.md` for that
licence trade-off recorded on purpose) · flank rectification, TriHard
triplet-loss embedding, and side-catalogue matching
(`edge/pipeline/identify.py`) · a full raw-photo-to-catalogue route and UI
screen (`POST /api/identify/upload`, "Identify a photo" —
`edge/pipeline/identify_upload.py`), landing real rows in the catalogue,
review queue, and audit log · individual catalogue and entity model ·
review queue · occupancy + offline SVG map · alert surface with
suppressions, proven both end to end and from generated data alone
(`tests/scenarios/`) · append-only audit with role-gated location reads ·
GeoJSON/CSV/Camtrap DP exports · edge-to-edge sync bundles · ops/drift.

**A real, unfixed limitation in the keypoint regressor above: it does
not determine true left/right flank side.** Every prediction is labelled
"right_shoulder"/"right_hip" by convention, regardless of which flank is
actually showing — confirmed empirically (`docs/RESULTS.md`'s "wild"
evaluation: every held-out image came back side='R', none 'L'). On a
real deployment, a genuine left-flank photo is silently compared against
the right-side catalogue. This was the literal scope asked for ("near-side
shoulder and hip only"), not a bug relative to it — but it means the
catalogue-matching pipeline is not yet trustworthy for production use
without a side classifier, which does not exist. See
`edge/pipeline/keypoints.py`'s module docstring.

Not built (interfaces defined, stubs honest): the central sync tier ·
M-STrIPES integration (`edge/exports/mstripes.py` refuses and says why,
on purpose).

Do not make a stub claim to work. `/api/sync/status` returns `enabled: false`
with a reason on purpose.

## The alert engine spec

`tools/seed_demo.py` plants eight scenarios. Four fire, four must be
suppressed. **They are the specification** — the engine in
`edge/pipeline/alerts.py` is correct when it reproduces all eight from data
rather than from the seed script's hardcoded rows.

Two independent proofs of this exist and both must stay green:
`tools/seed_demo.py` plus the live suite proves it end to end, through the
real pipeline (ingest, triage, matching, occupancy). `python -m
tests.scenarios.test_alert_scenarios` proves the engine alone, from a tiny
synthetic reserve built directly against `edge/db/repo.py` — no images, no
ingest, no triage — so a bug in `alerts.py` itself can never hide behind a
coincidence in the demo seed's own data.

| Scenario | Expected |
|---|---|
| PENCH-004 first capture at a buffer station near a village | `buffer_ward`, severity `act` |
| PENCH-004 centroid moved 4.1 km, past the 2.36 km core limit | `centroid_shift` fires |
| PENCH-002 absent, cameras in its range worked all cycle | `absence` fires |
| PENCH-011 first use of a station active since 2025 | `new_station` fires |
| PENCH-007 absent, but PN-C-008/009 died on day 4 | **suppressed** — effort coverage 0.31 |
| PENCH-009 at PN-C-015, installed this cycle | **suppressed** — camera arrived, tiger didn't move |
| PENCH-005 centroid moved but only 2 events | **suppressed** — below the 5-event minimum |
| PENCH-P-001 in the buffer at ID confidence 0.44 | **suppressed** — an alert can't exceed the ID beneath it |

## Verification

Never report a task finished without running `python -m tests.live.test_routes`
and quoting the result. A route that is defined but never wired passes every
static check and fails in front of a jury. Assert the **effect**, not the
existence.

When you add a feature, add its check to the live suite in the same change.

## Working style for this project

Diagnose before patching — reproduce the failure first, then fix. Say plainly
what you did not do and why. If a design decision in `docs/BLUEPRINT.md` looks
wrong, argue against it rather than quietly deviating.
