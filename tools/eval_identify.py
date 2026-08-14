"""Evaluates the trained embedder against ATRW, held out by identity.
See docs/DATA.md §7, docs/MODEL_CHOICES.md, docs/RESULTS.md.

    python -m tools.eval_identify

Requires data/weights/identify_embedder.pt (python -m tools.train_identify).

**Not the official ATRW test set** -- see tools/atrw_dataset.py's module
docstring: its labels were withheld by the CVWC2019 organizers and were
never released. This evaluates against the held-out-identity split of
ATRW's labelled training portion instead, and reports numbers under
that label, not as "the ATRW benchmark."

**Per ENTITY, not per identity -- structurally, not by convention.**
The ATRW paper treats each tiger's two flanks as separate, unrelated
entities (docs/DATA.md §1); scoring an L query against an R gallery
entry is exactly the mistake CLAUDE.md rule 6 exists to prevent, and it
would inflate accuracy for free. evaluate() groups by side before any
query/gallery pair is formed, so a cross-flank match is not possible to
construct. diagnose_side_leakage() proves this with a counterfactual:
it re-runs matching WITHOUT the side split and reports how many top-1
hits would have been opposite-flank -- "zero" is a measured fact here.

**No true cross-camera split is possible from this data, checked twice
now, not assumed.** The ATRW paper states the ORIGINAL cropped re-ID
images were renamed with camera id, shot id, frame number and entity
id. The LILA/CVWC2019-hosted download this build actually uses is not
that: every one of its 5,156 train+test filenames (exhaustively
checked, not sampled) matches a plain `NNNNNN.jpg` sequential pattern,
numbered 1-5155 with no gaps consistent with a compound field encoding.
The distributor stripped the richer naming -- almost certainly because
it would otherwise leak the withheld test identities directly through
the filename. Same-clip exclusion below therefore stays a frame-number-
proximity proxy (GALLERY_ID_EXCLUSION_GAP), stated as a proxy, not a
guarantee; a true single-camera/cross-camera split is not reported
because it cannot honestly be constructed from these files.

Reports, per side, with gallery size and a 95% confidence interval next
to every figure (n=63 and n=104 here -- small enough that a bare point
estimate reads more precise than it is):
    top-1, top-5 (Wilson score interval -- a binomial proportion)
    mAP (percentile bootstrap -- a mean of per-query AP, not a proportion)
    open-set AUC: can max similarity tell "this tiger is in the
        catalogue" from "this tiger has never been seen"? (bootstrap CI)
    threshold calibration: where genuine and impostor score
        distributions suggest t_low/t_high should sit, versus
        edge/config.py's current values, and what share of crops would
        land in auto-accept / review / provisional-enrol at each.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2                    # noqa: E402
import numpy as np            # noqa: E402

from edge import config                                          # noqa: E402
from edge.pipeline.identify import (                              # noqa: E402
    WEIGHTS_PATH, embed, infer_side, load_embedder, match, rectify_flank)
from tools.atrw_dataset import held_out_identity_split, load_labelled  # noqa: E402

GALLERY_ID_EXCLUSION_GAP = 50
"""Frame-number proximity treated as "too close to trust as an
independent gallery match" -- a stated proxy for same-clip exclusion,
not a measured clip boundary. See the module docstring for why a real
one cannot be built from this data."""


def embed_all(rows: list[dict], model, cfg) -> list[dict]:
    """One row per successfully-embedded crop. Crops the quality
    gate/rectifier would refuse in production are silently excluded
    from evaluation too -- evaluating on crops the pipeline would never
    have matched in the first place would overstate accuracy."""
    out = []
    for row in rows:
        side = infer_side(row["keypoints"])
        if side is None:
            continue
        img = cv2.imread(row["orig_path"])
        if img is None:
            continue
        rect = rectify_flank(img, row["keypoints"], side, cfg)
        if rect is None:
            continue
        emb = embed(model, rect)
        frame_id = int(row["file_name"].split(".")[0])
        out.append({"entity_id": f"{row['ind_id']}_{side}", "ind_id": row["ind_id"],
                     "side": side, "frame_id": frame_id, "embedding": emb})
    return out


def average_precision(ranked_entity_ids: list[str], query_entity: str) -> float | None:
    hits, precisions = 0, []
    for rank, eid in enumerate(ranked_entity_ids, start=1):
        if eid == query_entity:
            hits += 1
            precisions.append(hits / rank)
    return (sum(precisions) / hits) if hits else None


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion k/n --
    behaves at small n where a normal approximation can produce an
    interval outside [0, 1]."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z ** 2 / n
    center = (p + z ** 2 / (2 * n)) / denom
    margin = (z / denom) * ((p * (1 - p) / n + z ** 2 / (4 * n ** 2)) ** 0.5)
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def bootstrap_ci(values: list[float], n_boot: int = 2000, alpha: float = 0.05,
                  seed: int = 20260813) -> tuple[float, float] | None:
    """Percentile bootstrap CI for a mean -- used for mAP and AUC,
    neither of which is a simple binomial proportion."""
    if not values:
        return None
    rng = np.random.default_rng(seed)
    arr = np.array(values)
    means = np.sort([float(rng.choice(arr, size=len(arr), replace=True).mean())
                      for _ in range(n_boot)])
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (round(float(lo), 4), round(float(hi), 4))


def evaluate(embedded: list[dict]) -> dict:
    """Per-side closed-set evaluation: top-1, top-5, mAP, each with a
    95% CI, gallery size stated alongside. The query's own near-frame-ID
    (same-entity) crops are excluded from its gallery by construction
    (GALLERY_ID_EXCLUSION_GAP)."""
    by_side: dict[str, list[dict]] = defaultdict(list)
    for e in embedded:
        by_side[e["side"]].append(e)

    per_side: dict[str, dict] = {}
    for side, items in by_side.items():
        top1 = top5 = 0
        ap_values: list[float] = []
        skipped_no_gallery = 0
        for i, query in enumerate(items):
            gallery = [g for j, g in enumerate(items) if j != i and
                       not (g["entity_id"] == query["entity_id"] and
                            abs(g["frame_id"] - query["frame_id"]) <= GALLERY_ID_EXCLUSION_GAP)]
            if not any(g["entity_id"] == query["entity_id"] for g in gallery):
                skipped_no_gallery += 1
                continue
            ranked = match(query["embedding"],
                            [{"entity_id": g["entity_id"], "embedding": g["embedding"]}
                             for g in gallery])
            ranked_ids = [r["entity_id"] for r in ranked]
            if ranked_ids[0] == query["entity_id"]:
                top1 += 1
            if query["entity_id"] in ranked_ids[:5]:
                top5 += 1
            ap = average_precision(ranked_ids, query["entity_id"])
            if ap is not None:
                ap_values.append(ap)

        n_queries = len(items) - skipped_no_gallery
        entities = {e["entity_id"] for e in items}
        per_side[side] = dict(
            n_entities=len(entities), n_images=len(items), n_queries=n_queries,
            skipped_singleton=skipped_no_gallery,
            top1=round(top1 / n_queries, 4) if n_queries else None,
            top1_ci=wilson_ci(top1, n_queries),
            top5=round(top5 / n_queries, 4) if n_queries else None,
            top5_ci=wilson_ci(top5, n_queries),
            mAP=round(sum(ap_values) / len(ap_values), 4) if ap_values else None,
            mAP_ci=bootstrap_ci(ap_values),
        )
    return per_side


def diagnose_side_leakage(embedded: list[dict]) -> dict:
    """Counterfactual: re-run top-1 matching WITHOUT the side split
    (pool L and R together, the mistake CLAUDE.md rule 6 forbids) and
    count how many top-1 hits are opposite-flank matches of the SAME
    individual."""
    opposite_flank_hits = correct_entity_hits = 0
    for i, query in enumerate(embedded):
        gallery = [{"entity_id": g["entity_id"], "ind_id": g["ind_id"], "side": g["side"],
                     "embedding": g["embedding"]}
                    for j, g in enumerate(embedded) if j != i]
        ranked = match(query["embedding"], gallery)
        if not ranked:
            continue
        top = ranked[0]
        if top["entity_id"] == query["entity_id"]:
            correct_entity_hits += 1
        elif top["ind_id"] == query["ind_id"] and top["side"] != query["side"]:
            opposite_flank_hits += 1
    return dict(total=len(embedded), correct_entity_hits=correct_entity_hits,
                opposite_flank_hits=opposite_flank_hits)


def open_set_scores(embedded: list[dict]) -> tuple[list[float], list[float]]:
    """For every held-out crop as query: its genuine max-similarity
    (gallery includes crops of its own entity, past the same-entity
    frame exclusion) and its impostor max-similarity (gallery
    restricted to OTHER entities only, same side -- simulating "this
    tiger is not in the catalogue at all"). Computed per side (an L
    query is never scored against an R gallery here either) and pooled,
    because Identify.t_high/t_low are single global thresholds, not
    per-side ones."""
    by_side: dict[str, list[dict]] = defaultdict(list)
    for e in embedded:
        by_side[e["side"]].append(e)

    genuine, impostor = [], []
    for items in by_side.values():
        for i, query in enumerate(items):
            filtered = [g for j, g in enumerate(items) if j != i and
                        not (g["entity_id"] == query["entity_id"] and
                             abs(g["frame_id"] - query["frame_id"]) <= GALLERY_ID_EXCLUSION_GAP)]
            genuine_candidates = [g for g in filtered if g["entity_id"] == query["entity_id"]]
            if genuine_candidates:
                genuine.append(max(float(np.dot(query["embedding"], g["embedding"]))
                                    for g in genuine_candidates))

            impostor_candidates = [g for g in items if g["entity_id"] != query["entity_id"]]
            if impostor_candidates:
                impostor.append(max(float(np.dot(query["embedding"], g["embedding"]))
                                     for g in impostor_candidates))
    return genuine, impostor


def roc_auc(genuine: list[float], impostor: list[float]) -> float:
    """Mann-Whitney U form of ROC AUC: P(a random genuine score > a
    random impostor score). No sklearn dependency; ties split evenly,
    the standard convention."""
    scored = [(s, 1) for s in genuine] + [(s, 0) for s in impostor]
    scored.sort(key=lambda x: x[0])
    n = len(scored)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and scored[j][0] == scored[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j
    rank_sum_genuine = sum(r for r, (_, label) in zip(ranks, scored) if label == 1)
    n_pos, n_neg = len(genuine), len(impostor)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return (rank_sum_genuine - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def bootstrap_auc_ci(genuine: list[float], impostor: list[float], n_boot: int = 2000,
                      alpha: float = 0.05, seed: int = 20260813) -> tuple[float, float] | None:
    """Percentile bootstrap CI for AUC: resample genuine and impostor
    scores independently (each with replacement, same size as
    original), recompute AUC, repeat."""
    if not genuine or not impostor:
        return None
    rng = np.random.default_rng(seed)
    g_arr, i_arr = np.array(genuine), np.array(impostor)
    aucs = np.sort([roc_auc(list(rng.choice(g_arr, size=len(g_arr), replace=True)),
                             list(rng.choice(i_arr, size=len(i_arr), replace=True)))
                    for _ in range(n_boot)])
    lo = aucs[int((alpha / 2) * n_boot)]
    hi = aucs[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (round(float(lo), 4), round(float(hi), 4))


def calibrate_thresholds(genuine: list[float], impostor: list[float]) -> dict:
    """t_low: set so ~95% of genuine matches still score above it -- a
    real match missed here just costs a human a duplicate provisional
    entry, which is recoverable. t_high: set so ~99% of impostor
    (novel-tiger) scores fall BELOW it -- a novel tiger wrongly
    auto-accepted corrupts the catalogue permanently, so this bound has
    to be the conservative one."""
    g = np.array(sorted(genuine))
    imp = np.array(sorted(impostor))
    t_low = round(float(np.percentile(g, 5)), 3)
    t_high = round(float(np.percentile(imp, 99)), 3)
    return dict(t_low=t_low, t_high=t_high, crossed=t_high < t_low)


def bucket_shares(scores: list[float], t_low: float, t_high: float) -> dict:
    n = len(scores)
    if n == 0:
        return dict(n=0, auto=None, review=None, enroll=None)
    auto = sum(1 for s in scores if s >= t_high)
    review = sum(1 for s in scores if t_low <= s < t_high)
    enroll = sum(1 for s in scores if s < t_low)
    return dict(n=n, auto=round(auto / n, 3), review=round(review / n, 3),
                enroll=round(enroll / n, 3))


def main() -> int:
    if not WEIGHTS_PATH.exists():
        print(f"missing {WEIGHTS_PATH} -- run `python -m tools.train_identify` first")
        return 1
    cfg = config.CONFIG.identify
    model = load_embedder(WEIGHTS_PATH)

    rows = load_labelled("train")
    train_rows, held_rows = held_out_identity_split(rows)
    print(f"held-out identities: {len({r['ind_id'] for r in held_rows})}, "
          f"{len(held_rows)} images")

    embedded = embed_all(held_rows, model, cfg)
    print(f"{len(embedded)} of {len(held_rows)} held-out images embedded "
          f"(rest refused by side-inference or rectification)")

    leak = diagnose_side_leakage(embedded)
    print(f"\nSide-leakage counterfactual: {leak['opposite_flank_hits']} of "
          f"{leak['total']} top-1 matches would have been the same individual's "
          f"opposite flank if side separation were disabled. In the real, "
          f"side-separated pipeline this is structurally zero.")

    per_side = evaluate(embedded)
    print(f"\nClosed-set accuracy, held out by identity, per side, 95% CI on every figure:")
    print(f"{'side':6} {'entities':9} {'images':7} {'queries':8} "
          f"{'top-1 (95% CI)':22} {'top-5 (95% CI)':22} {'mAP (95% CI)':22}")
    for side in sorted(per_side):
        r = per_side[side]
        print(f"{side:6} {r['n_entities']:9} {r['n_images']:7} {r['n_queries']:8} "
              f"{str(r['top1']) + ' ' + str(r['top1_ci']):22} "
              f"{str(r['top5']) + ' ' + str(r['top5_ci']):22} "
              f"{str(r['mAP']) + ' ' + str(r['mAP_ci']):22}")

    print(f"\nNo cross-camera split reported: verified (not assumed) that this "
          f"download's filenames do not carry the camera/shot/frame encoding "
          f"the paper describes for the original release -- see module docstring.")

    genuine, impostor = open_set_scores(embedded)
    auc = roc_auc(genuine, impostor)
    auc_ci = bootstrap_auc_ci(genuine, impostor)
    print(f"\nOpen-set separation: can max similarity tell 'in the catalogue' "
          f"from 'never seen'? n_genuine={len(genuine)} n_impostor={len(impostor)}")
    print(f"  AUC = {auc:.4f}  95% CI {auc_ci}")

    calib = calibrate_thresholds(genuine, impostor)
    print(f"\nThreshold calibration from these score distributions "
          f"(t_low = 5th percentile of genuine scores, t_high = 99th percentile "
          f"of impostor scores):")
    print(f"  current (edge/config.py):  t_low={cfg.t_low}  t_high={cfg.t_high}")
    print(f"  calibrated (this data):    t_low={calib['t_low']}  t_high={calib['t_high']}"
          + ("  ** CROSSED -- t_high < t_low, this data is too thin to support "
             "three clean bands **" if calib["crossed"] else ""))

    print(f"\nShare of crops landing in auto-accept / review / provisional-enrol:")
    print(f"{'':28} {'n':>4} {'auto':>8} {'review':>8} {'enroll':>8}")
    for label, t_low, t_high in [("current thresholds, genuine", cfg.t_low, cfg.t_high),
                                  ("current thresholds, impostor", cfg.t_low, cfg.t_high),
                                  ("calibrated, genuine", calib["t_low"], calib["t_high"]),
                                  ("calibrated, impostor", calib["t_low"], calib["t_high"])]:
        scores = genuine if "genuine" in label else impostor
        b = bucket_shares(scores, t_low, t_high)
        print(f"{label:28} {b['n']:>4} {str(b['auto']):>8} {str(b['review']):>8} "
              f"{str(b['enroll']):>8}")

    print(f"\nThe dangerous number: impostor (novel-tiger) auto-accept rate at "
          f"CURRENT thresholds is "
          f"{bucket_shares(impostor, cfg.t_low, cfg.t_high)['auto']} -- a novel tiger "
          f"wrongly auto-matched to an existing entity, which corrupts the catalogue "
          f"and is not easily undone (CLAUDE.md rule 5: corrections supersede, they "
          f"do not erase the wrong record).")

    print(f"\nCaution on the calibrated values themselves: this is Amur zoo tigers, "
          f"not Bengal tigers on Pench camera traps, and n={len(genuine)+len(impostor)} "
          f"probes total -- the same caution docs/RESULTS.md already applies to "
          f"CCT20 and stage_a_blank_threshold applies here: this calibration is a "
          f"starting point for a real Pench pilot to re-run, not a number to paste "
          f"into edge/config.py as production truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
