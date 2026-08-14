# Agent task sequence

How to drive the fixes in `docs/AUDIT_AND_REVISED_PLAN.md` through Claude Code.

**Use one prompt per session, in order.** Do not paste several at once — an
agent handed nine tasks does all nine badly, and each of these has a
verification step whose result decides whether the next one is safe to start.

After every prompt: **the live suite must be green and the count must go up.**
If the count is the same, the agent added behaviour without adding a check —
send it back.

---

## Step 0 — put the files in place (you, 30 seconds)

```
docs/AUDIT_AND_REVISED_PLAN.md          ← the audit
docs/DATA.md                            ← datasets, model choice, evaluation
edge/db/migrations/0003_entities.sql    ← already renumbered, do not rename
```

Then commit, so every later change is a reviewable diff:

```
git add -A && git commit -m "Add audit, data plan, entity migration"
```

Do **not** apply the migration by hand. `repo.migrate()` runs it at startup.

---

## Prompt 1 — the app does not start on a clean machine

> Read `docs/AUDIT_AND_REVISED_PLAN.md` §3 P0-1.
>
> `edge/app.py` uses `UploadFile`/`File`/`Form`, so FastAPI raises at import
> time without `python-multipart`, and that package is not in
> `requirements.txt`. The app therefore fails to start on any machine that
> doesn't already have it. All 123 checks pass anyway, because the test process
> already had the package installed.
>
> 1. Add `python-multipart>=0.0.9` to `requirements.txt`.
> 2. Add a check to `tests/live/test_routes.py` that runs
>    `python -c "import edge.app"` in a **fresh subprocess** and asserts the
>    exit code is 0. It must fail if the dependency is missing — verify that by
>    temporarily uninstalling it, running the check, and confirming it goes red
>    before you reinstall.
>
> Then run `python -m tests.live.test_routes` and quote the result.

*Why this one first: it is small, and proving the test actually goes red
before it goes green establishes the working rhythm for everything after.*

---

## Prompt 2 — Stage A is quarantining animals as blank

> Read `docs/AUDIT_AND_REVISED_PLAN.md` §3 P0-2 and P0-3. This is the most
> serious defect in the codebase.
>
> `edge/pipeline/triage.py:84` computes
> `score = np.mean(np.abs(grid - background))` across all 256 cells of the
> 16×16 grid. Averaging the cells collapses them back into a global mean, which
> undoes the only reason the grid exists — `_read_grid`'s own docstring states
> that reasoning and the caller discards it eight lines later. A low-contrast
> animal covering up to 1.6% of the frame currently scores as blank and gets
> quarantined. Low contrast is the normal case for night infrared.
>
> Separately, the double gate means `conf_blank >= quarantine_conf_threshold`
> implies `score <= 0.1 × stage_a_blank_threshold`. The configured threshold is
> inert and the Ops screen displays a number that is 10× off the one in force.
>
> 1. Score on a high percentile of the cell differences (start with the 98th),
>    not their mean.
> 2. Re-tune `stage_a_blank_threshold` — the statistic changed, so the old
>    value means nothing. State how you chose the new one.
> 3. Collapse the double gate so the configured threshold is the threshold in
>    force. If you keep a confidence figure, derive it from a separately named
>    margin so both numbers mean something independently.
> 4. Create `tests/unit/test_triage_scoring.py` with the small-subject table
>    from the audit: for contrasts 12/25/60/140 and 1/2/4/8 lit cells, assert
>    every case scores **above** the blank threshold. Run it against the old
>    scoring first and confirm it fails, then against the new one.
> 5. Add a live check asserting the effective ceiling equals the configured
>    value, so the two can never silently diverge again.
>
> Do not change the quarantine file handling, the manifest, or the restore
> path — those are correct. Only the scoring and the gate.
>
> Run both suites and quote both results.

---

## Prompt 3 — migrations

> Read `docs/AUDIT_AND_REVISED_PLAN.md` §3 P1-4 and P1-5.
>
> `edge/db/migrations/0003_entities.sql` is already in the repo, renumbered to
> avoid colliding with your `0002_node_identity.sql`. Leave `0002` exactly as
> it is; databases in the field have already applied it.
>
> `node_identity` has no primary key and no single-row constraint, so two rows
> are possible. `origin_node` is what sync merges on, so an ambiguous node id
> corrupts every node this one ever syncs with, silently.
>
> 1. Add `edge/db/migrations/0004_node_identity_singleton.sql` using the
>    create-copy-drop-rename pattern in the audit, with
>    `id INTEGER PRIMARY KEY CHECK (id = 1)`.
> 2. Run `python -m tools.seed_demo --reset` and confirm the schema version
>    reaches 4.
> 3. Add live checks: the schema version is 4; a second `INSERT` into
>    `node_identity` raises; and the `entities` table exists.
>
> Run the suite and quote the result.

---

## Prompt 4 — the entity model

> Read `docs/DATA.md` §1 and `docs/AUDIT_AND_REVISED_PLAN.md` §4.1.
>
> The ATRW paper establishes that the unit of re-identification is the
> **entity** — one side of one tiger — not the individual. Left and right flank
> patterns are different and unrelated. Migration 0003 has already added the
> `entities` table, `assignments.entity_id`, and the
> `single_flank_individuals` view.
>
> Wire it up:
>
> 1. Add to `edge/db/repo.py` (all four are additive; change nothing existing):
>    - `rebuild_entities()` — derive entities from confirmed assignments,
>      excluding crops whose side is `unknown`. Safe to re-run.
>    - `entities(reserve_id)`
>    - `single_flank(reserve_id)`
>    - `catalogue_for_side(reserve_id, side)` — **raises `ValueError`** on
>      anything other than `'L'` or `'R'`. This is the guard that stops an L
>      crop ever being scored against an R catalogue.
> 2. In `tools/seed_demo.py`, call `repo.rebuild_entities()` after assignments
>    are inserted, and give roughly six of the thirteen individuals only one
>    flank, so `single_flank_individuals` is actually exercised. In the field a
>    good share of tigers are only ever photographed from one side.
> 3. Replace rule 6 in `CLAUDE.md` with the entity version from the audit, and
>    add a pointer to `docs/DATA.md`.
> 4. Live checks: entities exist and every one has side L or R; the L and R
>    catalogues are disjoint; `catalogue_for_side` raises on a bad side; no
>    sided assignment is left without an `entity_id`; single-flank individuals
>    exist in the fixture.
>
> Run the suite and quote the result.

*Do this before any re-identification code is written. `identify.py` doesn't
exist yet, which is the only reason this is a schema addition rather than a
rewrite.*

---

## Prompt 5 — two-pass Stage A

> Read `docs/AUDIT_AND_REVISED_PLAN.md` §3 P2-6.
>
> The median window in `edge/pipeline/triage.py` is causal — it only contains
> frames before the current one. Two consequences: the first frame at each
> station (and separately the first night frame) has no background and can
> never be quarantined, so the "% removed by Stage A" figure changes with input
> ordering; and an animal appearing early poisons the background for every
> frame after it.
>
> This is batch processing, not streaming. Make it two-pass: compute the median
> over **all** frames at a station first, then score every frame against it.
> Keep the day/night split.
>
> Add a live check that running triage twice on the same run produces the same
> quarantine count, and one that shuffling the input order produces the same
> result. Order-independence is the property that makes the throughput number
> reportable.
>
> Run the suite and quote the result.

---

## Prompt 6 — the alert scenario suite

> Read `docs/BLUEPRINT.md` §13 layer 4 and the scenario table in `CLAUDE.md`.
>
> All alert checks currently live in the live suite. Give them their own home,
> driven by generated data rather than the demo seed.
>
> Create `tests/scenarios/test_alert_scenarios.py` with a small generator that
> builds a synthetic capture history — stations with activity intervals,
> individuals with home ranges, captures sampled from them — then injects each
> of the eight scenarios and asserts the outcome. Four must fire, four must be
> suppressed with the right reason.
>
> The point is that `edge/pipeline/alerts.py` must reproduce all eight **from
> the data**, never from anything the seed script wrote. Prove that by running
> the suite against a database the demo seed never touched.
>
> Print a pass/fail table at the end — it goes on a slide.
>
> Run both suites and quote both results.

---

## Prompt 7 — blank-detection measurement

> Read `docs/DATA.md` §4 and §7.
>
> Stage A has never been measured. Blank-detection accuracy, with particular
> attention to false negatives, is an explicit jury criterion.
>
> ATRW cannot be used for this — it contains no blank frames at all. Use
> Caltech Camera Traps or Wellington from LILA.
>
> 1. Write `tools/eval_blank.py` that takes a labelled empty/animal set and
>    reports precision, recall, a threshold sweep, and the false-negative rate
>    at the chosen operating point.
> 2. **Split by station, never by image.** Stage A is a per-station background
>    model; validating on stations it was tuned on measures memorisation. Make
>    the split explicit in the output so nobody can misread which number it is.
> 3. Report Stage A alone and end-to-end separately.
>
> Write the numbers into `docs/RESULTS.md` as you get them.

---

## Prompt 8 — identification

> Read `docs/DATA.md` §§1–3 in full before writing any code. It settles the
> model choice, the input geometry and the rectification method, and it names
> two dead ends to avoid.
>
> Build `edge/pipeline/identify.py` in this order, stopping after each to show
> me output:
>
> 1. flank crop from the detection box
> 2. rectification on the **shoulder–hip quadrilateral** — not nose-to-tail;
>    those are the two noisiest keypoints in ATRW
> 3. a quality gate that **refuses** below threshold rather than guessing
> 4. embedding at **256 × 128**, with width/height hyperparameters swapped
>    because tiger boxes are horizontal-major
> 5. matching through `repo.catalogue_for_side()`
> 6. the three-way decision: auto-accept / human review / provisional enrol
>
> Constraints from the paper: build the **triplet-loss (TriHard)** baseline.
> Do not build PPbM — it buys 2.8 mAP for a seven-part pose model. Do not use
> OpenPose; it failed to converge on the tiger skeleton. If HRNet is needed at
> all, that is the one that works.
>
> Evaluation must report **cross-camera** numbers, not single-camera, and must
> exclude same-event frames from the gallery.

---

## Standing rules for every session

Paste this with any prompt if the agent starts drifting:

> Rules for this repo, from `CLAUDE.md`:
> all SQL lives in `edge/db/repo.py` and nowhere else; all thresholds live in
> `edge/config.py`; nothing in `edge/ui/` may reference a URL off this machine;
> `audit_log` is append-only; corrections supersede rather than overwrite;
> an L crop is never scored against an R catalogue; role gating is enforced
> server-side; and refusing to answer is a valid output — do not "fix" the
> quality gate, the effort-coverage floor or the review queue into always
> producing a result.
>
> Never report a task finished without running
> `python -m tests.live.test_routes` and quoting the count. Assert the effect,
> not the existence.

---

## Do not build

PPbM · OpenPose · a vector database · the central tier · an M-STrIPES parser
against a guessed schema.

The last one matters most: `edge/exports/mstripes.py` currently refuses and
explains why. That refusal is correct. Claiming an integration you have not
verified is the single thing most likely to cost you the room.
