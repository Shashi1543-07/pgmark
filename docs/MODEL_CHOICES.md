# Model choices

Every model decision behind Stage B (`edge/pipeline/detector.py`), the
keypoint regressor (`edge/pipeline/keypoints.py`), and re-identification
(`edge/pipeline/identify.py`), in pipeline order, and why. See
`docs/DATA.md` for the evidence each of these comes from, and
`docs/RESULTS.md` for what was measured once each piece was built.

## Stage B: MegaDetector V6, MDV6-mit-yolov9-c — not the smaller variant, not stock YOLO11

**Variant: MDV6-mit-yolov9-c.** YOLOv9 architecture, "compact" size,
9.7M parameters, **MIT-licensed**. Detects three classes — animal,
person, vehicle — matching `detections.label`'s existing vocabulary
exactly (`edge/db/migrations/0001_init.sql`).

**Not MDV6-yolov10-c**, despite being the smaller, faster-sounding
option (2.3M parameters, ~2% of MegaDetector V5's 139.9M) that this
project initially targeted. Checked directly against the Model Zoo,
not assumed: that specific compact build is **AGPL-3.0**, the opposite
of what Stage B needed. The size difference is real — 9.7M vs 2.3M
parameters, and the measured throughput in `docs/RESULTS.md` (3.02
images/sec on an 8-core laptop CPU) reflects the larger MIT-licensed
model, not the smaller AGPL one — but the licence constraint outranked
the size preference once the two turned out to conflict.

**Not stock YOLO11 either.** Two separate reasons, not one: COCO (what
a stock YOLO11 checkpoint is trained on) has no tiger class and no
night-IR camera-trap imagery — it is trained on web photos, a different
domain from what Stage A/B actually see. Separately, the MegaDetector
team themselves evaluated YOLOv11-based variants for this exact model
family and did not release them, citing limited performance and
architectural gains over the YOLOv9/YOLOv10/RT-DETR variants they did
ship. (That specific benchmarking claim is relayed from the brief that
set this constraint, not independently re-run here — the licensing and
Model Zoo variant facts above were checked directly; this one was not.
It did not need independent verification for the decision itself, since
MDV6-mit-yolov9-c was already the right choice on architecture-variant
and licence grounds alone.)

**Vendored, not `pip install`ed.** `pip install PyTorchWildlife` — the
standard way to run any MegaDetector V6 variant — unconditionally
requires `ultralytics` and `yolov5` (both AGPL-3.0) as hard
dependencies, regardless of which model you actually load. Installing
it at all would have pulled AGPL-licensed code into this deployment
even though MDV6-mit-yolov9-c's own weights are MIT. Fixed by vendoring
only the self-contained, `ultralytics`-free architecture code (~54KB,
13 files, every import checked by parsing, not grepping —
`tests/unit/test_vendor_licensing.py` makes that check permanent) from
the same MIT-licensed upstream repository, with `edge/pipeline/vendor/
yolo_mit/NOTICE.md` recording exactly what was and was not copied, and
why. `edge/pipeline/detector.py` is new code around that vendored core,
not PyTorchWildlife's own wrapper class, which pulls in `supervision`,
`wget`, and `lightning` that single-image inference against a
pre-downloaded checkpoint does not need.

## The keypoint regressor: YOLO11-pose, 2 keypoints — not HRNet, never OpenPose

**A different YOLO11 from the one Stage B avoids.** Stage B rules out
*stock YOLO11* (the general object detector, wrong training domain, no
tiger class). The keypoint regressor uses *YOLO11-pose*, Ultralytics'
pose-estimation variant of the same model family, for an entirely
different task (predicting two points, not detecting animal/person/
vehicle boxes). These are not in tension — different problem, different
tool, and the object-detection avoidance reasoning above simply does
not apply to a pose-estimation model with no COCO-tiger-class concern
in the first place.

**Never OpenPose.** The ATRW paper reports that adapting OpenPose to
the tiger skeleton produced a non-convergent training run — two hours
the authors did not get back. Not attempted here either, for the same
reason.

**YOLO11-pose with a custom 2-keypoint configuration, not the full
15-point skeleton, and not HRNet.** The original plan pointed at HRNet,
which reaches 86.9 AP on the full ATRW skeleton (docs/DATA.md §3) —
genuinely the stronger architecture for full pose estimation, and still
the right answer if this build ever needs more than two points. But
rectification only ever uses two of ATRW's fifteen keypoints (the
near-side shoulder and hip — see "Rectification" below), and training a
model to predict thirteen points nothing downstream reads is work spent
on a problem this build does not have. Ultralytics YOLO11-pose's own
keypoint count is a config value (`kpt_shape: [2, 3]` in
`data/raw/atrw_pose_yolo/data.yaml`), so "2 keypoints, not the full
skeleton" is a configuration choice on top of the architecture HRNet
would have needed built specially to support, not a different
architecture needing separate justification. Training reused
`reid_keypoints_{train,test}.json` — the same ATRW annotations already
in this build for re-ID and side inference — rather than downloading
`docs/DATA.md`'s separate ~293MB pose-specific set; those files carry
the same full 15-point COCO-format annotation for the same images, a
superset of what 2-keypoint training needs.

**Measured, not assumed:** 60 epochs, 44 minutes on an 8-core laptop
CPU (nano variant, 2.66M parameters, 256px images — far lighter than
full pose estimation), final validation mAP50 0.995. See
`docs/RESULTS.md` for the full table, the CPU/GPU note (this build
initially trained everything on CPU because a misread of
`torch.cuda.is_available()` was taken as "no GPU" rather than "wrong
torch wheel" — an available RTX 2050 sat unused until that was caught
and fixed), and the "wild" end-to-end evaluation this regressor made
possible.

**The AGPL-3.0 trade-off, recorded, not inherited silently.** Training
and running this model requires the `ultralytics` package — the
opposite licence decision from Stage B above, made on purpose, not by
inconsistency. Stage B had a genuinely MIT-licensed alternative that
made avoiding AGPL free; no equivalent MIT/Apache-licensed 2-keypoint
pose trainer was found for this task. `requirements.txt` declares
`ultralytics>=8.4` for this reason, with a comment pointing back here,
and `tests/unit/test_vendor_licensing.py` checks that this is scoped
correctly — ultralytics absent from Stage B specifically, present and
explained everywhere else. For a government forest-department
deployment, a copyleft licence on the source is acceptable, and
arguably desirable — AGPL's own network-copyleft provision means any
deployed improvement has to be shared back, which aligns with a
public-interest deployment's own incentives — but it is a choice made
and recorded here, not a licence that arrived unnoticed because a
dependency happened to need it.

**A real, unfixed limitation: this model does not determine true
left/right flank side.** Its two output points are always named
"right_shoulder"/"right_hip" by convention — a label rectify_flank()
needs, not a claim about the tiger's actual anatomy. Confirmed
empirically in `docs/RESULTS.md`'s "wild" evaluation: every one of 289
held-out images that reached an embedding came back labelled side='R';
none 'L'. This was the literal scope asked for ("near-side shoulder and
hip only"), so it is not a defect relative to what was built — but on a
real deployment, where roughly half of captures would show the left
flank, every one of them is silently compared against the right-side
catalogue. A real fix needs the model to also classify which side is
visible (e.g. two output classes instead of one, trainable from the
same ATRW side labels this build already derives via
`identify.infer_side()`) — not attempted here, flagged here instead of
discovered later. See `edge/pipeline/keypoints.py`'s module docstring
and `CLAUDE.md`'s "Built vs not built" section.

## Rectification: the shoulder-hip axis, not nose-to-tail

ATRW Table 4 (keypoint annotation variance, σ² × 10⁻⁴, lower = more
reliable):

| Most reliable | σ² | Least reliable | σ² |
|---|---|---|---|
| right shoulder | 4.1 | nose | 69.0 |
| left shoulder | 6.9 | right ear | 67.7 |
| right hip | 9.1 | root of tail | 46.7 |

The obvious body axis — nose to tail root — uses the two noisiest
keypoints in the dataset. Shoulders and hips are among the most
reliable. This replaces the PCA-on-mask approach in `docs/BLUEPRINT.md`
§7.1.

**Not a real 4-point quadrilateral, though**, despite "warp the
shoulder-hip quadrilateral" reading as four measured corners. Checked
against 905 side-resolved ATRW training crops, the far side's shoulder
and hip are unlabelled in every one of them — a profile photograph
geometrically cannot show them. Rectification instead uses the near
side's shoulder-hip line to set the body axis and length, with the
perpendicular (dorsal-ventral) extent estimated as a fixed proportion
of that length (`Identify.rect_body_depth_ratio`, `edge/config.py`) — a
stated approximation, not a fourth measured point. This was discovered
by checking the actual keypoint data before writing the training
pipeline against a false assumption, not found later by a failing
evaluation number.

**Where the four keypoints come from is a separate decision from
rectification itself.** Evaluating the pipeline against ATRW, the
dataset's own ground-truth annotations are used directly — training a
pose model to re-derive numbers ATRW already publishes would be
circular. A real, unlabelled Pench crop uses the trained keypoint
regressor above instead.

## Re-identification: triplet-loss (TriHard) on ResNet-50, not PPbM

| Method | single-cam mAP | cross-cam mAP | cross-cam top-1 |
|---|---|---|---|
| Cross-entropy, frozen backbone | 59.1 | 38.1 | 69.7 |
| **Triplet loss (TriHard)** | **71.3** | **47.2** | **77.6** |
| PPbM-a | 74.1 | 51.7 | 76.8 |

PPbM buys 2.8 mAP over triplet loss for a seven-part pose model with
per-part regional pooling and soft-attention aggregation — not a trade
worth making inside a hackathon build. TriHard on an ImageNet-pretrained
ResNet-50 gets 96% of the way there for a fraction of the engineering.
**Input is 256×128, not 128×256** — tiger boxes are horizontal-major,
the opposite of pedestrian re-ID, and every width/height hyperparameter
(`edge/pipeline/identify.py::RECT_WIDTH, RECT_HEIGHT`) is swapped
accordingly.

**Fallback, stated up front rather than discovered under deadline
pressure:** if the triplet-loss run had not converged to something
usable, the plan was to ship the cross-entropy row instead — frozen
backbone, two FC layers, minutes not hours to train — and say so on the
slide. Not needed in the event: `docs/RESULTS.md` shows the trained
embedder converging (loss 0.285 → 0.0006 over 40 epochs) and clearing a
useful held-out-identity signal.

### The no-rectification fallback, measured

`tools/train_identify_no_rect.py` trains a second embedder on raw,
un-rectified crops (resized straight to 256×128, no keypoints) with
heavy augmentation — random rotation, scale/crop jitter, colour jitter,
horizontal flip — standing in for the geometric normalisation
rectification would otherwise provide. Same entities, same
held-out-identity split, same 40 epochs as the rectified embedder, so
the comparison in `docs/RESULTS.md` is direct.

**The measured result is not the one the plan assumed**: on this
evaluation, the no-rectification fallback scores *higher* mAP than the
rectified embedder on both sides (~0.94 vs ~0.82–0.85) — see
`docs/RESULTS.md` for the full table and the caveats on reading it
(ATRW's zoo photography is more pose-consistent than Pench's camera
traps will be, which this comparison does not test — it does not mean
"drop rectification" for the real deployment). The point of measuring
this before the keypoint model finished, rather than only if it had
failed, was exactly so a surprising result like this one would be
caught and reported, not assumed away.

## Licence — belongs on the slide, not just in this file

ATRW is **CC BY-NC-SA 4.0**. Images are owned by MakerCollider and the
World Wildlife Fund.

1. **Non-commercial.** Correct for a hackathon and for research. A model
   trained on it is not something to claim is commercially deployable
   as-is.
2. **Share-alike.** Derivatives (the trained embedder's and keypoint
   regressor's weights) inherit the licence.
3. Say plainly: *"trained on CC BY-NC-SA research data; a production
   deployment would retrain on the reserve's own catalogue."* True,
   correct, and better volunteered than extracted under questioning.

Separately, and covered in full above: **MDV6-mit-yolov9-c (Stage B) is
MIT**; **the keypoint regressor's own weights are trained with
Ultralytics, AGPL-3.0**, a recorded trade-off, not an inherited one.
Three different licences across three models in this pipeline — state
which applies to which, not "the model" as a single blanket claim.

## What this build does not claim

- **Amur tigers, not Bengal.** ATRW is *P. t. altaica* from ten Chinese
  zoos on tripod-mounted SLRs and synchronised surveillance cameras.
  Pench has *P. t. tigris*, photographed by unattended camera traps,
  mostly at night on infrared. Two gaps stack: subspecies and capture
  modality. Report evaluation numbers as "validated on ATRW," never as
  "validated on tigers" without qualification.
- **No Pench data anywhere in training or evaluation.** None exists yet.
- **Cross-camera numbers are the honest ones**, ~24 mAP points below
  single-camera on every method in the paper. Report both, label both;
  the single-camera number alone overstates by roughly half again.
- **The keypoint regressor does not know true left from true right** —
  see above. The catalogue-matching pipeline is proven to work
  end to end (`docs/RESULTS.md`'s "wiring verified" section, Task 5),
  but is not yet trustworthy for unattended production matching against
  a side-separated catalogue without this fixed.
