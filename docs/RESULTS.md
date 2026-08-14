# Results

Numbers produced against real, external, labelled data -- never Pench
data, none of which exists yet (docs/DATA.md §6). Reproduce with:

```
python -m tools.fetch_data --set cct20
python -m tools.eval_blank
```

## Blank detection (Stage A)

AUDIT_AND_REVISED_PLAN.md P2-7: Stage A had never been measured against
labelled data before this. Evaluated against Caltech Camera Traps'
published `eccv_18` benchmark subset (LILA BC), using
`edge/pipeline/triage.py`'s own production `cell_score()` and
`_score_group()` -- not a reimplementation.

**Split by location, never by image** (docs/DATA.md §4): `cis_*` holds
out images at the same 10 locations Stage A's background would have
been built from; `trans_*` holds out entire locations never seen
anywhere else. `trans_test` is the honest number -- 21,497 true-animal
images and 1,778 true-blank images across 9 stations never touched
during any tuning.

The positive class is **animal present**, not blank, matching
camera-trap literature (MegaDetector's own convention): the dangerous
error is an animal wrongly discarded, which is a **false negative**
under this framing, not a false positive. Getting this backwards is an
easy, silent mistake -- see `tools/eval_blank.py`'s module docstring.

### At the shipped operating point (`stage_a_blank_threshold = 0.03`)

| split | n_animal | n_blank | FN rate | animal recall | blank caught |
|---|---|---|---|---|---|
| cis_val | 2,489 | 995 | 0.002 | 0.998 | 0.018 |
| cis_test | 14,647 | 1,180 | 0.000 | 1.000 | 0.000 |
| trans_val | 1,664 | 61 | 0.000 | 1.000 | 0.000 |
| **trans_test** | **21,497** | **1,778** | **0.0074** | **0.9926** | **0.0011** |

**Headline: 0.74% false-negative rate on held-out stations** -- 159 of
21,497 true-animal images wrongly quarantined. Safe, by the standard
`edge/config.py` states for this threshold: "a blank kept costs seconds
of review; an animal discarded is unrecoverable field data."

**But blank-catch rate is 0.11%** -- Stage A removes almost nothing on
this dataset at the current threshold. The 0.03 threshold was tuned in
Prompt 2 against a synthetic worst-case table (a single lit cell at low
night-IR contrast), with deliberate headroom "since a synthetic worst
case is not the same as having actually measured this against labelled
data" (`edge/config.py`). That measurement now exists, and it shows the
threshold is far more conservative on this dataset than the synthetic
case implied.

### Threshold sweep (`trans_test`)

| threshold | FN rate | animal recall | blank caught | quarantine precision |
|---|---|---|---|---|
| 0.01 | 0.000 | 1.000 | 0.000 | n/a |
| 0.02 | 0.0011 | 0.9989 | 0.000 | 0.000 |
| **0.03** | **0.0074** | **0.9926** | **0.0011** | **0.0124** ← shipped |
| 0.04 | 0.0165 | 0.9835 | 0.0096 | 0.0457 |
| 0.05 | 0.0328 | 0.9672 | 0.0214 | 0.0511 |
| 0.07 | 0.1049 | 0.8951 | 0.0529 | 0.0400 |
| 0.10 | 0.2541 | 0.7459 | 0.0861 | 0.0272 |
| 0.15 | 0.4084 | 0.5916 | 0.1935 | 0.0377 |
| 0.20 | 0.5116 | 0.4884 | 0.2306 | 0.0359 |

No threshold in this sweep achieves both a low false-negative rate and
meaningful throughput savings on CCT20. That is very likely a **domain
mismatch, not a bug**: CCT stations run for months across seasons and
times of day, with far more lighting variation per station than a
multi-week Pench survey cycle would see, and Stage A is a per-station
median background model that assumes a comparatively stable scene.
Reported here as a limitation of this validation set, not evidence
against the design -- but it is exactly the kind of gap that only shows
up by measuring against real data instead of a synthetic table, which
is the entire point of doing this.

**What this does not license:** retuning `stage_a_blank_threshold`
against CCT20. CCT20 is not Pench -- different ecology, different
camera hardware, different capture cadence, mostly daylight rather than
night-IR. Per docs/DATA.md §6, the honest position is: validated on
Caltech Camera Traps' published splits, not yet validated on Pench
data, and a real pilot would need to re-measure this exact sweep
against a few weeks of the reserve's own footage before moving the
number.

### Stage B: MegaDetector V6 (MDV6-mit-yolov9-c)

Built and wired in (AUDIT_AND_REVISED_PLAN.md Prompt 3) -- see
`docs/MODEL_CHOICES.md` for the model/licence decision and
`edge/pipeline/detector.py`. Verified against real photographs, not
synthetic frames: a real tiger photo produces a detection box (conf
0.94), a genuinely blank camera-trap frame produces none, and a person
detection is blurred and routed to `persons_restricted` rather than the
tiger pipeline (`tests/live/test_routes.py`).

**Throughput** (`python -m tools.bench_detector`) -- "processing
throughput on constrained hardware" is an explicit jury criterion; this
is the first real measurement, not the parameter-count comparison
alone. 200 real ATRW images, CPU only, 8 torch threads, on the only
hardware available to measure on -- not claimed as "the least powerful
laptop on the team," since no weaker machine was available to test:

| CPU | images/sec | ms/image | 20,000-frame cycle |
|---|---|---|---|
| Intel Core i5-12450HX (8C/12T laptop) | **3.02** | 331.6 | ~110 minutes |

MDV6-mit-yolov9-c is 9.7M parameters -- about 4x the 2.3M-parameter
YOLOv10-compact build originally cited for this role (docs/MODEL_CHOICES.md
explains the licence reason for the substitution), so this number is not
what that smaller variant would have measured. 110 minutes of Stage B
alone for a 20,000-frame cycle is a real cost to plan a field deployment
around, not a number to round away.

---

## Re-identification

See `docs/MODEL_CHOICES.md` for the model and rectification decisions,
and their honest limits (no pose model trained; ATRW's own ground-truth
keypoints are used for evaluation, not a Pench-deployable pipeline).

**Not the official ATRW/CVWC2019 test set** -- its labels were withheld
by the competition organizers and were never publicly released (checked
directly against the challenge site before relying on it). Numbers here
are against a held-out-by-identity split of ATRW's labelled *training*
portion instead (`tools/atrw_dataset.held_out_identity_split`): 86
identities / 723 side-resolvable images to train on, 21 identities /
167 side-resolvable images held out, zero identity overlap between the
two. Report these as "held out by identity from ATRW's labelled
training data," never as "the ATRW test set."

### Training

TriHard batch-hard mining, ResNet-50 (ImageNet-pretrained), P=8 entities
x K=4 images per batch, margin 0.3, 40 epochs, CPU only:
`python -m tools.train_identify`. Mean batch loss fell from 0.285
(epoch 1) to 0.0006 (epoch 40); active (margin-violating) triplets per
batch fell from 662 to 7. Reproduce with the command above; weights
land at `data/weights/identify_embedder.pt` (gitignored).

### Held-out-identity closed-set accuracy

`python -m tools.eval_identify` -- 21 held-out identities never seen in
training, 170 of their 409 images successfully embedded (the rest
refused by the same side-inference/rectification gates production would
apply, not a separate, more lenient evaluation path).

**Per ENTITY, not per identity -- proven, not asserted.** Training keys
on `(ind_id, side)` (`tools/train_identify.py::build_entities()`), and
evaluation groups by side before any query/gallery pair is formed
(`tools/eval_identify.py::evaluate()`), so an L crop is structurally
never compared against an R gallery. A counterfactual confirms this
empirically rather than by code inspection alone: re-running top-1
matching with the side split disabled (L and R pooled into one
gallery, the mistake CLAUDE.md rule 6 forbids) finds **0 of 170** top-1
matches would have been the same individual's opposite flank. None of
the number below comes from cross-flank leakage.

**Gallery size and a 95% confidence interval, stated with every number,
not once at the top.** At n=63 and n=104 a bare point estimate reads
more precise than it is -- top-1/top-5 use a Wilson score interval
(behaves at small n, unlike a normal approximation); mAP uses a
percentile bootstrap (2,000 resamples), since it is a mean of per-query
AP values, not a binomial proportion.

| side | entities | images | queries | top-1 (95% CI) | top-5 (95% CI) | mAP (95% CI) |
|---|---|---|---|---|---|---|
| L | 10 | 65 | 63 | 1.0000 (0.943, 1.000) | 1.0000 (0.943, 1.000) | 0.8477 (0.802, 0.891) |
| R | 12 | 105 | 104 | 0.9327 (0.868, 0.967) | 0.9904 (0.948, 0.998) | 0.8202 (0.784, 0.856) |

mAP is the paper's primary metric and is visibly stricter than top-1
here -- it penalises every irrelevant gallery item ranked above a
relevant one, not just whether the single best guess was right. R-side
mAP (0.82) against R-side top-1 (0.93) is the honest gap: the model
gets the best answer right most of the time, but its full ranking is
less clean than top-1 alone suggests. L-side top-1's CI floor is 0.943,
not 1.000 -- a perfect point estimate on 63 queries is not a perfect
model, and the interval says so.

**Same-clip exclusion is by construction, not by inspection.** No
camera or video/clip identifier exists anywhere in ATRW's public
files -- checked directly against every metadata file shipped
(`reid_list_*.csv`, `reid_keypoints_*.json`, `keypoint_*.json`), not
assumed. Since a true clip boundary cannot be recovered, every query's
gallery unconditionally excludes any crop of its own entity within 50
frame-numbers (`GALLERY_ID_EXCLUSION_GAP`) -- a stated proxy applied
the same way to every query, not a spot-check that a different sample
could have missed.

**A true cross-camera split was checked for specifically, twice, and
does not exist in this download.** The ATRW paper states the original
cropped re-ID images were renamed with camera id, shot id, frame number
and entity id. The LILA/CVWC2019-hosted files this build actually
downloads are not that: all 5,156 train+test filenames (every one
checked, not sampled) match a plain `NNNNNN.jpg` sequential pattern
numbered 1-5155, with no structure consistent with a compound field
encoding. The distributor stripped the richer naming -- almost
certainly to avoid leaking the withheld test identities directly
through the filename (the same reason the test-set CSV itself carries
no labels; see above). No single-camera/cross-camera split is reported
because it cannot honestly be constructed from these files, not
because it wasn't worth trying.

**This is still not comparable to the paper's 71.3% single-camera /
47.2% cross-camera mAP for TriHard**, and the gap that remains after
fixing entity-scoping, adding mAP, and tightening the exclusion is
gallery size: 10-12 held-out entities is a much smaller, easier closed
set than ATRW's full benchmark gallery, and mAP falls as the gallery
grows because there are more places for an irrelevant item to
intrude on the ranking. This number sits somewhere between the paper's
two figures, not equal to either.

**What this demonstrates:** the pipeline -- rectification from two
keypoints, a TriHard embedder trained from nothing on 723 images, and
cosine matching -- learns a real, non-trivial, side-correct signal on
identities it never trained on. It is a working baseline on a small
gallery, not a validated production accuracy figure, and should not be
quoted as a single "94% re-identification accuracy" number -- report
top-1, top-5, mAP, gallery size, and a confidence interval together,
every time.

### Open-set separation and threshold calibration

`python -m tools.eval_identify` -- same 170 embedded held-out crops.
Closed-set top-1/top-5/mAP above all assume the right answer is
somewhere in the gallery. At Pench it frequently won't be: most camera
captures will be either a tiger already in the catalogue OR a genuinely
new one, and the system has to tell those apart *before* top-1 is even
a meaningful question.

**Can max similarity separate "in the catalogue" from "never seen"?**
For every held-out crop: its genuine max-similarity (gallery includes
its own entity, past the same-entity exclusion) and its impostor
max-similarity (gallery restricted to other entities only, simulating
"this tiger isn't catalogued at all"), same side throughout.

| n genuine | n impostor | AUC | 95% CI |
|---|---|---|---|
| 167 | 170 | **0.9180** | (0.886, 0.947) |

A real, usable signal -- genuine and impostor score distributions are
well separated, not overlapping noise.

**T_HIGH (0.82) and T_LOW (0.55) in `edge/config.py` were guesses made
before any data existed. Calibrated against these distributions
(t_low = 5th percentile of genuine scores, t_high = 99th percentile of
impostor scores):**

| | current (`edge/config.py`) | calibrated (this data) |
|---|---|---|
| t_low | 0.55 | 0.75 |
| t_high | 0.82 | 0.946 |

**The dangerous number, stated bluntly: at 0.82 (the original guess),
14.7% of novel tigers -- ones the catalogue has never seen -- would be
wrongly auto-accepted as a match to an existing entity.** That
corrupts the catalogue in a way `assignments.superseded_by` can correct
after the fact (CLAUDE.md rule 5) but should not be relying on
correcting routinely. `edge/config.py` now ships `t_high = 0.95` --
deliberately conservative, rounded up from this measurement's 0.946,
and documented in `config.py` itself as a stated trade of auto-accept
convenience for catalogue safety, not a silent tightening.

Share of crops landing in each bucket:

| | n | auto-accept | review | provisional-enrol |
|---|---|---|---|---|
| current, genuine crops | 167 | 0.832 | 0.162 | 0.006 |
| current, impostor crops | 170 | **0.147** | 0.559 | 0.294 |
| calibrated, genuine crops | 167 | 0.263 | 0.683 | 0.054 |
| calibrated, impostor crops | 170 | 0.012 | 0.182 | 0.806 |

The trade the calibrated thresholds make is explicit: impostor
auto-accept drops from 14.7% to 1.2% (safer catalogue), but genuine
auto-accept drops from 83.2% to 26.3% and review load roughly
quadruples (16.2% -> 68.3% of genuine crops). That ratio is what
determines how much human review work the system actually creates, and
it is a real cost, not a free safety improvement.

**This calibration is a starting point, not a number to paste into
`edge/config.py` as production truth.** Same caution as CCT20 and
`stage_a_blank_threshold`: this is Amur zoo tigers on tripod cameras,
not Bengal tigers on Pench camera traps, and n=337 probes total. A real
pilot needs to re-run this exact calibration against the reserve's own
early data before moving the shipped thresholds.

---

## End-to-end wiring: a photo in, a catalogue entry out

`POST /api/identify/upload` and the "Identify a photo" screen
(AUDIT_AND_REVISED_PLAN.md Task 5) run the full chain -- detect
(Stage B) -> keypoints -> side -> rectify -> quality gate -> embed ->
match -> auto-accept/review/provisional-enrol -- against a real
uploaded photo, landing a real row in `images`, `detections`,
`flank_crops`, and (depending on the decision) `assignments`,
`review_queue`, or a new provisional `individuals` row, every call
audited (`identify.upload`).

**Keypoints come from a fixed geometric stub as of this build**
(`edge/pipeline/keypoints.py`: shoulder at 25% along the detection box,
hip at 75%, always the right side, since a bounding box alone carries
no signal about which flank is facing the camera). Verified against
real photographs through the real running server, not just the
function directly:

- a real tiger photo, uploaded, correctly enrols as a new provisional
  individual when the catalogue has nothing to match it against
- the identical photo, uploaded a second time, correctly auto-matches
  the entity just created from it (score 1.000) -- proving the stub's
  determinism holds end to end through the HTTP route, not only at the
  function level
- a forced mid-range score correctly lands in the review queue, visible
  through `/api/review`
- every call lands an audit log entry

**Wiring verified at the time this was written with the stub still in
place; Task 4 (below) has since trained the real regressor and swapped
it in.** The two subsections below are what came out of that.

---

## Task 4: the trained keypoint regressor

`tools/train_keypoints.py` -- Ultralytics YOLO11-pose (nano, 2.66M
params), a **custom 2-keypoint configuration** (near-side shoulder + hip
only, `kpt_shape: [2, 3]`), not the full 15-point skeleton, and not
HRNet -- see `docs/MODEL_CHOICES.md` for why, and for the AGPL-3.0
trade-off this deliberately makes (the opposite licence decision from
Stage B, recorded on purpose). Training data: `reid_keypoints_{train,
test}.json`, ATRW's own re-ID keypoint annotations, already in this
build -- not the separate ~293MB pose-specific download docs/DATA.md
names, which is a superset covering the same images.

**Training time, measured on both hardware paths available for this
build, not assumed:**

| device | epochs | wall time | per-epoch |
|---|---|---|---|
| CPU (i5-12450HX, 8C/12T) | 60 | 44.0 min | ~44s |
| *(no-rectification embedder retrain, for comparison)* GPU (RTX 2050) | 40 | ~11 min | ~13s |

The keypoint regressor itself was trained on CPU (started before the
GPU misconfiguration below was found and fixed) and still finished in
under 45 minutes -- well inside the "few hours, or fall back" budget --
because the task (2 points, 256px images, a nano backbone) is far
lighter than full pose estimation. **A CPU/GPU note worth recording
plainly: this build initially trained everything on CPU because
`torch.cuda.is_available()` returned `False`, which was read as "no
GPU" without checking further -- the actual cause was a CPU-only torch
wheel, not absent hardware (`nvidia-smi` showed a healthy RTX 2050 the
whole time). Fixed by installing `torch+cu130`; subsequent training
(the no-rectification fallback, below) ran roughly 7-8x faster on the
GPU once corrected.**

Final validation (held-out images at training time, not the
held-out-identity split used everywhere else in this document -- YOLO's
own train/val split): Pose precision 1.0, recall 1.0, mAP50 0.995,
mAP50-95 0.992. Inference: 10.1ms/image.

### The side-determination gap -- found, not hidden

**Neither the stub nor this trained model determines which physical
flank (true left or true right) is showing.** Both always label their
two output points "right_shoulder" / "right_hip" -- a fixed naming
convention `identify.infer_side()` needs, not a claim about the tiger's
actual anatomy. This was Task 4's literal scope ("near-side shoulder and
hip only"), so it is not a bug relative to what was asked -- but it is a
real limitation for production use: on a genuine Pench deployment, where
roughly half of captures would show the left flank, every one of them
gets compared against the RIGHT-side catalogue only, silently, because
the pipeline has no way to tell. Confirmed empirically in the wild
evaluation below, not just reasoned about: every single embedded image
came back labelled side='R' -- none as 'L' -- because the model's output
vocabulary literally has no left-flank label to produce. A real fix
needs the keypoint model to also classify which side is visible (e.g.
two output classes instead of one, trainable from the same ATRW side
labels this build already derives via `infer_side()` on the ground-truth
keypoints) -- not attempted here; flagged for a decision, not silently
worked around.

### The no-rectification fallback, measured

`tools/train_identify_no_rect.py` -- same entities, same
held-out-identity split, same 40 epochs as the rectified embedder;
raw crops resized straight to 256x128 with heavy augmentation (random
rotation, scale/crop jitter, colour jitter, horizontal flip) standing in
for rectify_flank()'s geometric normalisation.

| | side | entities | images | queries | top-1 (95% CI) | top-5 (95% CI) | mAP (95% CI) |
|---|---|---|---|---|---|---|---|
| **rectified** | L | 10 | 65 | 63 | 1.0000 (0.943, 1.000) | 1.0000 (0.943, 1.000) | 0.8477 (0.802, 0.891) |
| **rectified** | R | 12 | 105 | 104 | 0.9327 (0.868, 0.967) | 0.9904 (0.948, 0.998) | 0.8202 (0.784, 0.856) |
| **no-rectification** | L | 10 | 65 | 63 | 1.0000 (0.943, 1.000) | 1.0000 (0.943, 1.000) | **0.9367** (0.895, 0.971) |
| **no-rectification** | R | 12 | 105 | 104 | 0.9808 (0.933, 0.995) | 1.0000 (0.964, 1.000) | **0.9363** (0.908, 0.962) |

**Stated plainly, because it cuts against the plan's own assumption:
on this evaluation, the no-rectification fallback scores HIGHER mAP
than the rectified embedder**, on both sides, by a real margin (0.94 vs
0.82-0.85). This is measured, not a typo. Plausible reasons, none
confirmed further than this: `rectify_flank()`'s perspective warp uses
an estimated, not measured, dorsal-ventral extent
(`Identify.rect_body_depth_ratio`, `docs/MODEL_CHOICES.md`) that may
introduce more geometric distortion than the pose variation it corrects
for on ATRW's already fairly consistent zoo photography; heavy
augmentation may simply generalise better on a dataset this thin (723
training images). **This does not mean "drop rectification" --
Pench's real camera-trap photos have far more pose/angle variation than
ATRW's zoo shots, which is exactly the case rectification is meant for
and this comparison does not test.** It means the assumption should be
re-measured against real Pench data before being trusted either way,
and that the fallback's cost, if the keypoint model had failed, would
have been small or even negative on this dataset -- not the large
accuracy cliff the plan anticipated.

### The "wild" evaluation: predicted boxes, predicted keypoints

`tools/eval_identify_wild.py` -- the ATRW paper's own "wild" track
(fully automatic pipeline) against this build's "plain" track (manual/
ground-truth annotations, measured earlier in this document). Same
held-out-identity split (21 identities, 409 images), but every stage now
runs for real: Stage B detects the box, the trained regressor predicts
keypoints, `identify.py` does the rest -- nothing comes from ATRW's own
annotations.

| | queries | top-1 (95% CI) | top-5 (95% CI) | mAP (95% CI) |
|---|---|---|---|---|
| "plain" (ground-truth kpts, side-separated) | 63 (L) / 104 (R) | 1.00 / 0.933 | 1.00 / 0.990 | 0.848 / 0.820 |
| **"wild"** (predicted box + predicted kpts) | **289** | **0.9446** (0.912, 0.966) | **0.9723** (0.946, 0.986) | **0.7562** (0.730, 0.781) |

409 held-out images attempted; 289 reached an embedding. Refused before
that point: 52 (`stage_b_no_animal` -- the detector found nothing) and
68 (`side_not_determinable` -- keypoint confidence below
`CONF_THRESHOLD=0.5` for at least one point, `quality_gate()` correctly
refusing rather than rectifying from an unreliable point).

**Read this table with the side-determination gap above attached, not
separately.** The "wild" row has no L/R split because every one of the
289 embedded images came back labelled 'R' -- the side-blind limitation
confirmed in practice, not just in theory. That means this mAP is
computed over a gallery that silently pools what should be two separate
catalogues (true left-flank and true right-flank crops of the same
individuals, indistinguishable to this pipeline). The number is real and
reproducible, but it is not the same claim the "plain" per-side numbers
make, and should not be quoted as directly comparable to them without
this caveat.

**The gap that is comparable: top-1 (0.94) barely moved from "plain" to
"wild"; mAP dropped from ~0.83 (averaged) to 0.76.** Some of that drop
is genuinely the cost of predicted boxes and keypoints instead of
ground truth; some of it is the side-pooling effect above inflating the
"plain" per-side numbers' apparent precision by keeping galleries small
and clean in a way "wild" cannot. Both are real, and this build cannot
fully separate their individual contributions from this measurement
alone.
