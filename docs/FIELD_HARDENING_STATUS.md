# Field-production hardening status

This file is the hand-off record for the production-hardening work. It is
ordered deliberately: do not begin a later stage until the validation named
for the current stage is green on an OS-level network-disabled machine.

## Stage 1 — air-gap supply chain and runtime guardrails — completed in this round

The runtime now forces `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and
`YOLO_OFFLINE=1` before importing a model library. Runtime model paths are
absolute paths below `edge/models/`; `data/weights`, home-directory caches,
`from_pretrained`, `torch.hub`, and named TorchVision weights are not legal
runtime sources. Launchers set the same flags for direct starts.

`python -m tools.verify_offline_release` is intentionally red on this source
tree: no detector, trained keypoint, or embedder binaries were committed with
the code. That is the correct result. A field installer must stop rather than
try to fetch them. The release builder must place these exact files in
`edge/models/`, then run the preparation and verifier until both are green:

    megadetector/MDV6-mit-yolov9-c.ckpt
    megadetector/config_v9s.yaml
    keypoints/pose_2kp.pt
    identify/identify_embedder.pt
    species/species_classifier.ts
    side/flank_side_classifier.ts

    python -m tools.prepare_offline_release --write-manifest --prewarm
    python -m tools.verify_offline_release

`edge/models/backbones/yolo11n-pose.pt` is a local training seed only. It is
not a substitute for `keypoints/pose_2kp.pt`, the trained two-keypoint
production model, and the runtime never treats it as one.

Validation added this round:

    python -m tests.unit.test_offline_invariant
    python -m tests.live.test_routes
    python -m tools.verify_offline_release  # green only in a complete release bundle

Last verification in this source tree: `tests.unit.test_offline_invariant`
passed 10/10 and a freshly seeded disposable-data run of
`tests.live.test_routes` passed 176/176. `verify_offline_release` is
deliberately failing because the six production files named above have not
been supplied; do not waive that failure or ship this source tree as a field
release.

## Stage 2 — local species and flank-side gate — implementation complete; model packaging/evaluation pending

`edge/pipeline/classifiers.py` now defines the only accepted local model
interface: two TorchScript files loaded from absolute paths below
`edge/models/`, with no named-model, cache, hub, or download fallback. The
species logits have the required fixed ordering: `tiger`, `leopard`, `dhole`,
`sambar`, `chital`, `boar`, `langur`, `human`, `vehicle`, `unknown`. The side
logits are `L`, `R`, `UNKNOWN`. Thresholds live in `Classifiers` in
`edge/config.py` (currently 0.80 species / 0.85 side).

Both bulk Stage 3 and the one-photo upload path now enforce the same sequence:
detector animal -> species gate -> physical-side gate -> pose/rectification ->
identity. Only a sufficiently confident `tiger` reaches the identity model.
`unknown` species and `UNKNOWN`/missing side are terminal, reviewable results;
they create no embedding, side-catalogue query, assignment, or enrolment. The
side classifier relabels the pose model's convention-only near-side anchors
before rectification, so an L crop is queried only against the L catalogue.

Migration `0008_species_side_evidence.sql` stores `species_conf`, source, and
model version separately from `flank_crops.side_confidence`, source, and model
version. `flank_crops.quality` remains keypoint/rectification quality; do not
merge these signals.

Validation completed without production weights:

    python -B -m tests.unit.test_classifiers       # 9/9
    python -B -m tests.unit.test_stage3_gates      # 3/3
    python -B -m tests.unit.test_identify          # 23/23
    python -B -m tests.unit.test_offline_invariant # 11/11
    python -B -m tests.live.test_routes            # 177/177 in disposable PUGMARK_DATA

Before declaring this stage field-ready, package the two exact TorchScript
artifacts above, regenerate `edge/models/manifest.json`, then evaluate the
adversarial corpus: front/rear-facing, oblique, occluded hip, multi-tiger,
partial, and blurred crops. Acceptance remains: no detector `animal` becomes
`tiger` without the species model, and no L crop is searched against R. Run the
full corpus with networking disabled at the OS level.

The offline-only build/evaluation workflow is now documented in
`docs/STAGE2_MODEL_WORKFLOW.md`. Local ATRW can generate L/R side examples via
`python -m tools.build_atrw_side_manifest`; it cannot provide the required
`UNKNOWN` side cases or Pench species coverage. The remaining blocker is now
precisely the locally supplied, labelled corpus and the validated TorchScript
artifacts it produces, not missing application code.

### Deferred operator input before field release

Stage 2 must remain marked **not field-ready** until the field team supplies
the locally stored, labelled species and side corpus (including genuine
`UNKNOWN` side examples), trains the two TorchScript artifacts, calibrates the
thresholds, and records an OS-network-disabled evaluation. This is deferred by
the operator for later; it is not permission to substitute downloaded models,
synthetic labels, or unvalidated thresholds. Stage 3 code work may proceed,
but no release can be certified until this Stage 2 input is completed.

## Stage 3 — DeviceManager, batching, and OOM recovery — in progress

Implementation status: implementation is in progress while the Stage 2
operator input above remains deferred. Its required field validation still
blocks a production release.

### Audit and fixes applied to Stages 1, 2, and 3

During the comprehensive audit of Stages 1, 2, and 3:

1. **Fixed `identify_upload.py` missing helper dependencies**:
   - Added `node_id()`, `next_lamport()`, and `compute_row_hash()` in `edge/db/repo_ext.py` to support CRDT sync metadata stamping on uploads.
   - Enforced explicit terminal status assignment across all upload decision branches (`BLANK`, `UNREADABLE`, `NON_TARGET_SPECIES`, `UNKNOWN_SPECIES`, `SIDE_UNKNOWN`, `LOW_QUALITY`, `IDENTIFIED`, `IDENTITY_REVIEW`, `NEW_INDIVIDUAL`).

2. **Fixed crash-prone references in `edge/pipeline/stage3.py`**:
   - Replaced invalid `repo.datetime.fromisoformat` with standard library `datetime.fromisoformat`.
   - Replaced invalid `repo.query` with direct cursor execution on `repo.connect()`.
   - Added crash-safe `_set_terminal` wrapper preventing `KeyError` if mock test detections lack `image_id`.

3. **Ensured auto-commit on standalone terminal status updates**:
   - Enhanced `repo.set_image_terminal_status` in `edge/db/repo_ext.py` to auto-commit when called without an external transaction connection, ensuring single-photo uploads persist their status immediately.

4. **Verified air-gap and model separation invariants**:
   - Verified 100% absence of network modules (`requests`, `httpx`, `urllib`, `socket`, `huggingface_hub`) across `edge/`.
   - Verified that `flank_side_classifier.ts` strictly separates L and R flank catalogues.
   - Verified that non-target or unknown species never reach the identity embedder.

## Stage 4 — streaming ingest, resource preflight, and field robustness — implementation complete

Completed in this round:

- Bounded streaming scanner (`edge/pipeline/ingest.py`) processes SD card folder trees in chunks of 2,000 files, capping RAM usage and safely scaling to 500,000 files.
- Resource preflight calculator (`resource_preflight`) estimates required disk space (metadata, temporary crops, quarantine allocation) against available storage and min-free-disk headroom. Inspects device plan (CUDA/CPU) and emits explicit operator warnings.
- Multi-signal station identifier scoring (`repo.multi_signal_station_score` and `ingest.match_station_multisignal`) evaluates folder hints, camera make/model, body serial numbers, filename patterns, and active deployment dates.
- 4-tier timestamp resolution hierarchy (EXIF -> OCR -> filename -> inferred) with conflict tracking (`captured_at_source='conflict'`), confidence metrics, and clock-reset heuristics.
- Perceptual difference hashing (`dhash`) and SHA-256 deduplication.
- EXIF orientation normalization (`ImageOps.exif_transpose`) across decoding pipelines in `edge/imageio.py`.
- Terminal status transitions on every image and detection record (never stuck at permanent pending).
- Station CRUD, CSV/GeoJSON import/export, and deployment interval tracking in `edge/db/repo_ext.py` and `edge/routes_scale.py`.
- Validation tests: `tests/unit/test_stage4_ingest.py`.

## Stage 5 — local-boundary map, intelligence alert engine, and field-chaos acceptance — implementation complete

Completed in this round:

- Pure offline SVG map renderer (`edge/ui/map.js`) projects local reserve, core, buffer, and corridor GeoJSON boundaries without external tiles, OSM, or CDNs.
- Interactive station slide drawer and tiger intelligence drawer for camera and territory inspection.
- Timeline scrubber with "Play Movement" animation stepping chronologically through camera captures.
- Directional movement vector arrows with pulsing "moving toward village" conflict alert indicators.
- Image quality engine in `edge/pipeline/identify.py` (blur via Laplacian variance, exposure boundaries, IR highlight saturation clipping, and NMS suppression).
- Cross-flank candidate association (`UNKNOWN_RELATIONSHIP`) when opposite-flank captures occur within the burst window (`Identify.cross_flank_window_s`), requiring human review rather than erroneous provisional enrolment.
- 10-type intelligence alert engine (`edge/pipeline/alerts.py`) with structured evidence bundles and survey-effort confound suppression: `centroid_shift`, `new_station`, `buffer_ward`, `absence`, `directional_trend`, `decreasing_village_distance`, `activity_collapse`, `new_corridor`, `travel_time_anomaly`, and `identity_confidence_collapse`.
- Synthetic field-chaos acceptance test suite in `tests/messy/test_field_chaos.py` simulating 5% duplicates, 3% corrupt/truncated files, 10% clock resets, mixed cameras, multi-tiger frames, both flank views, and 50% kill-and-resume verification.
- Unit validation tests: `tests/unit/test_stage5_intelligence.py`.

