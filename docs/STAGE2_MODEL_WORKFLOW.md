# Stage 2 offline model workflow

Stage 2 has two independent classifiers. Neither is a downloader, a
pretrained model name, or a runtime fallback. Every input image, training
manifest, exported TorchScript module, metadata file, and release manifest is
local to the build machine.

## Artifact contract

The field release contains exactly these executable artifacts:

    edge/models/species/species_classifier.ts
    edge/models/side/flank_side_classifier.ts

Both are TorchScript modules accepting a normalized `N x 3 x 224 x 224` RGB
float tensor and returning exactly one `N x C` logits tensor.

Species class order is fixed:

    tiger, leopard, dhole, sambar, chital, boar, langur, human, vehicle, unknown

Side class order is fixed:

    L, R, UNKNOWN

The exporter writes an adjacent `.ts.json` file with data provenance, model
SHA-256, validation accuracy, and confusion matrix. Keep that sidecar with the
release evidence even though the runtime executes only the `.ts` file.

## Local data manifests

Training and evaluation use JSON Lines. Every source image must already be on
the local build machine; a relative `path` is resolved relative to the
manifest file.

```json
{"path":"images/tiger_001.jpg","split":"train","species":"tiger","side":"L","challenge_tags":["profile"]}
{"path":"images/oblique_014.jpg","split":"val","species":"unknown","side":"UNKNOWN","challenge_tags":["oblique","partial"]}
```

`tools.train_classifiers` refuses a candidate that lacks any runtime label in
either split. This deliberately includes `unknown` and `UNKNOWN`: unresolvable
images are an output class, not a hole in the data.

ATRW can supply local L/R profile labels only:

```powershell
python -m tools.build_atrw_side_manifest
```

It cannot supply `UNKNOWN` examples or the full India/Pench species set. Add
locally collected front/rear, oblique, occluded, partial, blurred, and
multi-tiger crops to the manifest before a candidate can be trained.

## Train, evaluate, package

```powershell
python -m tools.train_classifiers --task side --manifest data/raw/atrw_side/manifest.jsonl
python -m tools.train_classifiers --task species --manifest data/local/species/manifest.jsonl

python -m tools.eval_classifiers --task side --manifest data/local/side/acceptance.jsonl --report data/local/side/report.json
python -m tools.eval_classifiers --task species --manifest data/local/species/acceptance.jsonl --report data/local/species/report.json

python -m tools.prepare_offline_release --write-manifest --prewarm
python -m tools.verify_offline_release
```

Run the evaluator on a machine with OS-level networking disabled. The
acceptance manifests must include these tags: `front`, `rear`, `oblique`,
`occluded_hip`, `multi_tiger`, `partial`, and `motion_blur`. Do not choose an
operating threshold from this document: it is a field-policy value in
`edge/config.py` and must be calibrated from the reported confusion matrices.

No stage may turn an unavailable model, malformed output, insufficient species
confidence, or insufficient side confidence into a tiger identity. Those cases
must remain `unknown`/`UNKNOWN`, evidence-only, and reviewable.
