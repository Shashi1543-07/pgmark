"""Why did this run identify zero tigers?

Reads the model bundle and the database and reports, in order, exactly which
gate every detection died at. Answers the difference between four causes that
look identical from the interface:

    1. a model file is missing or will not load
    2. the model loads but its confidence never reaches the threshold
    3. the model is confident and simply says "not a tiger"
    4. the frames really did contain no tiger

Read-only. Touches nothing.

    python -m tools.diagnose_gates              # newest run
    python -m tools.diagnose_gates RUN_ID
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import config                      # noqa: E402
from edge.db import repo                     # noqa: E402


def rule(title: str = "") -> None:
    # Plain ASCII, not box-drawing characters: this tool has to run on a
    # field laptop whose console codepage is not guaranteed to be UTF-8, and
    # a UnicodeEncodeError here would replace a diagnostic with a crash --
    # the exact failure mode this whole tool exists to avoid.
    print("\n" + (f"== {title} " .ljust(74, "=")) if title else "=" * 74)


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):5.1f}%" if d else "    - "


# ── 1. the bundle on disk ────────────────────────────────────────────────

def check_models() -> dict[str, bool]:
    rule("MODEL BUNDLE")
    print(f"looking in: {config.MODELS_DIR}")
    if not config.MODELS_DIR.is_dir():
        print("\n  edge/models/ DOES NOT EXIST. Nothing below can work.")
        return {}

    present: dict[str, bool] = {}
    for name, path in config.runtime_model_paths().items():
        ok = path.exists()
        present[name] = ok
        size = f"{path.stat().st_size / 1e6:8.1f} MB" if ok else "        -"
        print(f"  {'OK  ' if ok else 'GONE'}  {name:24s} {size}   {path.name}")

    missing = [n for n, ok in present.items() if not ok]
    if missing:
        print("\n  Missing:", ", ".join(missing))
        print("  Every classifier fails CLOSED to 'unknown' when its file is absent,")
        print("  so a missing species model sends 100% of animals to human review.")
    return present


# ── 2. do they actually load and run? ────────────────────────────────────

def check_loading() -> None:
    rule("CAN THE CLASSIFIERS LOAD?")
    try:
        import numpy as np
        from edge.pipeline import classifiers
    except Exception as exc:                                     # noqa: BLE001
        print(f"  cannot import the pipeline: {type(exc).__name__}: {exc}")
        return

    # A grey square is meaningless input on purpose. We are testing whether
    # the model file loads and returns a well-shaped tensor, not whether it
    # is accurate -- a load failure and a low-confidence answer are the two
    # cases the interface cannot tell apart, and only the first is a bug.
    probe = np.full((240, 240, 3), 127, dtype=np.uint8)
    box = (0.1, 0.1, 0.8, 0.8)

    for label, fn in (("species", classifiers.classify_species),
                      ("side", classifiers.classify_side)):
        try:
            r = fn(probe, box)
        except Exception as exc:                                 # noqa: BLE001
            print(f"  {label:8s} RAISED  {type(exc).__name__}: {exc}")
            continue
        if r.source == "unavailable":
            print(f"  {label:8s} UNAVAILABLE - fails closed to '{r.label}'")
            print(f"           reason: {r.detail}")
        else:
            print(f"  {label:8s} loaded and ran   model={r.model_version}")


# ── 3. where the detections actually died ────────────────────────────────

GATE_MEANING = {
    "UNKNOWN_SPECIES": "species classifier was not confident enough (or unavailable)",
    "NON_TARGET_SPECIES": "classifier was confident this is NOT a tiger",
    "SIDE_UNKNOWN": "tiger confirmed, but left/right flank could not be decided",
    "LOW_QUALITY": "side known, but pose keypoints too poor to straighten the flank",
    "IDENTITY_REVIEW": "stripes extracted, match was borderline - sent to a human",
    "IDENTIFIED": "matched to a tiger already in the record",
    "NEW_INDIVIDUAL": "enrolled as a tiger not seen before",
    "BLANK": "no animal in the frame",
    "PERSON": "person - blurred and routed away from the tiger record",
    "VEHICLE": "vehicle",
    "CORRUPT": "file could not be opened",
    "UNREADABLE": "file could not be decoded",
}


def diagnose_run(run_id: str | None) -> None:
    conn = repo.connect()

    # An un-migrated or absent database is a normal state on a fresh laptop,
    # not a crash. Say so plainly rather than surfacing a sqlite traceback to
    # someone who is already trying to work out why nothing identified.
    try:
        conn.execute("SELECT 1 FROM runs LIMIT 1")
    except Exception:                                            # noqa: BLE001
        rule("RUNS")
        print(f"  No database at {config.DB_PATH}, or it has no runs table yet.")
        print("  Start the application once to create it, then process a card.")
        return

    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            rule("RUNS")
            print("  The database exists but no card has been processed yet.")
            return
        run_id = row["run_id"]

    rule(f"RUN {run_id}")
    total = conn.execute("SELECT COUNT(*) c FROM images WHERE run_id=?",
                         (run_id,)).fetchone()["c"]
    print(f"  {total} frames ingested")

    print("\n  Where every frame ended up:")
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM images WHERE run_id=?"
        " GROUP BY status ORDER BY c DESC", (run_id,)).fetchall()
    for r in rows:
        meaning = GATE_MEANING.get(r["status"], "")
        print(f"    {r['c']:6d}  {pct(r['c'], total)}  {r['status']:20s} {meaning}")

    # ── the species gate ────────────────────────────────────────────────
    rule("SPECIES GATE")
    thr = config.CONFIG.classifiers.species_min_confidence
    print(f"  threshold: a detection must score >= {thr:.2f} to be called a tiger\n")
    rows = conn.execute(
        "SELECT d.species, d.species_source, COUNT(*) c,"
        " MIN(d.species_conf) lo, AVG(d.species_conf) av, MAX(d.species_conf) hi"
        " FROM detections d JOIN images i ON i.image_id=d.image_id"
        " WHERE i.run_id=? AND d.label='animal'"
        " GROUP BY d.species, d.species_source ORDER BY c DESC", (run_id,)).fetchall()
    if not rows:
        print("  No animal detections at all - the detector found nothing.")
    for r in rows:
        conf = ("never scored" if r["av"] is None
                else f"min {r['lo']:.2f} / avg {r['av']:.2f} / max {r['hi']:.2f}")
        print(f"    {r['c']:6d}  species={str(r['species']):10s}"
              f" source={str(r['species_source']):12s} {conf}")

    unavail = conn.execute(
        "SELECT COUNT(*) c FROM detections d JOIN images i ON i.image_id=d.image_id"
        " WHERE i.run_id=? AND d.label='animal' AND d.species_source='unavailable'",
        (run_id,)).fetchone()["c"]
    near = conn.execute(
        "SELECT COUNT(*) c FROM detections d JOIN images i ON i.image_id=d.image_id"
        " WHERE i.run_id=? AND d.label='animal' AND d.species='unknown'"
        "   AND d.species_conf >= ?", (run_id, thr * 0.75)).fetchone()["c"]

    if unavail:
        print(f"\n  >> {unavail} detections got species_source='unavailable'.")
        print("     The model did not run. This is a FILE problem, not an accuracy problem.")
    elif near:
        print(f"\n  >> {near} detections scored within 25% of the threshold and were")
        print(f"     still rejected. The model IS running but is under-confident.")
        print(f"     Lower classifiers.species_min_confidence from {thr:.2f} and re-run,")
        print("     or retrain - but a lower gate means more wrong species reaching")
        print("     the tiger catalogue, so change it deliberately.")

    # ── the side gate ───────────────────────────────────────────────────
    rule("FLANK SIDE GATE")
    sthr = config.CONFIG.classifiers.side_min_confidence
    print(f"  threshold: left/right must score >= {sthr:.2f}\n")
    rows = conn.execute(
        "SELECT c.side, c.side_source, COUNT(*) n,"
        " MIN(c.side_confidence) lo, AVG(c.side_confidence) av, MAX(c.side_confidence) hi"
        " FROM flank_crops c JOIN detections d ON d.det_id=c.det_id"
        " JOIN images i ON i.image_id=d.image_id WHERE i.run_id=?"
        " GROUP BY c.side, c.side_source ORDER BY n DESC", (run_id,)).fetchall()
    if not rows:
        print("  No crops were made - nothing reached this gate.")
    for r in rows:
        conf = ("never scored" if r["av"] is None
                else f"min {r['lo']:.2f} / avg {r['av']:.2f} / max {r['hi']:.2f}")
        print(f"    {r['n']:6d}  side={str(r['side']):8s}"
              f" source={str(r['side_source']):12s} {conf}")

    # ── what came out the far end ───────────────────────────────────────
    rule("RESULT")
    inds = conn.execute("SELECT COUNT(*) c FROM individuals").fetchone()["c"]
    emb = conn.execute(
        "SELECT COUNT(*) c FROM flank_crops c JOIN detections d ON d.det_id=c.det_id"
        " JOIN images i ON i.image_id=d.image_id"
        " WHERE i.run_id=? AND c.embedding IS NOT NULL", (run_id,)).fetchone()["c"]
    q_open = conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE state='open'").fetchone()["c"]
    q_stuck = conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE state='claimed'").fetchone()["c"]
    q_blind = conn.execute(
        "SELECT COUNT(*) c FROM review_queue q JOIN flank_crops c ON c.crop_id=q.crop_id"
        " WHERE q.state='open' AND c.rect_ok=0").fetchone()["c"]

    print(f"  tigers in the record          {inds}")
    print(f"  stripe patterns extracted     {emb}")
    print(f"  review queue, open            {q_open}")
    print(f"    of those, nothing to match  {q_blind}   (dismiss-only items)")
    print(f"  review queue, held by a lock  {q_stuck}")

    print()
    if emb == 0 and q_blind:
        print("  VERDICT: not one stripe pattern was extracted, so identification")
        print("  never began. Every queue item is dismiss-only because there is no")
        print("  embedding to compare. Zero tigers is the CORRECT output of a run")
        print("  that was refused at the species or side gate - fix the gate above,")
        print("  then re-run Stage 3. The photographs are untouched.")
    elif emb and inds == 0:
        print("  VERDICT: stripe patterns WERE extracted but nothing was enrolled.")
        print("  That points at enrolment, not the classifiers. Check identify.t_low.")
    elif inds:
        print(f"  VERDICT: identification is working - {inds} tigers on record.")

    if q_stuck:
        print(f"\n  NOTE: {q_stuck} item(s) sit in state='claimed'. Before migration 0010")
        print("  these were unrecoverable from the interface. They now expire after")
        print("  15 minutes and return to the queue on their own.")


def main() -> int:
    check_models()
    check_loading()
    diagnose_run(sys.argv[1] if len(sys.argv) > 1 else None)
    rule()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
