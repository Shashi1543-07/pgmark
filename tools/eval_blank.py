"""Stage A blank-detection accuracy, measured against real, labelled
camera-trap data that neither our fixtures nor our threshold were ever
tuned against. See docs/DATA.md §4 and §7, AUDIT_AND_REVISED_PLAN.md
Prompt 7.

    python -m tools.eval_blank

Requires data/raw/cct20/ (python -m tools.fetch_data --set cct20).

Calls edge/pipeline/triage.py's own cell_score() and _score_group() --
the exact functions the production run_triage() calls, not a
reimplementation -- against Caltech Camera Traps' published eccv_18
benchmark subset, which ships four splits precisely so this can be
measured honestly:

    train        -- animal-only, no blanks. Irrelevant here: Stage A is
                     a classical per-station median background, not a
                     trained model, so there is nothing to fit.
    cis_val/test -- held-out IMAGES at the same 10 locations as train.
                     The optimistic bound. Reported, but labelled as
                     such -- docs/DATA.md is explicit that reporting
                     only this number "measures memorisation."
    trans_val/test -- held-out LOCATIONS, never seen anywhere else.
                     The honest number: Stage A's background model is
                     built fresh per station in the field too.

The dangerous error is an animal quarantined as blank, so the positive
class here is ANIMAL PRESENT, matching camera-trap literature (and
MegaDetector's own convention) -- not "blank," which would silently
invert what "false negative" means. false_negative_rate below is the
fraction of true-animal images Stage A wrongly quarantined. That
number, not accuracy, is the one this script leads with.

No SQL, no repo.py, no edge/db -- purely offline dataset evaluation.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge import config                              # noqa: E402
from edge.pipeline.triage import _read_grid, _score_group   # noqa: E402

RAW = Path(__file__).resolve().parents[1] / "data" / "raw" / "cct20"
ANNO_DIR = RAW / "eccv_18_annotation_files"
EMPTY_CATEGORY_ID = 30

SPLITS = ["cis_val", "cis_test", "trans_val", "trans_test"]
SWEEP_THRESHOLDS = [0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10, 0.15, 0.20]


def _find_images_root() -> Path:
    for candidate in RAW.rglob("*.jpg"):
        return candidate.parent.parent if candidate.parent.name != "cct20" else candidate.parent
    raise FileNotFoundError(
        f"No .jpg files found under {RAW} -- run `python -m tools.fetch_data --set cct20` first.")


def _index_images(root: Path) -> dict[str, Path]:
    print(f"  indexing images under {root} ...")
    idx = {p.name: p for p in root.rglob("*.jpg")}
    print(f"  {len(idx):,} images found on disk")
    return idx


def load_split(name: str, image_index: dict[str, Path]) -> list[dict]:
    """One row per image: image_id, station (location), captured_at,
    orig_path, is_animal (ground truth)."""
    data = json.loads((ANNO_DIR / f"{name}_annotations.json").read_text())
    cats_by_image: dict[str, set[int]] = {}
    for a in data["annotations"]:
        cats_by_image.setdefault(a["image_id"], set()).add(a["category_id"])

    rows = []
    missing = 0
    for im in data["images"]:
        path = image_index.get(im["file_name"])
        if path is None:
            missing += 1
            continue
        cats = cats_by_image.get(im["id"], set())
        is_animal = cats != {EMPTY_CATEGORY_ID}
        rows.append(dict(
            image_id=im["id"], station_id=str(im["location"]),
            captured_at=im.get("date_captured") or "", orig_path=str(path),
            is_animal=is_animal))
    if missing:
        print(f"  ({missing} images listed in {name} but not found on disk -- skipped)")
    return rows


def load_grids(rows: list[dict], grid_n: int) -> list[tuple[list[dict], dict]]:
    """Reads every image ONCE, grouped by station (Stage A is a
    per-station model) -- the expensive part (JPEG decode + resize for
    tens of thousands of files), done once regardless of how many
    threshold values get swept afterwards. Returns a list of
    (readable_rows, grids) per station group."""
    by_station: dict[str, list[dict]] = {}
    for r in rows:
        by_station.setdefault(r["station_id"], []).append(r)

    groups = []
    for station_rows in by_station.values():
        grids = {}
        readable = []
        for r in station_rows:
            grid = _read_grid(r["orig_path"], grid_n, band_frac=0.0)
            if grid is None:
                continue
            grids[r["image_id"]] = grid
            readable.append(r)
        if readable:
            groups.append((readable, grids))
    return groups


def score_groups(groups: list[tuple[list[dict], dict]], cfg) -> dict[str, tuple[bool, float]]:
    """Cheap: reuses grids already in memory, only cell_score's numpy
    comparison and the threshold re-run per station group."""
    decisions: dict[str, tuple[bool, float]] = {}
    for readable, grids in groups:
        decisions.update(_score_group(readable, grids, cfg))
    return decisions


def confusion(rows: list[dict], decisions: dict[str, tuple[bool, float]]) -> dict:
    tp_animal = fn_animal = fp_blank_kept = tn_blank = unreadable = 0
    for r in rows:
        d = decisions.get(r["image_id"])
        if d is None:
            unreadable += 1
            continue
        quarantined, _ = d
        if r["is_animal"]:
            if quarantined:
                fn_animal += 1     # the dangerous error
            else:
                tp_animal += 1
        else:
            if quarantined:
                tn_blank += 1
            else:
                fp_blank_kept += 1     # safe: costs review time, not data
    n_animal = tp_animal + fn_animal
    n_blank = tn_blank + fp_blank_kept
    return dict(
        n_animal=n_animal, n_blank=n_blank, unreadable=unreadable,
        false_negative_rate=round(fn_animal / n_animal, 4) if n_animal else None,
        animal_recall=round(tp_animal / n_animal, 4) if n_animal else None,
        blank_catch_rate=round(tn_blank / n_blank, 4) if n_blank else None,
        precision_of_quarantine=(round(tn_blank / (tn_blank + fn_animal), 4)
                                  if (tn_blank + fn_animal) else None),
        fn_animal=fn_animal, tp_animal=tp_animal,
        tn_blank=tn_blank, fp_blank_kept=fp_blank_kept,
    )


def main() -> int:
    if not ANNO_DIR.exists():
        print(f"missing {ANNO_DIR} -- run `python -m tools.fetch_data --set cct20` first")
        return 1
    root = _find_images_root()
    image_index = _index_images(root)

    base_cfg = config.CONFIG.triage
    operating_threshold = base_cfg.stage_a_blank_threshold

    splits = {}
    split_groups = {}
    for name in SPLITS:
        print(f"loading {name} ...")
        rows = load_split(name, image_index)
        splits[name] = rows
        split_groups[name] = load_grids(rows, base_cfg.stage_a_grid)

    print(f"\nOperating point: stage_a_blank_threshold = {operating_threshold} "
          f"(edge/config.py -- not retuned to this dataset)\n")

    results_at_operating = {}
    for name, rows in splits.items():
        decisions = score_groups(split_groups[name], base_cfg)
        results_at_operating[name] = confusion(rows, decisions)

    print("At the configured operating point:")
    print(f"{'split':12} {'n_animal':>9} {'n_blank':>8} {'FN rate':>9} "
          f"{'animal recall':>14} {'blank caught':>13}")
    for name in SPLITS:
        r = results_at_operating[name]
        print(f"{name:12} {r['n_animal']:>9} {r['n_blank']:>8} "
              f"{r['false_negative_rate']:>9} {r['animal_recall']:>14} "
              f"{r['blank_catch_rate']:>13}")

    print("\nHEADLINE (held-out stations, trans_test): false-negative rate = "
          f"{results_at_operating['trans_test']['false_negative_rate']} "
          f"({results_at_operating['trans_test']['fn_animal']} of "
          f"{results_at_operating['trans_test']['n_animal']} true-animal images "
          "wrongly quarantined)")
    print("Optimistic bound (same stations as any tuning, cis_test): false-negative rate = "
          f"{results_at_operating['cis_test']['false_negative_rate']} "
          "-- labelled as optimistic per docs/DATA.md §4, not the headline number")

    print("\nThreshold sweep (trans_test, the held-out-station split):")
    print(f"{'threshold':>10} {'FN rate':>9} {'animal recall':>14} "
          f"{'blank caught':>13} {'quarantine precision':>21}")
    trans_test_rows = splits["trans_test"]
    trans_test_groups = split_groups["trans_test"]
    for t in SWEEP_THRESHOLDS:
        cfg_t = replace(base_cfg, stage_a_blank_threshold=t)
        decisions = score_groups(trans_test_groups, cfg_t)
        r = confusion(trans_test_rows, decisions)
        marker = "  <- operating point" if abs(t - operating_threshold) < 1e-9 else ""
        fmt = lambda v: "n/a" if v is None else str(v)   # noqa: E731 -- nothing quarantined yet at this threshold
        print(f"{t:>10} {fmt(r['false_negative_rate']):>9} {fmt(r['animal_recall']):>14} "
              f"{fmt(r['blank_catch_rate']):>13} {fmt(r['precision_of_quarantine']):>21}{marker}")

    print("\nStage B (the detector) is not built in this deployment -- there is no "
          "end-to-end number to report. Every frame Stage A does not confidently call "
          "blank stays pending, genuinely awaiting a detector that does not exist yet "
          "(CLAUDE.md rule 8). The numbers above are Stage A alone.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
