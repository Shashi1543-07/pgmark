"""Apply the v0.2.0 hardening patches to the files that already exist.

New files are dropped in whole (see MEGAFIX_APPLY.md). This script handles
the six existing files that need surgical edits, because regenerating a
1,000-line module to change forty lines of it is how a fix introduces its
own regressions.

Idempotent: every edit checks for its own marker first, so running this
twice is a no-op. Run from the repo root:

    python -m tools.apply_megafix              # apply
    python -m tools.apply_megafix --check      # report, change nothing

Each patch below states the failure it fixes. They are not stylistic.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
applied: list[str] = []
skipped: list[str] = []
missing: list[str] = []


def patch(path: str, marker: str, old: str, new: str, why: str, *, check: bool) -> None:
    """Replace `old` with `new` in `path`, unless `marker` is already there."""
    p = ROOT / path
    if not p.exists():
        missing.append(f"{path} (file not found)")
        return
    s = p.read_text(encoding="utf-8")
    if marker in s:
        skipped.append(f"{path}: {why} (already applied)")
        return
    if old not in s:
        missing.append(f"{path}: could not find the anchor for '{why}' — "
                       "this file may have already been modified; apply by hand")
        return
    if not check:
        p.write_text(s.replace(old, new, 1), encoding="utf-8")
    applied.append(f"{path}: {why}")


def append(path: str, marker: str, text: str, why: str, *, check: bool) -> None:
    p = ROOT / path
    if not p.exists():
        missing.append(f"{path} (file not found)")
        return
    s = p.read_text(encoding="utf-8")
    if marker in s:
        skipped.append(f"{path}: {why} (already applied)")
        return
    if not check:
        p.write_text(s + text, encoding="utf-8")
    applied.append(f"{path}: {why}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    args = ap.parse_args()
    ck = args.check

    # ═════════════════════════════════════════════════════════════════════
    # 1. repo.py — expose the extension module
    # ═════════════════════════════════════════════════════════════════════
    append("edge/db/repo.py", "repo_ext",
           "\n\n# ── scale/hardening extension (v0.2.0) ─────────────────────────────────\n"
           "# Rule 1 still holds: one import path (`repo.`), one place to look.\n"
           "# See edge/db/repo_ext.py for why the additions live in their own file.\n"
           "from edge.db.repo_ext import *  # noqa: E402,F401,F403\n",
           "expose repo_ext (transactions, batch writes, occupancy inputs)", check=ck)

    # ═════════════════════════════════════════════════════════════════════
    # 2. config.py — the three settings the safety gates need
    #
    # WHY: Stage B answers `animal`, not `tiger`, and the keypoint model
    # labels every prediction 'right_*' regardless of the flank actually
    # shown. v0.1.1 had no switch for either fact, so bulk identification
    # would enrol sambar as tigers and score left flanks against the right
    # catalogue — 50,000 times, silently.
    # ═════════════════════════════════════════════════════════════════════
    patch("edge/config.py", "require_side_classifier",
          "    rect_body_depth_ratio: float = 0.6",
          '''    require_side_classifier: bool = True
    """Bulk Stage 3 will not auto-assign or auto-enrol while this is True.

    edge/pipeline/keypoints.py labels every prediction "right_shoulder"/
    "right_hip" regardless of which flank is actually showing -- confirmed
    empirically in docs/RESULTS.md's wild evaluation, where every held-out
    image came back side='R' and none 'L'. On a real deployment roughly
    half of captures show the left flank and would be scored against the
    RIGHT-side catalogue. One photograph at a time that is a bad match a
    human can catch; 50,000 at a time it is a corrupted catalogue.

    While True, crops and embeddings are still computed and stored -- the
    work is not thrown away -- but every decision goes to the review queue
    with the reason stated. Set False only once a side classifier exists.
    CLAUDE.md rule 8: refusing to answer is a valid output."""

    species_gate: str = "review"
    """strict | review | off.

    MegaDetector's `animal` class is not `tiger`. v0.1.1 sent every animal
    detection into the flank pipeline, so a leopard, sambar or wild dog
    could be embedded, scored against the tiger catalogue, and below t_low
    enrolled as a brand-new tiger. On a real reserve most animal detections
    are not tigers, so that is the common case, not the rare one.

      strict -- only detections confirmed as target_species proceed. With
                no species classifier installed, that is none, and the run
                says so rather than pretending.
      review -- crops and embeddings are computed, decisions go to a human.
                The honest default while no classifier exists.
      off    -- v0.1.1's behaviour. Recorded in the audit log; not advised."""

    target_species: str = "tiger"

    rect_body_depth_ratio: float = 0.6''',
          "add species gate + side-classifier gate", check=ck)

    # Batch/thread controls. Torch defaults to all cores, which on a 4-core
    # range-office laptop makes the machine unusable for anything else while
    # a 50,000-frame run is going.
    patch("edge/config.py", "batch_size: int",
          "    seconds_per_manual_review: float = 3.0",
          '''    batch_size: int = 200
    """Frames per checkpointed batch. A crash loses at most one batch."""

    torch_threads: int = 0
    """0 = leave PyTorch's default (every core). On a 4-core range-office
    laptop that makes the machine unusable for anything else for the hours
    a 50K run takes. 2 or 3 leaves the officer a working computer."""

    seconds_per_manual_review: float = 3.0''',
          "add batch size + CPU thread cap", check=ck)

    # ═════════════════════════════════════════════════════════════════════
    # 3. triage.py — THE DATA-LOSS BUG
    #
    # v0.1.1's own module docstring promises: "manifest.json is written
    # before the DB row -- restore() reverses it from that manifest alone,
    # so it survives the database being lost".
    #
    # It does not. `_quarantine_file()` moves the file and appends to an
    # in-memory list; `manifest.json` is written only after the ENTIRE loop
    # over every station finishes. Crash at frame 30,000 of 50,000 and
    # 30,000 original frames have been physically moved into
    # data/quarantine/<run_id>/ with NO manifest on disk and NO database
    # rows. The mapping from quarantine path back to original path exists
    # nowhere. That is unrecoverable field data, and the docstring says the
    # opposite.
    # ═════════════════════════════════════════════════════════════════════
    patch("edge/pipeline/triage.py", "_append_manifest",
          "def _quarantine_move(run_id: str, row: dict, reason: str, conf: float,",
          '''def _append_manifest(quarantine_dir: Path, name: str, entries: list[dict]) -> None:
    """Write manifest entries BEFORE the files they describe are moved.

    This is the fix for the worst bug in v0.1.1. The module docstring above
    claims the manifest is written before the DB row so restore() survives
    losing the database -- but the write happened after the whole
    station loop, so a crash mid-run left thousands of original frames
    physically moved into quarantine with nothing anywhere recording where
    they came from.

    Append-then-move, per batch, with an fsync. The worst case is now a
    manifest entry for a file that was never moved, which restore()
    already tolerates (it checks src.exists() first) -- the opposite and
    survivable failure.
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    path = quarantine_dir / name
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = []
    have = {e["image_id"] for e in existing}
    merged = existing + [e for e in entries if e["image_id"] not in have]
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.flush()
        import os as _os
        _os.fsync(fh.fileno())
    tmp.replace(path)


def _quarantine_move(run_id: str, row: dict, reason: str, conf: float,''',
          "FIX DATA LOSS: write the quarantine manifest before moving files", check=ck)

    # Stage A decode: 143.7 ms/frame -> 38.4 ms/frame (measured on a
    # 4000x3000 JPEG). Two hours -> 32 minutes for 50,000 frames.
    patch("edge/pipeline/triage.py", "imageio.read_grid",
          '''    path = Path(orig_path)
    try:
        with Image.open(path) as img:
            img = img.convert("L")
            w, h = img.size
            band = int(h * band_frac)
            if 0 < band < h:
                img = img.crop((0, 0, w, h - band))
            img = img.resize((grid_n, grid_n))
            return np.asarray(img, dtype=np.uint8)
    except Exception:
        return None''',
          '''    # Delegates to edge/imageio.py, which asks libjpeg to decode the JPEG's
    # DCT coefficients at 1/8 scale instead of building the full 12-megapixel
    # bitmap and immediately throwing 99.99% of it away. Measured on a
    # 4000x3000 camera-trap frame: 143.7 ms -> 38.4 ms, which is the
    # difference between two hours and half an hour over 50,000 frames.
    # It oversamples to grid_n*8 before the final resize so cell_score()'s
    # calibration (tests/unit/test_triage_scoring.py) is unchanged.
    from edge import imageio
    return imageio.read_grid(orig_path, grid_n, band_frac)''',
          "3.7x faster Stage A decode", check=ck)

    # A vehicle is not a subject. v0.1.1 set status='subject' for vehicle
    # frames, so a forest department jeep landed in the same bucket as a
    # tiger and was offered to Stage 3 for identification.
    patch("edge/pipeline/triage.py", "not a subject. v0.1.1 wrote",
          '''        elif any(d.label == detector_pipeline.VEHICLE_LABEL for d in detections):
            repo.set_image_status(row["image_id"], "subject", "B")
            vehicle += 1''',
          '''        elif any(d.label == detector_pipeline.VEHICLE_LABEL for d in detections):
            # A jeep is not a subject. v0.1.1 wrote status='subject' here,
            # so every vehicle frame joined the animal frames in the list
            # the UI offered for identification, and Stage 3 would try to
            # find a shoulder and a hip on a Mahindra Bolero.
            repo.set_image_status(row["image_id"], "vehicle", "B")
            vehicle += 1''',
          "vehicle frames get their own status, not 'subject'", check=ck)

    # triage.py imports the detector at module scope too, and edge/app.py
    # imports triage -- so the server still died on a torch-less machine
    # even after app.py's own import was made lazy. Found by booting the
    # patched tree, not by reading it.
    patch("edge/pipeline/triage.py", "_detector_mod()",
          "from edge.pipeline import detector as detector_pipeline",
          '''
def _detector_mod():
    """Import edge/pipeline/detector.py on first use, not at module import.

    detector.py imports torch at module scope, triage.py imported detector
    at module scope, and app.py imports triage -- so one missing optional
    dependency for one stage killed the entire server, including every
    screen that needs no model at all. Stage B already refuses gracefully
    when the WEIGHTS are absent (CLAUDE.md rule 8); it should do the same
    when the RUNTIME is."""
    from edge.pipeline import detector
    return detector''',
          "FIX: triage no longer imports torch at module scope", check=ck)

    patch("edge/pipeline/triage.py", "detector_pipeline = _detector_mod()",
          '''    pending = [i for i in repo.images_for_run(run_id)
               if i["status"] == "pending" and i["triage_stage"] == "A"]
    subject = person = vehicle = blank = awaiting_detector = 0''',
          '''    detector_pipeline = _detector_mod()
    pending = [i for i in repo.images_for_run(run_id)
               if i["status"] == "pending" and i["triage_stage"] == "A"]
    subject = person = vehicle = blank = awaiting_detector = 0''',
          "resolve the detector inside _run_stage_b", check=ck)

    patch("edge/pipeline/triage.py", "detector_pipeline = _detector_mod()\n    src = Path",
          '''    src = Path(row["orig_path"])
    blurred_path = config.RESTRICTED_DIR''',
          '''    detector_pipeline = _detector_mod()
    src = Path(row["orig_path"])
    blurred_path = config.RESTRICTED_DIR''',
          "resolve the detector inside _restrict_person", check=ck)

    # ═════════════════════════════════════════════════════════════════════
    # 4. ingest.py — cross-run duplicate handling
    #
    # image_id is sha256[:16]: content-addressed and therefore identical
    # across runs. repo.insert_many() uses INSERT OR REPLACE. Scanning the
    # same card into a second run therefore OVERWRITES the first run's rows
    # -- run_id and status lost, every detection/crop/assignment hanging
    # off them orphaned, no error anywhere. Reproduced against the seeded
    # demo database.
    # ═════════════════════════════════════════════════════════════════════
    patch("edge/pipeline/ingest.py", "already ingested by an earlier run",
          '''    image_rows = [_to_image_row(r, run_id, reserve_id, folder_station[r["folder"]], node)
                  for r in records if not r.get("duplicate_of")]
    repo.insert_many("images", image_rows)''',
          '''    candidate_rows = [_to_image_row(r, run_id, reserve_id, folder_station[r["folder"]], node)
                      for r in records if not r.get("duplicate_of")]

    # Content already ingested by an earlier run. image_id is a SHA-256
    # prefix, so the same photograph produces the same primary key in every
    # run -- and repo.insert_many()'s INSERT OR REPLACE would silently
    # overwrite the earlier run's row, dropping its run_id and status and
    # orphaning every detection, crop and assignment beneath it. Record the
    # overlap instead of destroying it.
    already = repo.existing_image_ids([r["image_id"] for r in candidate_rows])
    image_rows = [r for r in candidate_rows if r["image_id"] not in already]
    cross_run_duplicates = len(candidate_rows) - len(image_rows)
    repo.insert_many_ignore("images", image_rows)
    repo.connect().commit()''',
          "FIX DATA LOSS: never overwrite another run's image rows", check=ck)

    patch("edge/pipeline/ingest.py", '"cross_run_duplicates"',
          '''        "duplicate_count": duplicate_count,
        "corrupt_count": corrupt_count,''',
          '''        "duplicate_count": duplicate_count,
        "cross_run_duplicates": cross_run_duplicates,
        "cross_run_note": (
            f"{cross_run_duplicates} files in this folder were already ingested by an "
            "earlier run and were not re-imported. Their existing rows, and everything "
            "identified from them, are untouched." if cross_run_duplicates else None),
        "corrupt_count": corrupt_count,''',
          "report cross-run duplicates in the preflight", check=ck)

    # ═════════════════════════════════════════════════════════════════════
    # 5. app.py — lazy model import, new routes, postprocess after review
    #
    # `import edge.app` currently raises ModuleNotFoundError: No module
    # named 'torch' on any machine where the model stack did not install.
    # The ENTIRE server dies -- triage stats, the map, alerts, the audit
    # log, the catalogue, all of it -- because one optional dependency for
    # one stage is imported at module scope. Verified by running it.
    # ═════════════════════════════════════════════════════════════════════
    patch("edge/app.py", "identify_upload is imported lazily",
          "from edge.pipeline import identify_upload, ingest\n"
          "from edge.pipeline import triage as triage_pipeline",
          "from edge.pipeline import ingest\n"
          "# identify_upload is imported lazily inside the two routes that need it.\n"
          "# It pulls in torch via edge/pipeline/detector.py, and importing it here\n"
          "# meant a laptop with a broken torch install could not start the server at\n"
          "# all -- no map, no alerts, no audit log, no catalogue, because one\n"
          "# optional model dependency for one stage was imported at module scope.\n"
          "# Verified: `import edge.app` raised ModuleNotFoundError before this change.\n"
          "from edge.pipeline import triage as triage_pipeline",
          "FIX: server no longer dies when torch is absent", check=ck)

    patch("edge/app.py", "from edge.pipeline import identify_upload  # noqa: PLC0415",
          '''    try:
        return identify_upload.process_upload(
            image["orig_path"], run["reserve_id"], image["station_id"], actor)''',
          '''    from edge.pipeline import identify_upload  # noqa: PLC0415
    try:
        return identify_upload.process_upload(
            image["orig_path"], run["reserve_id"], image["station_id"], actor)''',
          "lazy import in identify_run_image", check=ck)

    patch("edge/app.py", "identify_upload  # noqa: PLC0415, F811",
          '''    dest.write_bytes(await file.read())
    try:''',
          '''    dest.write_bytes(await file.read())
    from edge.pipeline import identify_upload  # noqa: PLC0415, F811
    try:''',
          "lazy import in identify_upload_route", check=ck)

    patch("edge/app.py", "postprocess.after_review_decision",
          '''    try:
        return repo.review_decide(queue_id, ind_id,
                                  payload.get("actor", "director"),
                                  bool(payload.get("new_individual")))
    except KeyError:
        raise HTTPException(404, "queue item not found")''',
          '''    try:
        result = repo.review_decide(queue_id, ind_id,
                                    payload.get("actor", "director"),
                                    bool(payload.get("new_individual")))
    except KeyError:
        raise HTTPException(404, "queue item not found")

    # A correction changes which tiger was where, so both individuals'
    # home ranges change and an alert may now fire or stop firing. v0.1.1
    # recorded the correction faithfully and then left every downstream
    # number showing the pre-correction answer, with nothing marking it
    # stale. Recompute is arithmetic over data already on disk -- fast
    # enough that there is no reason to make the officer remember to do it.
    from edge.pipeline import postprocess  # noqa: PLC0415
    q = repo._one(repo.connect().execute(
        "SELECT crop_id FROM review_queue WHERE queue_id=?", (queue_id,)))
    if q:
        result["recomputed"] = postprocess.after_review_decision(
            q["crop_id"], payload.get("actor", "director"))
    return result''',
          "recompute occupancy + alerts after a review correction", check=ck)

    patch("edge/app.py", "_register_scale_routes",
          '''@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    applied = repo.migrate()
    if applied:
        repo.audit("schema.migrate", after={"applied": applied,
                                            "version": repo.schema_version()})''',
          '''@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    applied = repo.migrate()
    if applied:
        repo.audit("schema.migrate", after={"applied": applied,
                                            "version": repo.schema_version()})

    # A job marked `running` at boot cannot be running: this process just
    # started. Mark them interrupted so the run screen shows "stopped at
    # 30,142 of 50,000, resumable" instead of a progress bar that will
    # never move again.
    from edge import jobs  # noqa: PLC0415
    reaped = jobs.reap_stale()
    if reaped:
        repo.audit("startup.jobs_reaped", after={"count": reaped})

    # Bound PyTorch's thread pool. It defaults to every core, which on a
    # 4-core range-office laptop makes the machine unusable for anything
    # else for the hours a 50K run takes.
    threads = getattr(config.CONFIG.triage, "torch_threads", 0)
    if threads:
        try:
            import torch  # noqa: PLC0415
            torch.set_num_threads(int(threads))
        except ImportError:
            pass


from edge.routes_scale import register as _register_scale_routes  # noqa: E402
_register_scale_routes(app)''',
          "register 22 new routes + reap stale jobs at startup", check=ck)

    # ═════════════════════════════════════════════════════════════════════
    # 6. index.html + app.css — the map module and its styles
    # ═════════════════════════════════════════════════════════════════════
    patch("edge/ui/index.html", "map.js",
          '<script src="/ui/app.js"></script>',
          '<!-- The map renderer moved out of app.js. The version in there hardcoded\n'
          '     demo station IDs (PN-C-008/009/015 as "dead"/"new") and stretched\n'
          '     latitude and longitude independently, drawing every home range 87%\n'
          '     out of shape. Loaded first so window.PugMap exists when app.js runs. -->\n'
          '<script src="/ui/map.js"></script>\n'
          '<script src="/ui/app.js"></script>',
          "load the map module before app.js", check=ck)

    append("edge/ui/app.css", ".pugmap", MAP_CSS, "map styles", check=ck)



    patch("edge/db/repo.py", "SUM(status='vehicle')",
          '''        " SUM(status='person')      person,"''',
          '''        " SUM(status='person')      person,"
        " SUM(status='vehicle')     vehicle,"''',
          "count vehicle frames in the run summary", check=ck)

    # ═════════════════════════════════════════════════════════════════════
    # 7. app.js — the map renderer and the job-driven UI
    #
    # The old RENDER.map hardcoded three demo station IDs and stretched
    # latitude and longitude independently. Replaced wholesale rather than
    # patched: almost every line of it was one of the two problems.
    # ═════════════════════════════════════════════════════════════════════
    patch("edge/ui/app.js", "window.PugMap.render",
          OLD_MAP_BLOCK, NEW_MAP_BLOCK,
          "replace the map renderer + add background-job UI", check=ck)

    # The manual-identify table: one button per animal frame, unpaginated.
    # 945 on the seeded demo; ~4,000 scaled to a 50,000-frame import.
    patch("edge/ui/app.js", "runPipeline(nr.runId)",
          """  const t = await api(`/api/runs/${nr.runId}/triage/run`, { method: 'POST', body: {} });""",
          """  // v0.1.1 stopped here and handed the officer a table with one Identify
  // button per animal frame -- 945 of them on the seeded demo, roughly
  // 4,000 scaled to a 50,000-frame import. Triage is now the first step of
  // one background job that carries on through identification, occupancy
  // and alerts without further clicks.
  if (!nr.manualMode) { await runPipeline(nr.runId); return; }
  const t = await api(`/api/runs/${nr.runId}/triage/run`, { method: 'POST', body: {} });""",
          "triage button starts the whole pipeline as one job", check=ck)

    # ── report ───────────────────────────────────────────────────────────
    print(f"\n{'WOULD APPLY' if ck else 'APPLIED'} ({len(applied)}):")
    for a in applied:
        print(f"  + {a}")
    if skipped:
        print(f"\nALREADY APPLIED ({len(skipped)}):")
        for s in skipped:
            print(f"  = {s}")
    if missing:
        print(f"\nCOULD NOT APPLY ({len(missing)}) — apply these by hand:")
        for m in missing:
            print(f"  ! {m}")
    print()
    return 1 if missing else 0


OLD_MAP_BLOCK = 'RENDER.map = async () => {\n  if (!S.run) await RENDER.run();\n  const [stations, occ] = await Promise.all([\n    api(`/api/stations?reserve_id=${S.reserve.reserve_id}`),\n    api(`/api/runs/${S.run.run_id}/occupancy`),\n  ]);\n  const W = 900, H = 520, P = 34;\n  const lats = stations.map((s) => s.lat);\n  const lons = stations.map((s) => s.lon);\n  const [y0, y1] = [Math.min(...lats), Math.max(...lats)];\n  const [x0, x1] = [Math.min(...lons), Math.max(...lons)];\n  const X = (lon) => P + ((lon - x0) / ((x1 - x0) || 1)) * (W - 2 * P);\n  const Y = (lat) => H - P - ((lat - y0) / ((y1 - y0) || 1)) * (H - 2 * P);\n\n  const hulls = occ.filter((o) => o.hull_wkt).map((o) => {\n    const pts = o.hull_wkt.replace(/POLYGON\\(\\(|\\)\\)/g, \'\').split(\', \')\n      .map((p) => p.trim().split(\' \').map(Number))\n      .map(([lon, lat]) => `${X(lon).toFixed(1)},${Y(lat).toFixed(1)}`).join(\' \');\n    return `<polygon class="hull" points="${pts}"><title>${esc(o.ind_id)} —\n      ${o.area_km2} km²</title></polygon>`;\n  }).join(\'\');\n\n  const DEAD = new Set([\'PN-C-008\', \'PN-C-009\']);\n  const NEW = new Set([\'PN-C-015\']);\n  const pins = stations.map((s) => {\n    const cls = DEAD.has(s.station_id) ? \'dead\' : NEW.has(s.station_id) ? \'new\' : \'\';\n    return `<circle class="stn ${cls}" cx="${X(s.lon).toFixed(1)}"\n      cy="${Y(s.lat).toFixed(1)}" r="${cls ? 5 : 3.4}"><title>${esc(s.name)}\n      (${esc(s.zone)}) — ${esc(s.station_id)}</title></circle>`;\n  }).join(\'\');\n\n  $(\'#mapSvg\').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"\n    aria-label="Camera stations and tiger home ranges">\n    <rect class="zone-buffer" x="8" y="8" width="${W - 16}" height="${H - 16}" rx="4"/>\n    <rect class="zone-core" x="${P + 60}" y="${P + 40}" width="${W - 2 * P - 120}"\n          height="${H - 2 * P - 80}" rx="4"/>\n    <text x="${P + 70}" y="${P + 60}" font-size="11" fill="#6d7566"\n          letter-spacing="2" font-family="Bahnschrift, sans-serif">CORE</text>\n    <text x="20" y="26" font-size="11" fill="#6d7566" letter-spacing="2"\n          font-family="Bahnschrift, sans-serif">BUFFER</text>\n    ${hulls}${pins}</svg>`;\n\n  $(\'#occGeojson\').href = `/api/runs/${S.run.run_id}/occupancy/export.geojson`;\n  $(\'#occCsv\').href = `/api/runs/${S.run.run_id}/occupancy/export.csv`;\n\n  $(\'#occTable\').innerHTML = table(\n    [\'Tiger\', \'Stations\', \'Area km²\', \'Events\', \'Camera-days\', \'Note\'],\n    occ.map((o) => [\n      `<td class="n">${esc(o.ind_id)}</td>`,\n      `<td class="n">${o.station_set.length}</td>`,\n      `<td class="n">${o.area_km2 ?? \'—\'}</td>`,\n      `<td class="n">${nf(o.event_count)}</td>`,\n      `<td class="n">${o.effort_days}</td>`,\n      `<td style="color:var(--muted)">${esc(o.insufficient_reason || \'\')}</td>`]));\n};\n'

NEW_MAP_BLOCK = 'RENDER.map = async () => {\n  if (!S.run) await RENDER.run();\n  /* One request instead of two, and it carries what the old map faked:\n     which cameras stopped mid-cycle, which were installed this cycle, and\n     where each tiger\'s centroid was last cycle. All three used to be\n     either hardcoded (`const DEAD = new Set([\'PN-C-008\',\'PN-C-009\'])`) or\n     simply absent. */\n  const d = await api(`/api/runs/${S.run.run_id}/map`);\n  window.PugMap.render($(\'#mapSvg\'), { ...d, focus: S.mapFocus || null },\n    (ind) => { S.mapFocus = ind; RENDER.map(); });\n\n  $(\'#occGeojson\').href = `/api/runs/${S.run.run_id}/occupancy/export.geojson`;\n  $(\'#occCsv\').href = `/api/runs/${S.run.run_id}/occupancy/export.csv`;\n\n  const occ = d.occupancy;\n  if (!occ.length) {\n    /* Two empty states used to look identical and mean opposite things:\n       "nothing moved this cycle" and "this stage has never run against\n       your data". */\n    $(\'#occTable\').innerHTML = `<div class="card empty">\n      <strong>No home ranges yet</strong>\n      Nothing in this run has been identified to an individual, so there is\n      nothing to map. Run the pipeline on this run, or identify some frames\n      first.</div>`;\n    return;\n  }\n\n  $(\'#occTable\').innerHTML = table(\n    [\'Tiger\', \'Cameras\', \'Area km²\', \'Visits\', \'Camera-days\', \'Note\'],\n    occ.map((o) => [\n      `<td class="n"><button class="linkish" data-ind="${esc(o.ind_id)}">${esc(o.ind_id)}</button></td>`,\n      `<td class="n">${o.station_set.length}</td>`,\n      `<td class="n">${o.area_km2 ?? \'—\'}</td>`,\n      `<td class="n">${nf(o.event_count)}</td>`,\n      `<td class="n">${o.effort_days}</td>`,\n      `<td style="color:var(--muted)">${esc(o.insufficient_reason || \'\')}</td>`]));\n\n  $(\'#occTable\').querySelectorAll(\'[data-ind]\').forEach((b) =>\n    b.addEventListener(\'click\', () => {\n      S.mapFocus = S.mapFocus === b.dataset.ind ? null : b.dataset.ind;\n      RENDER.map();\n    }));\n};\n\n/* ── background jobs ──────────────────────────────────────────────────────\n   v0.1.1 ran a 50,000-frame import inside the HTTP request that asked for\n   it: no progress, no cancel, no resume, and a browser timeout partway\n   through left the run in a state nothing recorded. */\n\nasync function runPipeline(runId, actor = \'director\') {\n  const r = await api(`/api/runs/${runId}/pipeline`,\n    { method: \'POST\', body: { actor } });\n  S.job = r.job_id;\n  pollJob(r.job_id);\n  return r;\n}\n\nlet jobTimer = null;\nasync function pollJob(jobId) {\n  clearTimeout(jobTimer);\n  let j;\n  try { j = await api(`/api/jobs/${jobId}`); }\n  catch { return; }\n  drawJob(j);\n  if ([\'queued\', \'running\', \'paused\'].includes(j.state)) {\n    jobTimer = setTimeout(() => pollJob(jobId), 1500);\n  } else {\n    RENDER[S.view]?.();\n  }\n}\n\nfunction drawJob(j) {\n  const host = $(\'#jobPanel\');\n  if (!host) return;\n  host.hidden = false;\n  const pct = Math.round((j.progress || 0) * 100);\n  const eta = j.eta_seconds != null\n    ? `${Math.floor(j.eta_seconds / 60)} min ${Math.round(j.eta_seconds % 60)} s left`\n    : \'estimating…\';\n  const stage = j.detail?.stage ? ` · ${esc(j.detail.stage)}` : \'\';\n  host.className = `job ${esc(j.state)}`;\n  host.innerHTML = `\n    <div style="display:flex;justify-content:space-between;align-items:baseline">\n      <strong>${esc(j.kind)}${stage}</strong>\n      <span class="num">${esc(j.state)}</span>\n    </div>\n    <div class="bar"><i style="width:${pct}%"></i></div>\n    <div class="meta">\n      <span>${nf(j.done_count)} of ${nf(j.total)} · ${pct}%</span>\n      <span>${j.state === \'running\' ? eta : \'\'}</span>\n    </div>\n    ${j.error ? `<p class="note">${esc(j.error)}</p>` : \'\'}\n    ${j.failed_count ? `<div class="deadletters">${nf(j.failed_count)} frames could not\n      be read and were skipped. They are listed under this run — nothing was\n      silently dropped.</div>` : \'\'}\n    ${j.state === \'running\'\n      ? `<button class="btn ghost" id="jobCancel">Stop after this batch</button>`\n      : \'\'}\n    ${j.state === \'paused\'\n      ? `<button class="btn" id="jobResume">Resume from ${nf(j.done_count)}</button>`\n      : \'\'}`;\n  $(\'#jobCancel\')?.addEventListener(\'click\', async () => {\n    await api(`/api/jobs/${j.job_id}/cancel`, { method: \'POST\', body: { actor: \'director\' } });\n  });\n  $(\'#jobResume\')?.addEventListener(\'click\', async () => {\n    await api(`/api/jobs/${j.job_id}/resume`, { method: \'POST\', body: { actor: \'director\' } });\n    pollJob(j.job_id);\n  });\n}\n\n/* Pick up a job that was already running when the page was loaded — a\n   50,000-frame run outlives a browser tab, and closing the tab must not\n   look like the work stopped. */\n(async () => {\n  try {\n    const { active } = await api(\'/api/jobs\');\n    if (active?.length) pollJob(active[0].job_id);\n  } catch { /* server not up yet */ }\n})();\n'

MAP_CSS = """

/* ── map ─────────────────────────────────────────────────────────────────
   The previous map stretched latitude and longitude independently to fill a
   fixed 900x520 box. On the seeded reserve -- 9.8 km east-west by 10.0 km
   north-south, very nearly square -- that drew every home-range polygon at
   an aspect ratio of 1.84, i.e. 87% out of shape, directly beneath a table
   of areas that edge/pipeline/occupancy.py had projected into UTM to get
   right. edge/ui/map.js now uses one scale for both axes, which is what
   makes the scale bar below mean anything. */

.pugmap { width: 100%; height: auto; display: block;
          background: var(--surface-2); border-radius: 4px; }

.pugmap .zone        { stroke-width: 1; }
.pugmap .zone-core   { fill: #dfe6d6; stroke: var(--line); }
.pugmap .zone-buffer { fill: #e9ece2; stroke: var(--line-soft);
                       stroke-dasharray: 4 3; }

.pugmap .hull { fill: var(--pelage); fill-opacity: .11; stroke: var(--pelage);
                stroke-width: 1.2; cursor: pointer; transition: fill-opacity .12s; }
.pugmap .hull:hover        { fill-opacity: .22; }
.pugmap .hull.focus        { fill-opacity: .26; stroke-width: 2; }
.pugmap .hull.flagged      { stroke: var(--act); stroke-width: 1.8; }

/* Movement between cycles. The centroid_shift alert is the only alert whose
   evidence is a distance, and nothing drew it. */
.pugmap .shift line   { stroke: var(--act); stroke-width: 1.6; opacity: .8; }
.pugmap .shift .was   { fill: var(--muted); }
.pugmap #arrow path   { fill: var(--act); }

.pugmap .stn             { stroke: var(--surface); stroke-width: 1; }
.pugmap .stn-active      { fill: var(--ink-2); }
.pugmap .stn-idle        { fill: var(--sup); }
.pugmap .stn-offline     { fill: var(--act); }
.pugmap .stn-new         { fill: var(--info); }
.pugmap .stn.in-range    { stroke: var(--pelage); stroke-width: 2; }
.pugmap .stn:focus       { outline: 2px solid var(--pelage); outline-offset: 2px; }

.pugmap .scalebar line, .pugmap .north line, .pugmap .north path {
  stroke: var(--ink-2); stroke-width: 1.2; fill: none; }
.pugmap .north path { fill: var(--ink-2); }
.pugmap .scalebar text, .pugmap .north text {
  font-family: var(--f-display); font-size: 10px; letter-spacing: 1px;
  fill: var(--ink-2); }

.legend { margin-top: var(--s3); }
.legend .keys { display: flex; flex-wrap: wrap; gap: var(--s4);
                font-size: 12px; color: var(--muted); margin-bottom: var(--s3); }
.legend .keys span { display: flex; align-items: center; gap: 6px; }
.legend .k { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.legend .k.stn-active  { background: var(--ink-2); }
.legend .k.stn-idle    { background: var(--sup); }
.legend .k.stn-offline { background: var(--act); }
.legend .k.stn-new     { background: var(--info); }
.legend .k.k-hull  { background: var(--pelage); opacity: .35;
                     border: 1px solid var(--pelage); border-radius: 2px; }
.legend .k.k-shift { background: var(--act); border-radius: 1px; height: 2px; width: 14px; }

.legend .chips { display: flex; flex-wrap: wrap; gap: 6px; }
.legend .chip { font: inherit; font-size: 12px; cursor: pointer;
                background: var(--surface); border: 1px solid var(--line);
                border-radius: 3px; padding: 3px 8px; color: var(--ink-2);
                display: flex; gap: 6px; align-items: baseline; }
.legend .chip span  { color: var(--muted); font-family: var(--f-mono); font-size: 11px; }
.legend .chip:hover { border-color: var(--pelage); }
.legend .chip.on    { background: var(--pelage); color: #fff; border-color: var(--pelage); }
.legend .chip.on span { color: #f5e2d2; }
.legend .chip.act   { border-left: 3px solid var(--act); }
.legend .chip.clear { color: var(--muted); }

/* ── job progress ────────────────────────────────────────────────────────
   v0.1.1 ran a 50,000-frame import inside the HTTP request that asked for
   it, so there was nothing to show. */
.job { border: 1px solid var(--line); border-radius: 4px; padding: var(--s3);
       background: var(--surface); }
.job .bar   { height: 6px; background: var(--sunk); border-radius: 3px;
              overflow: hidden; margin: var(--s3) 0; }
.job .bar i { display: block; height: 100%; background: var(--pelage);
              transition: width .4s ease; }
.job .meta  { display: flex; justify-content: space-between; font-size: 12px;
              color: var(--muted); font-family: var(--f-mono); }
.job.failed .bar i    { background: var(--act); }
.job.paused .bar i    { background: var(--watch); }
.job .deadletters     { margin-top: var(--s3); font-size: 12px; color: var(--act); }
"""


if __name__ == "__main__":
    sys.exit(main())
