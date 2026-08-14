# Data and evaluation plan

What each source is for, what it is **not** for, how it is licensed, and the
evaluation protocol that follows from it.

Written from the ATRW paper (Li et al., ACM MM 2020, arXiv:1906.05586), the
LILA BC hosting of ATRW, and the iNaturalist Open Dataset.

---

## The short version

| Source | Gives us | Does NOT give us |
|---|---|---|
| **ATRW** | individual tiger identities + stripe crops + pose keypoints | any blank frames — every clip has a tiger |
| **LILA camera-trap sets** | real blank frames at scale, real burst structure, real station variety | any individual tiger identities |
| **iNaturalist** | labelled photos of Indian species for the species gate | individual IDs, and anything camera-trap-like |

Three sources, three separate jobs. None of them substitutes for another, and
none of them is Pench data.

---

## 1. ATRW — for re-identification only

### What to download

The re-ID subset is small. Get this tonight; it is not the 2 GB the detection
set is.

| Part | Size | Use |
|---|---|---|
| Re-ID train images + annotations | 132 MB | train the stripe embedder |
| Re-ID test images + annotations | 90 MB | held-out accuracy number |
| Pose train + val images + annotations | ~293 MB | flank rectification |
| Detection train/test | 2 GB / 1.4 GB | optional — we use a camera-trap detector instead |

Formats: bounding boxes in **Pascal VOC**, pose in **COCO keypoint** format
with named categories (`left_ear`, `right_ear`, `nose`, …), identities as a
CSV of `[ID, filename]` pairs.

**Use the published train/val/test splits.** They correspond to the ICCV 2019
CVWC competition, so any number you report is comparable to published work.
Inventing your own split throws that away.

### LICENCE — read this before the deck

ATRW is released under **CC BY-NC-SA 4.0**. Images are owned by MakerCollider
and the World Wildlife Fund.

Three consequences, all of which belong in `docs/MODEL_CHOICES.md`:

1. **Non-commercial.** Fine for a hackathon and for research. A model trained
   on it is not something you can claim is commercially deployable.
2. **Share-alike.** Derivatives inherit the licence.
3. In the pitch, say *"trained on CC BY-NC-SA research data; a production
   deployment would retrain on the reserve's own catalogue."* That is true,
   it is the correct answer, and volunteering it is far better than being
   asked.

### What the paper settles about our design

**The unit of re-ID is the entity, not the individual.**

> Since the left and right side of Amur Tigers have different stripe patterns,
> and it is rare to capture both sides of the tiger in the wild environment,
> the authors treat each side of the same tiger as a **different entity**.

92 tigers produce **182 entities** — very close to two sides each, because
ATRW was shot in zoos where both flanks could be captured. Average 28.3
bounding boxes per entity.

Do not read that ratio as reassurance. The paper's reason for splitting sides
into separate entities is a statement about the **wild**, not about their own
data: capturing both sides of the same animal in the field is rare. Camera
traps at Pench are the wild case, so single-flank individuals will be common
for us even though they are not common in ATRW. Our fixture data reflects the
field expectation, not the zoo one.

This is a schema correction, not just a validation of the `side` column:
the catalogue is keyed on `(ind_id, side)`. Migration `0002_entities.sql`
adds it.

**Input geometry: 256 × 128, and swap your width/height hyperparameters.**
The paper resizes to 256 × 128 for the horizontal aspect ratio of tigers, and
explicitly notes they had to exchange every width/height hyperparameter
because tiger boxes are horizontal-major while pedestrian re-ID boxes are
vertical-major. Copying pedestrian re-ID code without swapping these is a
silent accuracy loss.

---

## 2. Which model to build — the paper answers this

Benchmark results, mAP on the plain (manual box + pose) track:

| Method | single-cam mAP | **cross-cam mAP** | cross-cam top-1 |
|---|---|---|---|
| Cross-entropy, frozen backbone | 59.1 | 38.1 | 69.7 |
| **Triplet loss (TriHard)** | **71.3** | **47.2** | **77.6** |
| Aligned-reID | 64.8 | 44.2 | 73.8 |
| PPbM-a (their pose-part model) | 74.1 | 51.7 | 76.8 |
| PPbM-b | 72.8 | 47.8 | 77.1 |

**Build the triplet-loss baseline. Do not build PPbM.**

PPbM is a 7-part star model with per-part regional average pooling and
soft-attention aggregation, and it buys **2.8 mAP** over plain triplet loss.
That is not a 24-hour trade. TriHard triplet loss on an ImageNet ResNet-50 at
256 × 128 gets you 96% of the way there in a fraction of the work.

The cross-entropy row is your **hour-6 fallback**: frozen backbone, train two
FC layers, minutes to converge. If the embedder is not working by mid-build,
ship CE and say so.

### Report cross-camera, not single-camera

Cross-camera mAP is **~24 points lower** than single-camera across every
method. Our actual task *is* cross-camera — a tiger photographed at station A
matching one photographed at station B is the entire point.

**So the honest number to report is the cross-camera one, around 47–52 mAP.**
Reporting single-camera would overstate by roughly half again, and a jury
member who has read this paper will know it. Report both and label them.

### Exclude same-event frames from the gallery

The paper excludes temporally adjacent images within one second from query
results when computing AP. We already group bursts within 10 s into one
`event`. **The evaluation must exclude same-event frames from the gallery**,
or you are grading the matcher on finding the frame taken two seconds later —
which is trivially easy and inflates the number toward 95%.

Our `events` table makes this a one-line filter. Use it.

---

## 3. Rectification — Table 4 is the useful table

Rectifying the flank before matching is what handles pose deformation. The
obvious body axis is nose → root of tail. **Table 4 says that is the worst
possible choice.**

Keypoint annotation variance, σ² × 10⁻⁴ (lower is more reliable):

| Most reliable | | Least reliable | |
|---|---|---|---|
| right shoulder (4) | **4.1** | nose (3) | **69.0** |
| left shoulder (6) | 6.9 | right ear (2) | 67.7 |
| left back paw (13) | 6.9 | right front paw (5) | 51.3 |
| left ear (1) | 7.7 | root of tail (14) | 46.7 |
| right hip (8) | 9.1 | left front paw (7) | 41.7 |

The four points that define the trunk — **shoulders (4, 6) and hips (8, 11)** —
are among the most reliably annotated in the dataset. Nose and tail-root, the
intuitive axis, are the two noisiest. Keypoint 15 ("centre") is derived from
nose and tail-root, so it inherits their noise (σ² = 29.0).

**Rectify on the shoulder–hip quadrilateral.** Warp those four points to a
canonical rectangle and crop. This replaces the PCA-on-mask approach in
`docs/BLUEPRINT.md` §7.1 and is strictly better, because ATRW ships the
keypoints as annotations.

### If you train a pose model: HRNet, never OpenPose

| Estimator | AP (OKS = 0.5) |
|---|---|
| OpenPose | **failed to converge** |
| AlphaPose | 57.4 |
| HRNet | 86.9 |

The authors report that modifying OpenPose for the tiger skeleton produced a
non-convergent training run. Two hours you do not have to lose.

Note HRNet reaches 86.9% on tiger pose versus 77.0% on human pose — tiger pose
is, surprisingly, the easier problem.

---

## 4. LILA camera-trap datasets — for blank detection

**ATRW contains no blank frames.** Every clip has a tiger in it. So the entire
first requirement of the problem statement — blank filtering, and the
false-negative rate the jury will ask about — has to be validated on
something else.

| Dataset | Size | Why it fits |
|---|---|---|
| **Caltech Camera Traps** | 243,100 images, 140 locations, 22 categories, ~66k boxes | empty/non-empty labels and a **recommended location-based split** |
| **Wellington Camera Traps** | 270,450 images, 187 locations | cameras record **sequences of three images** per trigger — matches our event model exactly |
| **Snapshot Serengeti** | 7.1M images, ~**76% empty** | scale, and a published MegaDetector baseline to compare against |
| **SWG Camera Traps 2018–2020** | Southeast Asia | closest ecological analogue to Indian tropical forest |

### The methodological point that matters most

**Split by location, not by image.**

Our Stage A prefilter is *literally a per-station background model*. Tune it
on a station and validate on the same station and you are measuring
memorisation, not detection — and the number will look wonderful.

Caltech's recommended split is by camera location precisely for this reason.
Hold out stations. Report the held-out-station number. If you also report the
same-station number, label it as the optimistic bound and say why.

### Two things worth stealing

**Sequence-level label noise is a finding, not an obstacle.** Snapshot
Serengeti's own documentation notes that labels are tied to images but are
only reliable at the *sequence* level — a lion walks out of frame in the third
image of a burst, and all three are still labelled "lion". Our burst grouping
is the correct handling of exactly this. Say so; it turns someone else's known
data problem into a justification for our event model.

**Published MegaDetector results exist for every LILA camera-trap dataset.**
That gives you a free baseline column: our cascade versus published
MegaDetector on the same images. A comparison table you did not have to
generate.

---

## 5. iNaturalist Open Data — for the species gate

Access needs no AWS account:

```
aws s3 ls --no-sign-request s3://inaturalist-open-data/
```

Bucket is in `us-east-1`. Metadata is four tab-separated CSVs —
`observations`, `observers`, `photos`, `taxa` — regenerated monthly. Photos
resolve at
`https://inaturalist-open-data.s3.amazonaws.com/photos/[photo_id]/medium.[ext]`,
with sizes original 2048 px, large 1024, medium 500, small 240, thumb 100.

**Licence: Creative Commons or CC0, varying per image.** You must read the
licence column per photo and honour attribution using the licence, observer
name and observer login. Do not treat the bucket as uniformly free.

### What it is actually for

iNaturalist has **no individual animal identities**, so it contributes nothing
to stripe matching. Its value is the one gap neither other source fills:

**Stage C, the species gate.** A camera-trap detector returns `animal`.
Pugmark needs *which* animal — tiger, leopard, sloth bear, wild boar, chital,
nilgai, dhole, gaur, all of which are at Pench. iNaturalist has labelled,
openly licensed photos of every one of them, including from India. Right now
that classifier has no training data at all; this is where it comes from.

Second use: **hard negatives.** A leopard is spotted, not striped. A stripe
matcher handed a leopard crop must refuse, not enrol a phantom individual. A
few hundred leopard photos make that testable.

### Two cautions

**A different domain gap, not a smaller one.** iNaturalist photos are daylight
tourist and DSLR shots. Camera-trap frames are night IR, motion-blurred and
partial. Train the species gate with heavy augmentation — greyscale, low
light, blur, partial crops — and expect it to transfer imperfectly.

**For a few thousand photos, use the iNaturalist API, not the bucket.** The
full metadata snapshot is tens of gigabytes. Pulling `taxa` → `observations`
→ `photos` for eight species is far cheaper through the API. Use the open-data
bucket when you need redistributable, licence-checked images.

---

## 6. What we still do not have, and must say so

- **No Pench data. No Bengal tiger identities. No Indian camera-trap tiger
  re-ID data at all.**
- ATRW is **Amur** tigers (*P. t. altaica*) from ten Chinese zoos, captured
  with time-synchronised surveillance cameras and tripod-mounted SLRs. Pench
  has **Bengal** tigers (*P. t. tigris*) photographed by camera traps, mostly
  at night on infrared. Two gaps stacked: subspecies and capture modality.
- Detection mAP in the paper tops out around **0.51**, which is why their
  "wild" end-to-end numbers sit below the "plain" ones. Ours will too. Report
  both tracks.

The slide line: *"validated on ATRW, LILA camera-trap collections and
synthetic field scenarios; not yet validated on Pench data — here is exactly
what a pilot would need."* That is honest, complete, and far stronger than a
number you cannot defend.

---

## 7. Evaluation protocol — the numbers to produce

**Blank detection**
Held-out **stations** from Caltech CT and Wellington. Report precision,
recall, a threshold sweep with the operating point marked, and **lead with the
false-negative rate**. Report Stage A, Stage B and end-to-end separately.
Compare against published MegaDetector results on the same data.

**Species gate**
Held-out split of the iNaturalist pull, per-class accuracy, confusion matrix.
Report tiger-vs-leopard separately — it is the confusion that matters.

**Re-identification**
ATRW's published test split, held out **by identity**. Report:
- closed-set top-1 and top-5, **single-camera and cross-camera separately**
- open-set: can max similarity separate a known entity from a new one? AUC
  plus the chosen thresholds
- day versus night, once night augmentation is in
- same-event frames excluded from the gallery

**Alerts**
The eight scenarios in `CLAUDE.md`. Four fire, four suppressed.

**Throughput**
Images/second per cascade stage, on the least powerful laptop on the team,
CPU only, with peak RAM.
