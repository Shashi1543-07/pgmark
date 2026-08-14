# PUGMARK — audit of the current build, and the revised plan

Audited: `pugmark-v0.1.1` as submitted, 13 Aug 2026.
Reviewed against `docs/BLUEPRINT.md`, `CLAUDE.md`, and findings from the ATRW
paper (Li et al., ACM MM 2020), the LILA BC camera-trap collections, and the
iNaturalist Open Dataset.

**Read §1 and §2 before touching anything.** §3 is the defect list in priority
order. §4 is what the new data sources change. §5 is the merge and renumbering
instructions. §6 is the revised build order.

---

## 1. Verdict

The build is in good shape. Four of five pipeline stages exist, the discipline
held (no SQL outside `repo.py`, no magic numbers outside `config.py`), and the
live suite grew from 49 checks to 123 and passes.

**Three defects have to be fixed before anything else is built:**

| | Defect | Consequence |
|---|---|---|
| **P0-1** | `python-multipart` missing from `requirements.txt` | **The application does not start on a clean machine.** |
| **P0-2** | Stage A averages away the cell grid it just built | **Animals are silently quarantined as blank.** Unrecoverable data loss. |
| **P0-3** | The configured blank threshold is not the threshold in force | The number shown to the officer is 10× off the real one. |

P0-1 kills the demo in the first thirty seconds. P0-2 is the one failure mode
the problem statement singles out by name. Neither is visible from a passing
test suite, which is the point — both passed 123 checks.

---

## 2. What is done well — do not rewrite these

Say this plainly so nobody "fixes" working code under time pressure.

**`edge/pipeline/occupancy.py` is the best file in the repo.** A hand-rolled
Snyder/USGS Transverse Mercator projection reading the zone and hemisphere from
the EPSG code itself, Andrew's monotone-chain hull, shoelace on projected
metres, and — importantly — it returns `None` rather than a degenerate polygon
for fewer than three points or collinear points. It also refuses to be given a
reserve's UTM zone as a constant. This is exactly right; leave it alone.

**`edge/effort.py`** is clean, holds no SQL, and derives cycle periods from run
start times rather than assuming a fixed calendar. `coverage()` returning
`None` for "no baseline" and letting each rule decide what that means is the
correct design.

**`edge/pipeline/alerts.py`** implements all four rules with their confounds,
derived from data rather than hardcoded, with confidence propagation
(`min(id_conf, rule_strength) × coverage`) and deterministic alert IDs.
Suppression reasons are written in plain language a range officer can read.
This is the differentiator and it works.

**`edge/exports/mstripes.py` refuses to fabricate.** Every function raises with
the reason that no real M-STrIPES export has ever been seen. That is the
correct behaviour and it is the sort of judgement that is hard to teach. Keep
it exactly as it is.

**`edge/pipeline/triage.py`** does a real file move, writes `manifest.json`
*before* the database row so restore survives losing the database, and is
honest that Stage B does not exist — frames it is not confident about stay
`pending` rather than being silently reclassified. The architecture is right.
The scoring inside it is not (see P0-2).

---

## 3. Defects

### P0-1 · The application will not start on a clean machine

`edge/app.py:17` imports `File`, `Form` and `UploadFile`, and line 416 declares:

```python
async def apply_bundle_route(file: UploadFile = File(...), actor: str = Form("director")):
```

FastAPI requires `python-multipart` for `Form`/`File` parameters and raises at
**import time**, not request time:

```
RuntimeError: Form data requires "python-multipart" to be installed.
```

`requirements.txt` does not list it. On a laptop that has not happened to
install it, `launcher/run.bat` dies before the browser opens.

This is the environment-assumption bug class: it works on the machine that
built it and fails everywhere else. It also proves a gap in the test suite —
the live suite imports `edge.app` inside a process that already had the package.

**Fix**

1. Add `python-multipart>=0.0.9` to `requirements.txt`.
2. Add a live check that the app imports **in a clean subprocess**:

```python
import subprocess, sys
r = subprocess.run([sys.executable, "-c", "import edge.app"],
                   capture_output=True, text=True)
check("app imports in a fresh interpreter", r.returncode == 0, r.stderr[-200:])
```

3. Before the 17th, on every team laptop: create a fresh virtualenv, install
   from `requirements.txt` only, and run the suite. Anything missing surfaces
   there instead of at 10 a.m. on the 18th.

---

### P0-2 · Stage A throws away the cell grid it just built

`edge/pipeline/triage.py:84`:

```python
score = float(np.mean(np.abs(grid.astype(float) - background))) / 255.0
```

`grid` is the 16×16 cell grid. Taking `np.mean` over all 256 cells collapses it
back into a single global average — which is mathematically almost identical to
averaging the full-resolution frame.

The blueprint (§6.3) says: *compare on a cell grid, not a global mean; a tiger
occupying 4% of the frame barely moves a global average but lights up several
cells.* `_read_grid`'s own docstring repeats that reasoning. Then the caller
averages the cells away, undoing the only reason the grid exists.

**Proof.** With the effective ceiling (see P0-3) at 0.0012, these frames score
as blank and are quarantined:

| Contrast above background | Cells lit | % of frame | Score | Outcome |
|---|---|---|---|---|
| 60 | 1 | 0.4% | 0.00092 | quarantined |
| 25 | 2 | 0.8% | 0.00077 | quarantined |
| 12 | 4 | 1.6% | 0.00074 | quarantined |

Low contrast against a warm background is the *normal* case for night infrared,
which is when most tiger captures happen. 1.6% of a 4000×3000 frame is roughly
a 480×300 pixel region — a whole tiger at moderate distance.

**Why this is the worst possible bug here.** The problem statement is explicit:
irreversible deletion of a misclassified frame destroys irreplaceable field
data. Quarantine makes it recoverable, but only if someone knows to look. A
blank kept costs seconds of review; an animal discarded costs the observation.

**Fix.** Score on a high percentile or the maximum of the cell differences, not
their mean:

```python
cell_diff = np.abs(grid.astype(float) - background) / 255.0
score = float(np.percentile(cell_diff, 98))   # near-max, robust to one hot cell
```

Then re-tune `stage_a_blank_threshold` — the scale changes completely, so the
old value is meaningless against the new statistic.

**Add a unit test that would have caught it.** In `tests/unit/`:

```python
def test_small_subject_is_not_scored_as_blank():
    bg = np.full((16, 16), 100.0)
    for contrast in (12, 25, 60, 140):
        for cells in (1, 2, 4, 8):
            frame = bg.copy()
            frame.flat[:cells] = 100.0 + contrast
            assert score(frame, bg) > BLANK_THRESHOLD, (
                f"{cells} cells at contrast {contrast} scored as blank")
```

This test is worth more than any number on a slide: it is the direct answer to
*"how do you know you aren't throwing away tigers?"*

---

### P0-3 · The threshold on screen is not the threshold in force

Two gates run in series:

```python
conf_blank = max(0.0, min(1.0, 1 - score / cfg.stage_a_blank_threshold))
if score <= cfg.stage_a_blank_threshold and conf_blank >= cfg.quarantine_conf_threshold:
```

`conf_blank >= 0.90` expands to `score <= 0.1 × stage_a_blank_threshold`. The
second gate is 10× stricter than the first, so the first never binds. The
configured `0.012` is inert; the number actually in force is `0.0012`.

The Ops screen renders "Blank if motion under 0.012" from config. That
statement is false, and CLAUDE.md rule 2 exists precisely so an officer can see
what the machine was told. Anyone tuning that value is tuning a dead knob.

**Fix.** One gate, one meaning. Either:

- keep `stage_a_blank_threshold` as *the* score ceiling and drop `conf_blank`
  from the decision (keep it as a reported confidence only), or
- derive `conf_blank` from a separate, explicitly named margin
  (`stage_a_confident_margin`) so both numbers mean something independently.

Whichever you choose, add a live check asserting that the effective ceiling
equals the configured one, so the two can never silently diverge again.

---

### P1-4 · Migration number collision

Two different `0002` migrations now exist:

- `0002_node_identity.sql` — this build (node id + Lamport counter)
- `0002_entities.sql` — my v0.2.0 (the entity model, §4.1)

Both are wanted. `repo.migrate()` keys on the integer prefix, so whichever
applies first blocks the other permanently on any database that has already run
one of them.

**Fix.** Keep `0002_node_identity.sql` as-is (it shipped first and databases
already have it). Renumber the entity migration to `0003_entities.sql`. Nothing
inside it changes.

---

### P1-5 · `node_identity` permits more than one identity

```sql
CREATE TABLE node_identity (
  node_id         TEXT NOT NULL,
  lamport_counter INTEGER NOT NULL DEFAULT 0
);
```

No primary key, no uniqueness, no single-row constraint. Two `INSERT`s — a
retry, a double-run of setup — leave the node with two identities. Every row it
stamps afterwards is ambiguous, and because `origin_node` is what sync merges
on, that corruption spreads to every node it ever syncs with, silently.

**Fix**, as `0004_node_identity_singleton.sql`:

```sql
CREATE TABLE node_identity_new (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  node_id         TEXT NOT NULL CHECK (length(node_id) > 0),
  lamport_counter INTEGER NOT NULL DEFAULT 0
);
INSERT INTO node_identity_new (id, node_id, lamport_counter)
SELECT 1, node_id, lamport_counter FROM node_identity LIMIT 1;
DROP TABLE node_identity;
ALTER TABLE node_identity_new RENAME TO node_identity;
```

`CHECK (id = 1)` makes a second row impossible at the database level rather
than by convention.

---

### P2-6 · Stage A is single-pass and causal

The median window only contains frames *before* the current one, so:

- The first frame at each station (and separately, the first night frame) has
  no background at all and can never be quarantined. Harmless in itself, but it
  means the reported "% removed by Stage A" varies with input ordering — which
  makes the throughput number unreproducible.
- An animal appearing in the first frames at a station poisons the background
  for every frame after it.

This is batch processing, not streaming. A two-pass approach is strictly better
and simpler: pass one computes the median over **all** frames at that station
(robust to an animal appearing in a minority of them), pass two scores every
frame against it. Same cost, no cold start, order-independent.

---

### P2-7 · Stage A has never been measured

The prefilter is written but its accuracy is unknown, because there is no
labelled empty/animal data in the repo. **Blank-detection accuracy with
particular attention to false negatives is an explicit jury criterion.** See
§4.2 for where the data comes from.

---

### P2-8 · Requirement 2 of four has not been started

`edge/pipeline/identify.py` does not exist and `edge/models/` is empty. Flank
cropping, rectification, embedding and matching — the entire individual
identification requirement — is unbuilt. That is fine at this point in the
schedule, and §4.1 explains why it is fortunate that it has not started yet.

### P2-9 · `tests/unit/`, `tests/messy/` and `tests/scenarios/` are empty

All 123 checks live in the live suite. That suite is the right shape, but the
alert engine's correctness argument depends on the scenario suite existing as
its own thing, and P0-2 is exactly the class of bug a unit test catches in
seconds and an end-to-end test never catches at all.

---

## 4. What the new sources change

Three sources arrived after this build started. Full detail in `docs/DATA.md`.

### 4.1 The unit of matching is the entity, not the individual

The ATRW paper establishes that a tiger's left and right flanks carry
**different, unrelated stripe patterns** — not mirror images — and treats each
side of each tiger as a separate **entity**. 92 tigers yield 182 entities.

That ratio is close to two sides per tiger because ATRW was shot in zoos. The
paper's reason for splitting sides is a statement about **the wild**, where
capturing both flanks of one animal is rare. Camera traps are the wild case.

**Consequences for code that does not exist yet — which is why now is the time:**

- The catalogue is keyed on `(ind_id, side)`. Migration `0003_entities.sql`
  adds the `entities` table, `assignments.entity_id`, the
  `single_flank_individuals` view, and `repo.catalogue_for_side()`, which
  **raises** on a bad side rather than trusting the caller.
- An `L` crop is never scored against an `R` catalogue. Enforced in the query.
- A crop of a side never seen for an individual is **unresolvable** — neither a
  match nor evidence of a new tiger. That is a first-class state, not an
  absence.

Had `identify.py` been written first, this would be a rewrite. It is currently
a schema addition.

### 4.2 Three sources, three separate jobs

| Source | Job | Cannot do |
|---|---|---|
| ATRW | re-identification | **no blank frames at all** — every clip has a tiger |
| LILA camera-trap sets | blank detection, real burst structure | no individual identities |
| iNaturalist | the species gate, leopard hard-negatives | no individual identities |

**ATRW cannot validate Stage A.** For P2-7 the data is Caltech Camera Traps
(243,100 images, 140 locations, empty/animal labels, and a **recommended
location-based split**) and Wellington (270,450 images, real 3-shot bursts).

**Split by station, never by image.** Stage A is literally a per-station
background model; validating on stations you tuned on measures memorisation.
Caltech ships a location-based split for exactly this reason.

**The species gate finally has a data source.** A camera-trap detector returns
`animal`; Stage C needs *which* animal. iNaturalist has openly licensed,
labelled photos of tiger, leopard, sloth bear, wild boar, chital, nilgai, dhole
and gaur, including from India. Leopards double as hard negatives — a stripe
matcher handed a spotted cat must refuse, not enrol a phantom.

### 4.3 Which model to build — settled by the paper's own benchmark

| Method | cross-camera mAP |
|---|---|
| Cross-entropy, frozen backbone | 38.1 |
| **Triplet loss (TriHard)** | **47.2** |
| PPbM-a (their 7-part pose model) | 51.7 |

**Build triplet loss. Do not build PPbM.** Their elaborate pose-part model buys
2.8 mAP. That is not a 24-hour trade. Cross-entropy on a frozen backbone is the
fallback if the embedder will not converge.

Other settled parameters:

- Input **256 × 128**, and swap every width/height hyperparameter — tiger boxes
  are horizontal-major, pedestrian re-ID code assumes vertical-major.
- **Rectify on the shoulder–hip quadrilateral, not nose-to-tail.** The paper's
  keypoint variance table shows shoulders (4.1, 6.9) and hips (9.1) are the
  most reliably annotated points and nose (69.0) and tail-root (46.7) are the
  two worst. The intuitive axis is the noisy one. This supersedes the
  PCA-on-mask approach in `BLUEPRINT.md` §7.1.
- If a pose model is trained at all: **HRNet** (AP 86.9). **OpenPose failed to
  converge** on the tiger skeleton — a documented dead end.

### 4.4 Report cross-camera numbers, and exclude same-event frames

Single-camera mAP runs ~24 points above cross-camera across every method. Our
task *is* cross-station. Reporting the single-camera number overstates by about
half again, and a jury member who has read the paper will know.

Separately: the paper excludes temporally adjacent frames from query results.
We already group bursts into `events`. **The evaluation must exclude same-event
frames from the gallery**, or the matcher is being graded on matching a frame
to the one taken two seconds later. That inflates toward 95% and means nothing.

### 4.5 Licence, and one line for the pitch

ATRW is **CC BY-NC-SA 4.0**; images are owned by MakerCollider and WWF.
Non-commercial, share-alike. Record it in `docs/MODEL_CHOICES.md` and say it
before being asked: *"trained on CC BY-NC-SA research data; a production
deployment retrains on the reserve's own catalogue."*

The re-ID subset is 132 MB + 90 MB. It downloads in an evening.

---

## 5. Merge instructions

Apply in this order. Run `python -m tests.live.test_routes` after each step.

1. **`requirements.txt`** — add `python-multipart>=0.0.9`. Add the
   fresh-subprocess import check to the live suite. (P0-1)
2. **`edge/pipeline/triage.py`** — replace the mean with a 98th-percentile cell
   score, re-tune `stage_a_blank_threshold`, and add the small-subject unit
   test. (P0-2)
3. **`edge/config.py` + `triage.py`** — collapse the double gate so the
   configured threshold is the one in force; add a live check asserting they
   match. (P0-3)
4. **Renumber** the entity migration to `0003_entities.sql`. Do not touch
   `0002_node_identity.sql`. (P1-4)
5. **Add `0004_node_identity_singleton.sql`.** (P1-5)
6. **Merge `repo.py`** — take the current file and add `rebuild_entities()`,
   `entities()`, `single_flank()`, `catalogue_for_side()`. All four are
   additive; nothing existing changes.
7. **`tools/seed_demo.py`** — call `repo.rebuild_entities()` after assignments
   are inserted, and give some individuals only one flank so
   `single_flank_individuals` is actually exercised. Roughly six of thirteen
   matches the field expectation.
8. **`CLAUDE.md`** — replace rule 6 with the entity version, and add a pointer
   to `docs/DATA.md`.
9. **Copy in `docs/DATA.md`.**

Expected result: 123 checks plus the new ones, all green.

---

## 6. Revised build order

Everything below assumes §5 is done first.

**Next — finish what is already standing up**

1. **Two-pass Stage A** (P2-6). Small change, removes the cold start, makes the
   throughput number reproducible.
2. **`tests/scenarios/`** — move the eight alert scenarios into their own suite
   driven by a synthetic history generator, so alert correctness can be proved
   without the CV pipeline. This is the table that answers *"are your alerts
   noisy?"*
3. **`tests/messy/`** — the fuzz corpus from `BLUEPRINT.md` §13 layer 2:
   zero-byte file, truncated JPEG, EXIF year 1970, two camera serials in one
   folder, BOM in the manifest CSV. Assertion: the run completes and every
   problem appears in preflight.

**Then — the missing requirement**

4. **Blank-detection measurement.** Pull Caltech CT, split **by location**,
   report precision, recall, threshold sweep, and lead with the false-negative
   rate. This closes P2-7 and produces a jury-facing number.
5. **`edge/pipeline/identify.py`**, in this order:
   flank crop → shoulder–hip rectification (§4.3) → quality gate that refuses
   below threshold → embedding → `catalogue_for_side()` match → three-way
   decision (auto / review / provisional enrol).
6. **Species gate** on iNaturalist data, if time remains. Without it, be
   explicit that "animal" is being treated as "tiger" — do not let that
   assumption go unstated.

**Do not build**: PPbM, OpenPose, a vector database, the central tier.

---

## 7. Definition of done, per item

1. It runs against the demo database.
2. It has at least one check in the live suite asserting its **effect**, not its
   existence.
3. `python -m tests.live.test_routes` is green and the count is quoted.
4. Anything deliberately not done is written down, in the module docstring,
   the way `edge/exports/mstripes.py` already does it.

---

## 8. The two lessons behind these defects

**A passing suite is not a working system.** All 123 checks passed on a build
that would not start on a clean machine and was quarantining animals as blanks.
Neither failure was invisible — both were simply not asked about. When adding a
feature, ask what it would look like if it were subtly wrong, and write *that*
assertion.

**Docstrings drift from code.** `_read_grid`'s docstring correctly explains why
the cell grid exists. The line that consumes it discards that reasoning eight
lines later. When a comment states a principle, the test should assert the
principle — not the comment.
