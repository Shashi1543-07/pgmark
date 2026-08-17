"""Live route tests.

These boot the real application and drive the real HTTP surface against the
real database. They exist because a route that is defined but never wired,
or a query that is written but never correct, passes every static check and
fails in front of a jury.

Rule this file enforces: assert the EFFECT, not the existence.

    python -m tests.live.test_routes
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# edge.config reads PUGMARK_SYNC_SECRET at import time, so this has to be
# set before the first `from edge...` import, not just before it's used.
os.environ.setdefault("PUGMARK_SYNC_SECRET", "test-only-secret-do-not-reuse")

from fastapi.testclient import TestClient     # noqa: E402

from edge import config, effort                 # noqa: E402
from edge.app import app                       # noqa: E402
from edge.db import repo                       # noqa: E402
from edge.db import repo_ext                   # noqa: E402
from edge.pipeline import occupancy             # noqa: E402
from edge.sync import bundle as bundle_sync      # noqa: E402
from tests.fixtures.ingest_corpus import build as build_ingest_corpus   # noqa: E402
from tests.fixtures.triage_corpus import build as build_triage_corpus   # noqa: E402

PASS, FAIL = [], []

# The admin credential as it existed BEFORE this suite hijacked it, so the
# operator is not locked out of their own node by running the very command
# CLAUDE.md tells them to run. Populated in _run(), consumed by
# _restore_admin_password() in main()'s finally-block.
_ORIGINAL_ADMIN: dict = {}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def _restore_admin_password() -> None:
    """Put the operator's own admin password back after the run.

    Best-effort by design: a failure here must never turn a passing suite
    into a non-zero exit, but it must also never fail silently -- being
    quietly locked out is the exact failure this exists to prevent, so it
    says so on stdout and names the fallback password.
    """
    if not _ORIGINAL_ADMIN.get("pwd_hash"):
        return
    try:
        conn = repo.connect()
        conn.execute(
            "UPDATE users SET pwd_hash=?, must_change_password=?,"
            " failed_login_attempts=0, locked_until=NULL WHERE username='admin'",
            (_ORIGINAL_ADMIN["pwd_hash"],
             _ORIGINAL_ADMIN.get("must_change_password", 0)))
        conn.commit()
        print("  note  admin password restored to what it was before this run")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  WARN  could not restore the original admin password ({exc}).\n"
              f"        The admin password is currently 'TestAdminPass123!' -- "
              f"log in with that and change it.")



def _rows_compat(cursor):
    """sqlite3.Row -> dict, without reaching into repo's private helpers."""
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def main() -> int:
    _check_fresh_import()
    from tools import seed_demo
    seed_demo.main(reset=True)
    try:
        with TestClient(app) as c:
            return _run(c)
    finally:
        # finally, not a trailing call: an assertion blowing up mid-suite is
        # exactly when a half-finished run would otherwise leave the machine
        # holding a test password.
        _restore_admin_password()


def _check_fresh_import() -> None:
    """The most basic claim this suite makes -- that the app starts at
    all -- can't be proven from inside a process that already has every
    dependency imported. A route using Form()/File() without python-
    multipart installed makes FastAPI raise at import time (during route
    registration, not the first request), and every check below runs in
    a process where that import already succeeded once, so none of them
    would ever see this class of failure."""
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run([sys.executable, "-c", "import edge.app"],
                       capture_output=True, text=True, cwd=str(root))
    check("the app imports cleanly in a fresh interpreter, not just this test process",
          r.returncode == 0, r.stderr.strip()[-300:] if r.returncode else "")


def _run(c: TestClient) -> int:
    # ── meta ────────────────────────────────────────────────────────────
    h = c.get("/api/health").json()
    check("health responds", h["ok"])
    check("schema migrated", h["schema_version"] >= 1, f"v{h['schema_version']}")
    check("schema reaches migration 8 (species/side evidence)",
          h["schema_version"] >= 8, f"v{h['schema_version']}")

    # ── authentication & offline security ────────────────────────────────
    from edge import auth
    # Verify unauthenticated request is blocked
    check("unauthenticated request returns 401", c.get("/api/reserves").status_code == 401)
    check("unauthenticated request returns 401 without cookie", "pugmark_session" not in c.cookies)

    # Seed admin if needed and setup known test passwords.
    #
    # This overwrites the real admin password with a known test value so the
    # suite can authenticate. Harmless against a throwaway database, but this
    # suite is ALSO the documented verification gate that CLAUDE.md tells
    # every session to run -- and when it is pointed at a live node it locks
    # the actual operator out of their own machine with no message saying
    # why. That happened. So: remember what was there, and hand it back in a
    # finally-block at the end of the run (see _restore_admin_password).
    repo.ensure_admin()
    _prior = repo._one(repo.connect().execute(
        "SELECT pwd_hash, must_change_password FROM users WHERE username='admin'"))
    if _prior:
        _ORIGINAL_ADMIN.update(dict(_prior))
    admin_pw = "TestAdminPass123!"
    repo.connect().execute("UPDATE users SET pwd_hash=?, must_change_password=0 WHERE username='admin'",
                           (auth.hash_secret(admin_pw),))
    repo.connect().commit()

    # Test invalid login fails and doesn't reveal user existence
    bad_login = c.post("/api/auth/login", json={"username": "admin", "password": "WrongPassword123!"})
    check("invalid password returns 401", bad_login.status_code == 401)
    check("generic failure message", bad_login.json()["detail"] == "Invalid username or password.")
    check("login returns attempts remaining", "attempts_remaining" in bad_login.json())

    # Test user creation (idempotent — skip if user already exists from a prior run)
    field_pw = "TestFieldPass123!"
    field_rec = "TEST-RECO-VERY-CODE-1111-2222"
    try:
        repo.create_user("field_user", "Test Field", "field", auth.hash_secret(field_pw),
                         auth.hash_secret(auth.normalise_recovery_code(field_rec)), must_change=False)
    except Exception:
        pass  # user already seeded from a previous run — reset locked_until if needed
    conn = repo.connect()
    conn.execute("UPDATE users SET locked_until=NULL, failed_login_attempts=0 WHERE username='field_user'")
    conn.commit()


    # Test lockout math (5 failed attempts locks user)
    for _ in range(5):
        c.post("/api/auth/login", json={"username": "field_user", "password": "BadPassword123!"})
    locked_login = c.post("/api/auth/login", json={"username": "field_user", "password": field_pw})
    check("account is locked after 5 failed attempts", locked_login.status_code == 401)
    check("lockout returns locked indicator", locked_login.json().get("locked") is True)
    user_row = repo.user("field_user")
    check("locked_until is set in DB", user_row["locked_until"] is not None)

    # Test emergency recovery / forgot password
    new_field_pw = "NewFieldPass123!"
    forgot_res = c.post("/api/auth/forgot-password", json={
        "username": "field_user",
        "recovery_code": field_rec,
        "new_password": new_field_pw,
    })
    check("forgot password succeeds with valid recovery code", forgot_res.status_code == 200)
    check("new recovery code returned and rotated", "recovery_code" in forgot_res.json() and forgot_res.json()["recovery_code"] != field_rec)
    user_after_reset = repo.user("field_user")
    check("failed attempts reset to 0 after recovery", user_after_reset["failed_login_attempts"] == 0)
    check("lockout cleared after recovery", user_after_reset["locked_until"] is None)

    # Test field login with new password
    c_field = TestClient(app)
    field_login = c_field.post("/api/auth/login", json={"username": "field_user", "password": new_field_pw})
    check("field user can login after recovery", field_login.status_code == 200)

    # RBAC. This block previously asserted that a field user COULD reach
    # /api/dev/seed (expecting 400-on-bad-payload rather than 403), which
    # encoded the bug instead of catching it: dev_seed erases every reserve,
    # run, tiger and alert on the node, and it was reachable by the lowest
    # -privilege account. The assertion is inverted deliberately -- the
    # secure behaviour is that the role gate rejects it outright.
    field_seed_probe = c_field.post("/api/dev/seed", json={"which": "not-a-real-option"})
    check("field user is BLOCKED from the destructive dev-seed route",
          field_seed_probe.status_code == 403,
          f"{field_seed_probe.status_code} {field_seed_probe.text[:120]}")
    check("field user cannot build a sync bundle (data leaves the machine)",
          c_field.get("/api/sync/bundle").status_code == 403)
    check("field user cannot back up the whole database",
          c_field.post("/api/ops/backup").status_code == 403)
    check("field user can read audit log", c_field.get("/api/audit").status_code == 200)
    check("field user cannot manage users", c_field.get("/api/auth/users").status_code == 403)
    # The privacy leak this audit found: a person frame is blurred, filed to
    # persons_restricted and kept out of the tiger pipeline, but the
    # image-file route served the untouched original to any logged-in
    # account. Blueprint section 10 gives `field` no access to person images
    # at all.
    _person = repo._one(repo.connect().execute(
        "SELECT image_id FROM images WHERE status='person' LIMIT 1"))
    if _person:
        person_probe = c_field.get(f"/api/images/{_person['image_id']}/file")
        check("original person frames are never served, even to a logged-in user",
              person_probe.status_code == 403,
              f"{person_probe.status_code} {person_probe.text[:120]}")

    # Test analyst user for coordinate generalisation
    analyst_pw = "TestAnalystPass123!"
    analyst_rec = "TEST-RECO-VERY-CODE-3333-4444"
    repo.create_user("analyst_user", "Test Analyst", "analyst", auth.hash_secret(analyst_pw),
                     auth.hash_secret(auth.normalise_recovery_code(analyst_rec)), must_change=False)
    c_analyst = TestClient(app)
    c_analyst.post("/api/auth/login", json={"username": "analyst_user", "password": analyst_pw})

    # Log in main client as admin
    admin_login = c.post("/api/auth/login", json={"username": "admin", "password": admin_pw})
    check("admin login succeeds", admin_login.status_code == 200)
    check("session cookie set", "pugmark_session" in c.cookies)
    me = c.get("/api/auth/me").json()
    check("whoami returns admin user", me["username"] == "admin" and me["role"] == "admin")

    reserves = c.get("/api/reserves").json()
    check("reserve seeded", len(reserves) == 1)
    rid = reserves[0]["reserve_id"]
    check("reserve carries a UTM zone", reserves[0]["utm_epsg"] == 32644,
          "area in km² is wrong without projecting first")

    # ── runs ────────────────────────────────────────────────────────────
    runs = c.get(f"/api/runs?reserve_id={rid}").json()
    check("three cycles present", len(runs) == 3)
    run_id = runs[0]["run_id"]
    run = c.get(f"/api/runs/{run_id}").json()
    counts = run["counts"]
    check("frames counted", counts["total"] > 2000, f"{counts['total']}")
    check("blank ratio is realistic",
          counts["quarantined"] > counts["subject"] * 5,
          f"{counts['quarantined']} blank vs {counts['subject']} with a subject")
    check("timestamp fallbacks exercised",
          len(run["timestamp_sources"]) >= 3,
          str([r["src"] for r in run["timestamp_sources"]]))
    check("ingest flags surfaced", len(run["flags"]) >= 2)

    # ── ingest: Stage 1, real files through the real routes ─────────────
    # Everything above this point reads the seeded (fabricated) demo data.
    # This block is the one part of the suite that drives actual bytes on
    # disk through the actual ingest pipeline -- no seed script involved.
    target = c.get(f"/api/reserves/{rid}/stations").json()[0]
    fuzzed_folder = target["folder_hint"][:-1]     # drop one char: tests fuzzy match, not exact
    first_active = repo.station_first_active(target["station_id"])
    activity_start = datetime.fromisoformat(first_active) if first_active else datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    tmp_root = Path(tempfile.mkdtemp(prefix="pugmark_ingest_"))
    ingest_run_id = None
    try:
        build_ingest_corpus(tmp_root, fuzzed_folder, activity_start)

        pf = c.post("/api/runs", json={"reserve_id": rid, "root_path": str(tmp_root),
                                       "cycle_label": "ingest-test"}).json()
        ingest_run_id = pf["run_id"]
        check("every file on disk is accounted for, corrupt or not",
              pf["files_found"] == 15, f"{pf['files_found']}")
        check("corrupt files counted, run did not crash",
              pf["corrupt_count"] == 3, f"{pf['corrupt_count']}")
        check("duplicate content detected once, not re-inserted as a second row",
              pf["duplicate_count"] == 1, f"{pf['duplicate_count']}")
        check("unmatched folders are reported, never silently guessed",
              set(pf["unmatched_folders"]) == {"MIXED_BODIES", "UNSORTED_CARD"},
              str(pf["unmatched_folders"]))
        check("a folder within edit-distance of a real station's hint still matches",
              fuzzed_folder not in pf["unmatched_folders"])
        check("mixed camera bodies flagged at the folder level, not split",
              "MIXED_BODIES" in pf["mixed_camera_folders"])
        check("preflight discloses the assumption behind its time estimate",
              "estimated_seconds_per_image_assumed" in pf)

        # ── folder browser: the new-run screen's click-through picker ───
        browse_root = c.get("/api/fs/browse", params={"path": str(tmp_root)}).json()
        check("browsing a real folder lists its real subfolders",
              set(e["name"] for e in browse_root["entries"])
              >= {"UNSORTED_CARD", "MIXED_BODIES"},
              str(browse_root["entries"]))
        check("browsing reports the parent so the picker can step back up",
              browse_root["parent"] == str(tmp_root.parent))
        leaf = c.get("/api/fs/browse", params={"path": str(tmp_root / "UNSORTED_CARD")}).json()
        check("a folder with only files, no subfolders, browses to an empty list",
              leaf["entries"] == [])
        check("browsing a file, not a directory, is refused",
              c.get("/api/fs/browse",
                    params={"path": str(tmp_root / "UNSORTED_CARD" / "notes.txt")})
               .status_code == 400)
        no_path = c.get("/api/fs/browse").json()
        check("browsing with no path starts from somewhere real (drives or root), not empty",
              len(no_path["entries"]) > 0, str(no_path["entries"]))

        # The real dialog blocks on a human, so only its plumbing is
        # testable headlessly here -- the human-facing pop-up itself is
        # verified by hand, not by this suite.
        with patch("tkinter.filedialog.askdirectory", return_value=str(tmp_root)):
            native = c.post("/api/fs/native-browse").json()
        check("the native folder dialog route is wired to a real OS picker call",
              native == {"available": True, "path": str(tmp_root)}, str(native))
        with patch("tkinter.filedialog.askdirectory", return_value=""):
            cancelled = c.post("/api/fs/native-browse").json()
        check("cancelling the native dialog reports no path chosen, not an error",
              cancelled == {"available": True, "path": None}, str(cancelled))

        bad_confirm = c.post(f"/api/runs/{ingest_run_id}/confirm", json={})
        check("confirm refuses while folders remain unresolved",
              bad_confirm.status_code == 400)

        conf = c.post(f"/api/runs/{ingest_run_id}/confirm",
                       json={"skip_folders": ["MIXED_BODIES", "UNSORTED_CARD"]}).json()
        check("confirm proceeds once every folder is assigned or skipped",
              conf["stage"] == "confirmed")
        check("bursts became distinct events, not one capture per frame",
              conf["events"] >= 4, f"{conf['events']} events from 9 frames in the matched folder")

        ingested = repo.images_for_run(ingest_run_id)
        sources = {i["captured_at_source"] for i in ingested if i["station_id"]}
        check("exif, filename and inferred tiers all actually fired",
              sources == {"exif", "filename", "inferred"}, str(sources))
        check("OCR tier is honestly absent, not faked",
              all("ocr" != i["captured_at_source"] for i in ingested))

        drifted = [i for i in ingested if i["drift_applied_s"]]
        check("a reset camera clock is corrected, not just discarded", len(drifted) == 1,
              f"{len(drifted)}")
        if drifted:
            corrected = datetime.fromisoformat(drifted[0]["captured_at"])
            check("the correction actually lands near the station's known deployment date",
                  abs((corrected - activity_start).total_seconds()) < 86400,
                  f"corrected to {corrected}, anchor was {activity_start}")
            check("the correction is flagged, not silent",
                  "camera_clock_reset_corrected" in json.loads(drifted[0]["flags"]))

        skipped = [i for i in ingested if i["station_id"] is None]
        check("skipped folders' files stay station-less rather than being guessed at",
              len(skipped) == 6, f"{len(skipped)}")
        check("nothing here fabricates a finished pipeline: everything sits at pending",
              all(i["status"].lower() in ("pending", "corrupt") for i in ingested))

        matched = [i for i in ingested if i["station_id"] == target["station_id"]]
        check("the night heuristic runs for real against actual pixels",
              all(i["is_night"] == 1 for i in matched) and len(matched) > 0,
              str([i["is_night"] for i in matched]))

        # a run with BOTH a matched folder and skipped, station-less folders
        # in it -- triage must not lump unrelated skipped folders together
        # under a shared "no station" background (found via manual UI testing)
        pending_skipped = [i for i in skipped if i["status"].lower() == "pending"]
        tri = c.post(f"/api/runs/{ingest_run_id}/triage/run").json()
        check("triage leaves station-less frames alone instead of comparing "
              "unrelated skipped folders against each other",
              tri["skipped_no_station"] == len(pending_skipped)
              and tri["quarantined"] + tri["awaiting_detector"] + tri["unreadable"]
              == len(matched), f"{tri} vs {len(matched)} matched, "
              f"{len(pending_skipped)} pending-and-skipped")
        untouched = repo.images_for_run(ingest_run_id)
        check("station-less frames are left exactly as they were, not silently classified",
              all(i["status"].lower() in ("pending", "corrupt")
                  for i in untouched if i["station_id"] is None))

        # ── sync: a real bundle, applied to a genuinely separate database ─
        node_b_dir = Path(tempfile.mkdtemp(prefix="pugmark_nodeb_"))
        conn_b = repo.connect(node_b_dir / "node_b.db")
        repo.migrate(node_b_dir / "node_b.db")
        # Node B needs the same reference data as a precondition: reserves
        # and stations aren't syncable tables here (see edge/sync/bundle.py)
        # -- a real deployment bootstraps every node against identical
        # reserve/station reference data, then syncs the dynamic rest.
        repo.insert("reserves", repo.reserve(rid), conn_b)
        station_cols = ["station_id", "reserve_id", "name", "lat", "lon", "zone",
                        "village_dist_km", "grid_cell", "folder_hint"]
        repo.insert("stations", {k: target[k] for k in station_cols}, conn_b)

        no_secret_err = None
        try:
            bundle_sync.build_bundle(rid, 0, "")
        except ValueError as e:
            no_secret_err = str(e)
        check("building a bundle without a configured secret refuses outright",
              bool(no_secret_err), no_secret_err or "")

        bundle_resp = c.get(f"/api/sync/bundle?reserve_id={rid}&since_lamport=0")
        check("bundle download is a real signed file",
              bundle_resp.status_code == 200
              and "attachment" in bundle_resp.headers.get("content-disposition", ""))
        bundle = bundle_resp.json()
        check("bundle carries this run's images, each with a real Lamport stamp",
              len(bundle["rows"]["images"]) == len(matched) + len(skipped)
              and all(r["lamport"] for r in bundle["rows"]["images"]),
              f"{len(bundle['rows']['images'])} vs {len(matched) + len(skipped)}")
        check("bundle carries the run row itself", len(bundle["rows"]["runs"]) == 1)

        tampered = json.loads(json.dumps(bundle))
        tampered["rows"]["images"][0]["status"] = "quarantined"
        check("a tampered bundle fails signature verification",
              not bundle_sync.verify_bundle(tampered, config.SYNC_SECRET))
        tamper_refused = False
        try:
            bundle_sync.apply_bundle(tampered, config.SYNC_SECRET, conn_b)
        except ValueError:
            tamper_refused = True
        check("applying a tampered bundle is refused, not silently accepted", tamper_refused)

        stats1 = bundle_sync.apply_bundle(bundle, config.SYNC_SECRET, conn_b)
        check("first apply inserts everything as new",
              stats1["inserted"] == len(bundle["rows"]["runs"]) + len(bundle["rows"]["images"])
              and stats1["unchanged"] == 0, str(stats1))

        node_b_images = repo.images_for_run(ingest_run_id, conn_b)
        check("node B has the real image rows, not just a count that happens to match",
              {i["image_id"] for i in node_b_images} == {i["image_id"] for i in matched + skipped})

        stats2 = bundle_sync.apply_bundle(bundle, config.SYNC_SECRET, conn_b)
        check("re-applying the same bundle is a true no-op, same as a bundle arriving twice",
              stats2["inserted"] == 0
              and stats2["unchanged"] == len(bundle["rows"]["runs"]) + len(bundle["rows"]["images"]),
              str(stats2))

        # a genuine conflict: the same image independently edited on each
        # side, resolved by whichever side has the higher Lamport value
        conflict_id = matched[0]["image_id"]
        for _ in range(5):
            repo.next_lamport(conn_b)   # advance B's clock well past A's
        row_b = dict(conn_b.execute(
            "SELECT * FROM images WHERE image_id=?", (conflict_id,)).fetchone())
        row_b["status"] = "person"
        row_b["lamport"] = repo.next_lamport(conn_b)
        row_b["origin_node"] = repo.node_id(conn_b)
        row_b["synced_at"] = None
        row_b["row_hash"] = repo.compute_row_hash(row_b)
        set_cols = [c for c in row_b if c != "image_id"]
        conn_b.execute(
            f"UPDATE images SET {', '.join(f'{c}=?' for c in set_cols)} WHERE image_id=?",
            (*[row_b[c] for c in set_cols], conflict_id))
        conn_b.commit()

        return_bundle = bundle_sync.build_bundle(rid, bundle["up_to_lamport"],
                                                  config.SYNC_SECRET, conn_b)
        check("the reverse bundle carries the conflicting edit",
              any(r["image_id"] == conflict_id for r in return_bundle["rows"]["images"]))

        pre_status = next(i["status"] for i in repo.images_for_run(ingest_run_id)
                          if i["image_id"] == conflict_id)
        applied_back = bundle_sync.apply_bundle(return_bundle, config.SYNC_SECRET)
        check("applying B's bundle back to A resolves the conflict, not a crash or a silent skip",
              applied_back["conflict_resolved"] >= 1, str(applied_back))
        post_status = next(i["status"] for i in repo.images_for_run(ingest_run_id)
                           if i["image_id"] == conflict_id)
        check("the higher-Lamport edit wins the conflict, not whichever merge ran first",
              pre_status != post_status and post_status == "person",
              f"before={pre_status} after={post_status}")

        shutil.rmtree(node_b_dir, ignore_errors=True)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if ingest_run_id:
            repo.delete_run_cascade(ingest_run_id)
            shutil.rmtree(config.QUARANTINE_DIR / ingest_run_id, ignore_errors=True)

    # ── triage Stage A: the motion prefilter, against real pixels ───────
    tri_root = Path(tempfile.mkdtemp(prefix="pugmark_triage_"))
    try:
        manifest = build_triage_corpus(tri_root, fuzzed_folder, activity_start)
        pf2 = c.post("/api/runs", json={"reserve_id": rid, "root_path": str(tri_root),
                                        "cycle_label": "triage-test"}).json()
        tri_run_id = pf2["run_id"]
        check("triage fixture ingests cleanly, one matched folder",
              pf2["files_found"] == manifest["frames"] and not pf2["unmatched_folders"],
              str(pf2["unmatched_folders"]))

        c.post(f"/api/runs/{tri_run_id}/confirm", json={})
        result = c.post(f"/api/runs/{tri_run_id}/triage/run").json()

        n_subjects = len(manifest["subject_indices"])
        frame_index = lambda p: int(Path(p).stem.split("_")[-1])   # noqa: E731
        triaged_now = repo.images_for_run(tri_run_id)
        quarantined_indices = {frame_index(i["orig_path"]) for i in triaged_now
                               if i["status"] == "quarantined"}
        check("subject frames are never quarantined by Stage A -- the injected shapes "
              "are not real animals, so Stage B may still call them blank, but Stage A's "
              "own decision must never discard a frame it saw motion in",
              quarantined_indices.isdisjoint(manifest["subject_indices"]),
              f"quarantined={sorted(quarantined_indices)} subjects={manifest['subject_indices']}")
        check("frames matching an established background ARE quarantined",
              result["quarantined"] >= len(manifest["blank_indices"]) - 1,
              f"quarantined={result['quarantined']}, blank frames planted="
              f"{len(manifest['blank_indices'])}")
        check("every frame lands in exactly one outcome bucket -- Stage A's quarantine, "
              "genuinely unreadable, still awaiting the detector, or one of Stage B's own "
              "terminal calls (subject/person/vehicle/blank)",
              result["quarantined"] + result["awaiting_detector"] + result["unreadable"]
              + result["subject"] + result["person"] + result["vehicle"]
              + result["blank_by_detector"] == manifest["frames"])

        # the configured threshold must be the threshold actually in force --
        # AUDIT_AND_REVISED_PLAN.md P0-3: a prior version gated on a second,
        # derived confidence value that was 10x stricter, so the number on
        # the Ops screen was never the number deciding anything
        from edge.pipeline.triage import cell_score
        thr = config.CONFIG.triage.stage_a_blank_threshold
        bg_probe = np.full((16, 16), 100.0)
        # A small margin either side of the boundary, not the exact value --
        # thr*255 then /255 does not round-trip exactly in floating point,
        # and a margin also proves more than an exact-boundary test would:
        # under the old bug, "just below" would still have failed the
        # hidden 10x-stricter gate.
        just_below = bg_probe.copy()
        just_below[0, 0] = 100.0 + (thr - 0.002) * 255
        just_above = bg_probe.copy()
        just_above[0, 0] = 100.0 + (thr + 0.01) * 255
        check("the configured blank threshold is the real decision boundary, "
              "not a value rescaled by a second, hidden gate",
              cell_score(just_below, bg_probe) <= thr
              and cell_score(just_above, bg_probe) > thr,
              f"below={cell_score(just_below, bg_probe):.4f} "
              f"above={cell_score(just_above, bg_probe):.4f} threshold={thr}")

        triaged = repo.images_for_run(tri_run_id)
        q_rows = [i for i in triaged if i["status"] == "quarantined"]
        p_rows = [i for i in triaged if i["status"] == "pending"]
        check("quarantine and pending counts match the route's own report",
              len(q_rows) == result["quarantined"] and len(p_rows) == result["awaiting_detector"])

        manifest_path = config.QUARANTINE_DIR / tri_run_id / "manifest.json"
        check("a real manifest.json is written, not just a database row",
              manifest_path.exists())
        on_disk = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
        check("manifest entries match the quarantined rows one for one",
              len(on_disk) == len(q_rows), f"{len(on_disk)} vs {len(q_rows)}")
        check("quarantined frames are physically moved, not just flagged",
              all(Path(m["quarantine_path"]).exists() and not Path(m["orig_path"]).exists()
                  for m in on_disk))

        subject_rows = [i for i in triaged
                        if frame_index(i["orig_path"]) in manifest["subject_indices"]]
        # A drawn ellipse is not a real animal, so the real detector may
        # honestly call these blank too (checked directly above) -- the
        # invariant that matters is that a frame is never lost, not that
        # Stage B leaves it exactly where Stage A did.
        stage_b_manifest_path = config.QUARANTINE_DIR / tri_run_id / "manifest_stage_b.json"
        stage_b_moved = {m["orig_path"] for m in (
            json.loads(stage_b_manifest_path.read_text()) if stage_b_manifest_path.exists()
            else [])}
        check("frames with an actual subject are never lost -- Stage A never quarantines "
              "them (checked above), and if Stage B's real detector does not recognise the "
              "injected shape as an animal either, the frame is honestly quarantined by "
              "Stage B instead of silently vanishing",
              len(subject_rows) == n_subjects
              and all(Path(i["orig_path"]).exists() or i["orig_path"] in stage_b_moved
                      for i in subject_rows))

        # ── the bulk-run -> Identify bridge: no re-upload needed ─────────
        # A bulk scan never runs Stage 3 on its own (see identify_upload.py's
        # module docstring) -- these two routes let the UI run it directly
        # against a frame the run already ingested, using the file already
        # on this machine, rather than making someone find and re-upload it.
        from PIL import Image as _BridgeImage, ImageDraw as _BridgeDraw
        bridge_path = tri_root / "_bridge_probe.jpg"
        bridge_img = _BridgeImage.new("RGB", (96, 72), (90, 110, 90))
        _BridgeDraw.Draw(bridge_img).ellipse((20, 15, 76, 57), fill=(180, 120, 40))
        bridge_img.save(bridge_path)
        bridge_image_id = repo.new_id("img_")
        repo.insert_many("images", [dict(
            image_id=bridge_image_id, reserve_id=rid, run_id=tri_run_id,
            station_id=triaged[0]["station_id"], orig_path=str(bridge_path),
            sha256=bridge_image_id, dhash=None, captured_at=repo.now(), captured_at_raw=None,
            captured_at_source="inferred", drift_applied_s=0, is_night=0, width=None,
            height=None, bytes=bridge_path.stat().st_size, status="subject",
            triage_stage="B", flags="[]")])

        listed = c.get(f"/api/runs/{tri_run_id}/images", params={"status": "subject"}).json()
        check("the bulk-run image list surfaces a real subject frame for the bridge",
              any(im["image_id"] == bridge_image_id for im in listed), str(listed))

        bridge_result = c.post(f"/api/runs/{tri_run_id}/images/{bridge_image_id}/identify",
                                json={"actor": "tester"})
        from edge.pipeline import detector as bridge_detector, identify as bridge_identify
        bridge_models_present = (bridge_detector.CHECKPOINT_PATH.exists()
                                 and bridge_detector.CONFIG_PATH.exists()
                                 and bridge_identify.WEIGHTS_PATH.exists())
        if bridge_models_present:
            check("the bridge route runs Stage 3 against the file already on disk and returns "
                  "a real decision, not a 500 -- no re-upload dialog required",
                  bridge_result.status_code == 200 and "decision" in bridge_result.json(),
                  f"{bridge_result.status_code} {bridge_result.text[:200]}")
        else:
            check("the bridge route refuses a missing local model bundle rather than trying "
                  "to download one", bridge_result.status_code == 400
                  and "edge/models" in bridge_result.text,
                  f"{bridge_result.status_code} {bridge_result.text[:200]}")

        missing = c.post(f"/api/runs/{tri_run_id}/images/img_does_not_exist/identify",
                          json={"actor": "tester"})
        check("the bridge route 404s for an image that isn't part of this run",
              missing.status_code == 404)

        # ── Stage B: MegaDetector V6, against real photographs ──────────
        # Guarded on the weights and sample photos actually being present
        # -- the offline model bundle plus optional ATRW / CCT20 evaluation
        # data (docs/DATA.md), and this suite
        # must still pass on a fresh clone that has not run them.
        from edge.pipeline import detector as detector_pipeline
        atrw_sample = Path("data/raw/atrw/train/000001.jpg")
        cct_root = Path("data/raw/cct20/eccv_18_all_images_sm")
        cct_blank_candidates = (list(cct_root.glob("*.jpg"))[:1] if cct_root.exists() else [])

        if (detector_pipeline.CHECKPOINT_PATH.exists() and detector_pipeline.CONFIG_PATH.exists()
                and atrw_sample.exists()):
            det = detector_pipeline.get_detector()
            animal_result = det.detect(str(atrw_sample), conf_threshold=0.20)
            check("a frame containing an animal produces a real detection box",
                  len(animal_result) > 0 and animal_result[0].label == "animal"
                  and animal_result[0].conf > 0.5,
                  str(animal_result))

            if cct_blank_candidates:
                import json as _json
                anno = _json.loads(Path(
                    "data/raw/cct20/eccv_18_annotation_files/trans_test_annotations.json"
                ).read_text())
                cats_by_img = {}
                for a in anno["annotations"]:
                    cats_by_img.setdefault(a["image_id"], set()).add(a["category_id"])
                blank_img = next((im for im in anno["images"] if cats_by_img.get(im["id"]) == {30}
                                   and (cct_root / im["file_name"]).exists()), None)
                if blank_img:
                    blank_result = det.detect(str(cct_root / blank_img["file_name"]),
                                               conf_threshold=0.20)
                    check("a genuinely blank frame produces no detection box",
                          len(blank_result) == 0, str(blank_result))

            # Person routing: tested directly against _restrict_person(),
            # not by finding a real photo with a person in it (this suite
            # has no licensed-for-redistribution photo of a person to
            # commit) -- this isolates and proves the ROUTING code
            # (blur, persons_restricted row, status='person'), which is
            # what Task 3 actually needs verified; MegaDetector's own
            # person-detection recall is the upstream model's established,
            # general capability, not something re-proven here.
            from edge.pipeline import triage as _triage_pipeline
            from edge.pipeline.detector import Detection as _Detection
            from PIL import Image as _Image, ImageDraw as _ImageDraw
            # A genuinely separate image row, not one of the 10 corpus
            # frames -- Stage B may have honestly quarantined every one of
            # them by now (checked above), and reusing any of their
            # image_ids here would permanently pull that frame out of
            # 'pending' (_restrict_person sets status='person'), corrupting
            # the re-triage idempotency check further down. Some visual
            # detail (not a flat colour) so the blur actually changes bytes.
            person_probe_path = tri_root / "_person_probe.jpg"
            probe_img = _Image.new("RGB", (64, 48), (80, 80, 80))
            _ImageDraw.Draw(probe_img).ellipse((10, 8, 50, 38), fill=(200, 40, 40))
            probe_img.save(person_probe_path)
            person_probe_id = repo.new_id("img_")
            repo.insert_many("images", [dict(
                image_id=person_probe_id, reserve_id=rid, run_id=tri_run_id,
                station_id=triaged[0]["station_id"], orig_path=str(person_probe_path),
                sha256=person_probe_id, dhash=None, captured_at=repo.now(),
                captured_at_raw=None, captured_at_source="inferred", drift_applied_s=0,
                is_night=0, width=None, height=None, bytes=person_probe_path.stat().st_size,
                status="subject", triage_stage="B", flags="[]")])
            person_row = {"image_id": person_probe_id, "orig_path": str(person_probe_path)}
            before_pixels = Path(person_row["orig_path"]).read_bytes()
            fake_person = _Detection(label=detector_pipeline.PERSON_LABEL, conf=0.93,
                                      x=0.1, y=0.1, w=0.3, h=0.3)
            _triage_pipeline._restrict_person(person_row, fake_person)

            restricted_rows = repo.persons_restricted_for_image(person_row["image_id"])
            check("a frame containing a person lands in persons_restricted, not the "
                  "tiger pipeline", len(restricted_rows) == 1, str(restricted_rows))
            after_status = repo.images_for_run(tri_run_id)
            person_status = next(i["status"] for i in after_status
                                  if i["image_id"] == person_row["image_id"])
            check("a person frame's image status becomes 'person', not 'subject'",
                  person_status == "person", person_status)
            blurred_path = Path(restricted_rows[0]["blurred_path"])
            check("the blurred copy is a real file, distinct from the untouched original",
                  blurred_path.exists() and blurred_path.read_bytes() != before_pixels,
                  str(blurred_path))
        else:
            print("  (skipping Stage B live checks: the verified offline MegaDetector bundle "
                  "or ATRW/CCT20 evaluation data is not present)")

        # Stage B quarantines its own honestly-blank frames too now (same
        # mechanism as Stage A, see triage.py's _quarantine_move()), so the
        # summary's total spans both stages, not just Stage A's own count.
        summary_before = c.get(f"/api/runs/{tri_run_id}/triage").json()["summary"]
        check("the savings report reflects a real, physical quarantine from both stages",
              summary_before["quarantined"] == len(q_rows) + result["blank_by_detector"]
              and summary_before["bytes"] > 0,
              f"{summary_before} -- person_hours_saved rounds to 0.0 at this fixture's tiny "
              "scale, same as it would for a handful of real frames; bytes and count don't")

        restored = c.post(f"/api/runs/{tri_run_id}/quarantine/restore",
                          json={"actor": "tester"}).json()
        check("restore moves files back to their original path, Stage A's and Stage B's alike",
              restored["restored"] == len(q_rows) + result["blank_by_detector"]
              and all(Path(m["orig_path"]).exists() for m in on_disk), f"{restored}")

        # Prompt 5 / AUDIT_AND_REVISED_PLAN.md P2-6: two-pass scoring fixes
        # the background once per station/night group instead of building
        # it up causally frame by frame, so the result must not depend on
        # processing order. Test this directly against the real corpus
        # files -- all back at their original paths after the restore
        # above, before anything mutates them again.
        from edge.pipeline import triage as triage_pipeline
        band_frac = config.CONFIG.ingest.timestamp_band_frac
        current_rows = repo.images_for_run(tri_run_id)
        group_rows = [{"image_id": i["image_id"], "captured_at": i["captured_at"]}
                      for i in current_rows]
        grids = {i["image_id"]: triage_pipeline._read_grid(
                    i["orig_path"], config.CONFIG.triage.stage_a_grid, band_frac)
                 for i in current_rows}
        decisions_a = triage_pipeline._score_group(group_rows, grids, config.CONFIG.triage)
        shuffled_rows = group_rows[:]
        random.Random(20260813).shuffle(shuffled_rows)
        decisions_b = triage_pipeline._score_group(shuffled_rows, grids, config.CONFIG.triage)
        check("shuffling the input order produces the same verdict for every frame",
              decisions_a == decisions_b,
              f"{sum(1 for k in decisions_a if decisions_a[k] != decisions_b[k])} of "
              f"{len(decisions_a)} frames disagree")

        # And at the route level: re-running triage on the exact same
        # (now restored) frames must reach the exact same verdicts.
        repo.set_run_stage(tri_run_id, "confirmed")
        result_again = triage_pipeline.run_triage(tri_run_id)
        check("running triage twice on the same run produces the same quarantine count "
              "-- the background is fixed once per pass, not accumulated causally",
              result_again["quarantined"] == result["quarantined"]
              and result_again["awaiting_detector"] == result["awaiting_detector"],
              f"first quarantined={result['quarantined']} awaiting={result['awaiting_detector']}; "
              f"second quarantined={result_again['quarantined']} "
              f"awaiting={result_again['awaiting_detector']}")

        restored_again = c.post(f"/api/runs/{tri_run_id}/quarantine/restore",
                                json={"actor": "tester"}).json()
        check("the second triage run's quarantine can be restored too, same as the first",
              restored_again["restored"]
              == result_again["quarantined"] + result_again["blank_by_detector"],
              f"{restored_again}")
    finally:
        shutil.rmtree(tri_root, ignore_errors=True)
        repo.delete_run_cascade(tri_run_id)
        shutil.rmtree(config.QUARANTINE_DIR / tri_run_id, ignore_errors=True)

    # ── triage and the undo ─────────────────────────────────────────────
    t = c.get(f"/api/runs/{run_id}/triage").json()
    before = t["summary"]["quarantined"]
    check("person-hours computed", t["summary"]["person_hours_saved"] > 0,
          f"{t['summary']['person_hours_saved']} h")
    check("assumption is disclosed",
          "seconds_per_review_assumed" in t["summary"])
    check("stage A carries real load", (t["counts"]["stage_a"] or 0) > 0,
          f"{t['counts']['stage_a']} removed before the detector ran")

    r = c.post(f"/api/runs/{run_id}/quarantine/restore",
               json={"actor": "tester"}).json()
    check("restore returns frames", r["restored"] == before, f"{r['restored']}")
    after = c.get(f"/api/runs/{run_id}/triage").json()["summary"]
    check("restore is real, not cosmetic", after["quarantined"] == 0)
    r2 = c.post(f"/api/runs/{run_id}/quarantine/restore",
                json={"actor": "tester"}).json()
    check("restore is idempotent", r2["restored"] == 0)

    # ── individuals ─────────────────────────────────────────────────────
    inds = c.get(f"/api/individuals?reserve_id={rid}").json()
    check("catalogue populated", len(inds) == 13, f"{len(inds)}")
    check("provisional individuals exist",
          any(i["provisional"] for i in inds),
          "auto-enrolled tigers must still be reviewed")
    ind = c.get(f"/api/individuals/{inds[0]['ind_id']}").json()
    check("captures joined through to stations",
          bool(ind["captures"]) and "station_name" in ind["captures"][0])

    # ── entities: the real unit of re-identification (blueprint §7.3) ───
    ents = repo.entities(rid)
    check("entities exist and every one has a real side",
          len(ents) > 0 and all(e["side"] in ("L", "R") for e in ents), f"{len(ents)}")

    l_cat = repo.catalogue_for_side(rid, "L")
    r_cat = repo.catalogue_for_side(rid, "R")
    check("the L and R catalogues are disjoint -- an L crop has nothing to match "
          "against an R identity and vice versa",
          {e["entity_id"] for e in l_cat}.isdisjoint(e["entity_id"] for e in r_cat)
          and len(l_cat) > 0 and len(r_cat) > 0)

    bad_side_raised = False
    try:
        repo.catalogue_for_side(rid, "B")
    except ValueError:
        bad_side_raised = True
    check("catalogue_for_side refuses a bad side rather than returning an empty "
          "or wrong list", bad_side_raised)

    conn = repo.connect()
    orphaned = conn.execute(
        "SELECT COUNT(*) c FROM assignments a JOIN flank_crops c ON c.crop_id = a.crop_id"
        " WHERE a.superseded_by IS NULL AND c.side IN ('L','R') AND a.entity_id IS NULL"
    ).fetchone()["c"]
    check("no sided, confirmed assignment is left without an entity_id",
          orphaned == 0, f"{orphaned} orphaned")

    sf = repo.single_flank(rid)
    check("single-flank individuals exist in the fixture -- the field-common case, "
          "not the zoo-shot exception", len(sf) > 0, f"{len(sf)}")
    check("single-flank individuals are genuinely single-flank, not a stale view",
          all(s["sides_seen"] == 1 for s in sf))

    # ── identify.py: rectification, quality gate, embedding, matching ──────
    from edge.pipeline import identify as identify_pipeline

    bad_side_embeds = False
    try:
        repo.crop_embeddings_for_side(rid, "B")
    except ValueError:
        bad_side_embeds = True
    check("crop_embeddings_for_side refuses a bad side the same way "
          "catalogue_for_side does", bad_side_embeds)

    station_for_id = repo.stations(rid)[0]["station_id"]
    ind_for_id = inds[0]["ind_id"]
    ident_image_id = repo.new_id("img_")
    repo.insert_many("images", [dict(
        image_id=ident_image_id, reserve_id=rid, run_id=run_id, station_id=station_for_id,
        orig_path="synthetic/identify_test.jpg", sha256=ident_image_id, dhash=None,
        captured_at=repo.now(), captured_at_raw=None, captured_at_source="inferred",
        drift_applied_s=0, is_night=0, width=None, height=None, bytes=None,
        status="subject", triage_stage=None, flags="[]")])
    ident_det_id = repo.new_id("det_")
    repo.insert_many("detections", [dict(
        det_id=ident_det_id, image_id=ident_image_id, model="synthetic", model_version="0",
        label="animal", species="tiger", conf=0.9, x=None, y=None, w=None, h=None)])
    ident_crop_id = repo.new_id("crop_")
    repo.insert_many("flank_crops", [dict(
        crop_id=ident_crop_id, det_id=ident_det_id, side="R", rect_ok=0, quality=0,
        path=None, embedding=None, embed_model_version=None)])
    repo.insert_many("assignments", [dict(
        assign_id=repo.new_id("as_"), crop_id=ident_crop_id, ind_id=ind_for_id,
        score=0.91, method="embed", decision="auto", confidence=0.91,
        superseded_by=None, decided_at=repo.now(), actor="tester")])
    repo.rebuild_entities(rid)

    probe_embedding = np.random.default_rng(20260813).normal(
        size=identify_pipeline.EMBED_DIM).astype(np.float32)
    probe_embedding /= np.linalg.norm(probe_embedding)
    repo.save_crop_embedding(ident_crop_id, identify_pipeline.serialize_embedding(probe_embedding),
                              identify_pipeline.EMBED_MODEL_VERSION, 0.9, True)

    r_catalogue = repo.crop_embeddings_for_side(rid, "R")
    l_catalogue = repo.crop_embeddings_for_side(rid, "L")
    check("a saved embedding round-trips through the database exactly",
          any(np.array_equal(identify_pipeline.deserialize_embedding(row["embedding"]),
                              probe_embedding)
              for row in r_catalogue if row["crop_id"] == ident_crop_id))
    check("an R-side embedding never appears in the L-side catalogue",
          ident_crop_id not in {row["crop_id"] for row in l_catalogue})

    identify_model = identify_pipeline.TripletEmbedder()
    fake_image = np.zeros((300, 300, 3), dtype=np.uint8)
    fake_keypoints = {"right_shoulder": (60, 60, 2), "right_hip": (200, 90, 2)}
    catalogue_as_arrays = [
        {**row, "embedding": identify_pipeline.deserialize_embedding(row["embedding"])}
        for row in r_catalogue]
    identify_result = identify_pipeline.identify_crop(
        fake_image, fake_keypoints, catalogue_as_arrays, identify_model,
        config.CONFIG.identify)
    check("identify_crop runs the full pipeline end to end against a real, "
          "database-backed catalogue and reaches a real three-way decision",
          identify_result["decision"] in ("auto", "review", "enroll")
          and identify_result["side"] == "R" and identify_result["embedding"] is not None,
          str({k: v for k, v in identify_result.items()
               if k not in ("embedding", "candidates", "rect")}))

    ambiguous_result = identify_pipeline.identify_crop(
        fake_image,
        {"right_shoulder": (60, 60, 2), "right_hip": (200, 90, 2),
         "left_shoulder": (60, 200, 2), "left_hip": (200, 230, 2)},
        catalogue_as_arrays, identify_model, config.CONFIG.identify)
    check("identify_crop refuses when the side is not determinable, rather than "
          "guessing which catalogue to match against",
          ambiguous_result["decision"] == "refuse", str(ambiguous_result["reason"]))

    # ── /api/identify/upload: Task 5, the full chain behind a real route ──
    # Keypoints come from edge/pipeline/keypoints.py::estimate_keypoints(),
    # which uses the trained regressor when edge/models/keypoints/ exists
    # (Task 4) and falls back to the fixed geometric stub otherwise -- these
    # checks prove the pipe reaches the catalogue, review queue, and audit
    # log either way, not match accuracy (docs/RESULTS.md's "wild"
    # evaluation covers that). Guarded on the same optional, heavy downloads
    # as the Stage B checks above -- this suite must still pass on a fresh
    # clone that has not fetched megadetector/atrw/a trained embedder.
    if (detector_pipeline.CHECKPOINT_PATH.exists() and detector_pipeline.CONFIG_PATH.exists()
            and Path("data/raw/atrw/train").exists() and identify_pipeline.WEIGHTS_PATH.exists()):
        atrw_samples = sorted(Path("data/raw/atrw/train").glob("*.jpg"))[:3]
        if len(atrw_samples) >= 2:
            with open(atrw_samples[0], "rb") as f:
                up1 = c.post("/api/identify/upload",
                             files={"file": (atrw_samples[0].name, f, "image/jpeg")},
                             data={"reserve_id": rid, "actor": "live-suite"}).json()
            check("an upload of a real photo through the real route reaches a "
                  "genuine decision, landing an image/detection/crop row",
                  up1["decision"] in ("auto", "review", "enroll", "refuse", "no_animal_detected", "side_unknown", "non_target_species", "quality_refusal"),
                  str(up1))

            if up1["decision"] == "enroll":
                check("an enrolled upload creates a real, fetchable individual",
                      "ind_id" in up1 and repo.individual(up1["ind_id"]) is not None, str(up1))

                # The exact same photo again must match what was just enrolled --
                # both the stub and the trained regressor are deterministic
                # (same box -> same keypoints either way), so this is a real
                # assertion regardless of which one is behind
                # keypoints.estimate_keypoints() on this machine.
                with open(atrw_samples[0], "rb") as f:
                    up2 = c.post("/api/identify/upload",
                                 files={"file": (atrw_samples[0].name, f, "image/jpeg")},
                                 data={"reserve_id": rid, "actor": "live-suite"}).json()
                check("the identical photo, uploaded again, matches the entity "
                      "just enrolled from it -- determinism made real end to end, "
                      "not just at the function level",
                      up2["decision"] == "auto" and up2.get("ind_id") == up1["ind_id"],
                      str(up2))

            audit_upload = c.get("/api/audit?limit=10&q=identify.upload").json()
            check("every /api/identify/upload call is audited",
                  len(audit_upload) >= 1, f"{len(audit_upload)}")
    else:
        print("  (skipping /api/identify/upload live checks: the verified offline detector/"
              "embedder bundle or ATRW evaluation data is not present)")

    # ── privacy: role gating is a server control, not a UI suggestion ───
    precise = c.get(f"/api/individuals/{inds[0]['ind_id']}").json()
    coarse = c_analyst.get(f"/api/individuals/{inds[0]['ind_id']}").json()
    p_lat = precise["captures"][0]["lat"]
    a_lat = coarse["captures"][0]["lat"]
    check("analyst coordinates are generalised", p_lat != a_lat,
          f"{p_lat} vs {a_lat}")
    audit = c.get("/api/audit?limit=50&q=location.read").json()
    check("location reads are audited", len(audit) >= 2,
          "who looked at a tiger's locations is the question that matters")

    # ── review ──────────────────────────────────────────────────────────
    rv = c.get("/api/review").json()
    check("review queue populated", rv["open"] > 0, f"{rv['open']} open")
    check("queue is prioritised by impact",
          rv["items"][0]["priority"] >= rv["items"][-1]["priority"])

    # Confirming a review item MOVES a capture from one individual to another,
    # and the route recomputes alerts straight afterwards. The eight seeded
    # scenarios are this project's specification (CLAUDE.md), and every one of
    # them is a statement about a particular tiger's captures this cycle -- so
    # landing a decision on a scenario individual silently rewrites the fixture
    # that the alert checks several hundred lines below then assert against.
    #
    # That is exactly how this bit: the top of the queue is not the seeded item
    # on every run (the ingest, triage and /api/identify/upload stages above add
    # items of their own first), and on a run where it targeted PENCH-002 the
    # tiger whose whole scenario is BEING ABSENT gained a buffer-station
    # capture, the absence alert stopped existing, and the suite failed on a
    # bare next() far away from the cause.
    #
    # The correction still has to be a real one -- a different individual than
    # the crop currently carries, so an assignment genuinely supersedes -- it
    # just has to happen on a tiger the alert spec says nothing about.
    SCENARIO_INDS = {"PENCH-002", "PENCH-004", "PENCH-005", "PENCH-007",
                     "PENCH-009", "PENCH-011", "PENCH-P-001"}

    def _pick(items):
        """First queue item that can be decided without touching a scenario."""
        for it in items:
            cands = [x["ind_id"] for x in it["candidates"]]
            if not cands or cands[0] in SCENARIO_INDS:
                continue                     # would move a capture AWAY from one
            for other in cands[1:]:
                if other != cands[0] and other not in SCENARIO_INDS:
                    return it, other         # ...and this moves it TO a safe one
        return None, None

    item, target_ind = _pick(rv["items"])
    check("a review item exists that can be corrected without disturbing "
          "the eight alert scenarios", item is not None,
          "the seeded queue carries 14, spread across the whole catalogue")
    d = c.post(f"/api/review/{item['queue_id']}/decide",
               json={"ind_id": target_ind,
                     "actor": "tester"}).json()
    check("decision reduces the queue", d["remaining"] == rv["open"] - 1)
    check("the correction moved the crop to a different individual than it "
          "already carried", target_ind != item["candidates"][0]["ind_id"],
          f"{item['candidates'][0]['ind_id']} -> {target_ind}")

    conn = repo.connect()
    sup = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE superseded_by IS NOT NULL"
    ).fetchone()["c"]
    check("corrections supersede rather than overwrite", sup >= 1,
          "the record of who thought what must survive being corrected")

    # ── occupancy: a real MCP hull, projected before its area is measured ─
    occ = c.get(f"/api/runs/{run_id}/occupancy").json()
    check("occupancy computed", len(occ) > 0)

    # ── the map's sightings feed: corrections must reach the map ────────
    # This is run AFTER the review step above, which supersedes a real
    # assignment, so there is genuinely a corrected crop in the database by
    # the time these run.
    # Swept across EVERY run, not just this one. The review step above
    # corrects a crop in whichever run its queue item came from -- usually an
    # earlier cycle -- so checking only the current run passed without a
    # single superseded row to get wrong, i.e. it could not fail. The
    # assertion below refuses to pass unless there is real corrected data
    # somewhere to be got wrong.
    conn2 = repo.connect()
    all_runs = [r["run_id"] for r in repo.runs(run["reserve_id"], limit=50)]
    total_superseded = 0
    leaked_pairs = []
    # NOT `rid`: that name already holds the RESERVE id in this function, and
    # shadowing it here left it pointing at a run id, so a station export 200
    # lines later asked for a reserve that does not exist and returned zero
    # features.
    for each_run in all_runs:
        shown = {(r["event_id"], r["ind_id"]) for r in repo.map_events(each_run)}
        rows = conn2.execute(
            "SELECT e.event_id, a.ind_id FROM events e"
            " JOIN image_event ie ON ie.event_id = e.event_id"
            " JOIN images      im ON im.image_id = ie.image_id"
            " JOIN detections   d ON d.image_id  = im.image_id"
            " JOIN flank_crops  c ON c.det_id    = d.det_id"
            " JOIN assignments  a ON a.crop_id   = c.crop_id"
            " WHERE im.run_id = ? AND a.superseded_by IS NOT NULL", (each_run,)).fetchall()
        superseded = {(r["event_id"], r["ind_id"]) for r in rows}
        total_superseded += len(superseded)
        leaked_pairs += sorted(shown & superseded)

    check("the review step actually produced corrected data for this to be "
          "tested against", total_superseded > 0,
          f"{total_superseded} superseded sighting(s) across {len(all_runs)} runs")
    check("a corrected identification never reaches the map under its old "
          "name", not leaked_pairs,
          f"{len(leaked_pairs)} superseded pair(s) still drawn, e.g. {leaked_pairs[:2]}")

    sightings = repo.map_events(run_id)

    # Rule 1: this query used to live inline in edge/routes_scale.py, and the
    # missing superseded_by guard is exactly the kind of defect that hides
    # when there is more than one place to look for a query.
    scale_src = (Path(__file__).resolve().parents[2] / "edge/routes_scale.py").read_text(encoding="utf-8")
    check("the map's sighting query lives in repo.py, not in the route",
          "FROM events" not in scale_src and "repo.map_events(" in scale_src,
          "CLAUDE.md rule 1")

    # The hulls and the movement player read the same cycle; if they disagree
    # the map contradicts itself on screen.
    occ_stations = {o["ind_id"]: set(o.get("station_set") or []) for o in repo.occupancy(run_id)}
    disagree = sorted({
        r["ind_id"] for r in sightings
        if r["station_id"] not in occ_stations.get(r["ind_id"], set())})
    check("the movement player and the home ranges agree on where each "
          "tiger was", not disagree, str(disagree[:4]))

    # Occupancy counts an ungrouped frame as one visit (repo_ext
    # .occupancy_inputs' docstring is explicit about it). map_events used to
    # require the events join, so an import whose photos were never
    # burst-grouped -- no usable timestamps, which is every photo without
    # EXIF -- gave occupancy "3 visits" and the movement player "No sightings
    # this cycle" for the same tiger in the same cycle.
    # Compared against occupancy_inputs(), not the stored occupancy table.
    # Both are derived live from assignments, so they must agree exactly. The
    # stored table is a snapshot taken when occupancy was last computed, and
    # this suite inserts its own synthetic sighting afterwards -- comparing
    # against it made the check fail on the fixture rather than on the code.
    occ_counts = {ind: sum(r["event_count"] for r in rows)
                  for ind, rows in repo_ext.occupancy_inputs(run_id).items()}
    seen_counts: dict[str, int] = {}
    for r in sightings:
        seen_counts[r["ind_id"]] = seen_counts.get(r["ind_id"], 0) + 1
    off = [(i, seen_counts.get(i, 0), n) for i, n in occ_counts.items()
           if seen_counts.get(i, 0) != n]
    check("the sighting feed counts the same visits occupancy does",
          not off, f"{len(off)} tiger(s) disagree, e.g. {off[:3]}")

    # Undated sightings must be RETURNED, not filtered away: "nothing
    # happened" and "sightings whose photos carry no timestamp" are
    # different facts and the UI has to be able to say which.
    check("sightings with no timestamp are still reported, so the player can "
          "say why it has no timeline",
          all("started_at" in r for r in sightings),
          "started_at may be null, but the row must exist")

    check("the movement player and the home ranges agree on where each "
          "tiger was", not disagree, str(disagree[:4]))
    check("insufficient captures reported, not faked",
          any(o["insufficient_reason"] for o in occ),
          "a hull needs 3 stations; fewer must say so")

    with_area = [o for o in occ if o["area_km2"] is not None]
    check("home ranges carry a real, non-placeholder area",
          len({o["area_km2"] for o in with_area}) > 1,
          "areas should vary by individual, not all collapse to one number")
    check("every hull polygon closes (first point repeated last, valid WKT ring)",
          all(o["hull_wkt"].startswith("POLYGON((")
              and o["hull_wkt"].split("((")[1].split(",")[0].strip()
              == o["hull_wkt"].rstrip("))").split(",")[-1].strip()
              for o in with_area))

    known_square = occupancy.polygon_area_m2([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    check("shoelace on a known 1km square returns exactly 1 km2 -- blueprint's own unit test",
          abs(known_square - 1_000_000.0) < 1e-6, f"{known_square} m2")

    e1, n1 = occupancy.project_utm(21.6500, 79.3000, 32644)
    e2, n2 = occupancy.project_utm(21.6680, 79.3190, 32644)
    utm_km = math.hypot(e2 - e1, n2 - n1) / 1000
    mean_lat = math.radians((21.6500 + 21.6680) / 2)
    equi_km = math.hypot((21.6680 - 21.6500) * 111.0,
                          (79.3190 - 79.3000) * 111.320 * math.cos(mean_lat))
    check("UTM projection is within 1% of the independent flat-earth cross-check used "
          "elsewhere in this codebase (alerts.py's centroid-shift distance)",
          abs(utm_km - equi_km) / equi_km < 0.01, f"utm={utm_km:.4f}km equi={equi_km:.4f}km")

    # ── exports: the map is never the only way to get this data out ─────
    gj_resp = c.get(f"/api/runs/{run_id}/occupancy/export.geojson")
    check("GeoJSON export serves as a downloadable file",
          gj_resp.headers["content-type"].startswith("application/geo+json")
          and "attachment" in gj_resp.headers.get("content-disposition", ""))
    gj = gj_resp.json()
    check("GeoJSON is a real, valid FeatureCollection", gj["type"] == "FeatureCollection")
    with_hull = [o for o in with_area if o["hull_wkt"]]
    check("one polygon feature per individual with a real hull, not per row",
          len(gj["features"]) == len(with_hull), f"{len(gj['features'])} vs {len(with_hull)}")
    feat = gj["features"][0]
    check("feature geometry is a real polygon with real coordinates",
          feat["geometry"]["type"] == "Polygon" and len(feat["geometry"]["coordinates"][0]) >= 4)
    check("feature properties carry the same area the map/table shows",
          any(abs(f["properties"]["area_km2"] - o["area_km2"]) < 1e-9
              for f in gj["features"] for o in with_hull
              if f["properties"]["ind_id"] == o["ind_id"]))

    gj_analyst = c_analyst.get(f"/api/runs/{run_id}/occupancy/export.geojson").json()
    check("an analyst's export carries no precise geometry, only the aggregate area",
          all(f["geometry"] is None for f in gj_analyst["features"])
          and all(f["properties"]["area_km2"] is not None for f in gj_analyst["features"]))

    csv_resp = c.get(f"/api/runs/{run_id}/occupancy/export.csv")
    check("CSV export serves as a downloadable file",
          csv_resp.headers["content-type"].startswith("text/csv")
          and "attachment" in csv_resp.headers.get("content-disposition", ""))
    csv_lines = csv_resp.text.strip().splitlines()
    check("CSV has one data row per occupancy row, including insufficient ones",
          len(csv_lines) - 1 == len(occ), f"{len(csv_lines) - 1} vs {len(occ)}")
    check("CSV header names its columns", csv_lines[0].split(",")[:2] == ["ind_id", "run_id"])

    csv_analyst = c_analyst.get(f"/api/runs/{run_id}/occupancy/export.csv").text
    header = csv_analyst.strip().splitlines()[0].split(",")
    first_row = csv_analyst.strip().splitlines()[1].split(",")
    check("an analyst's CSV blanks the centroid columns rather than rounding them",
          first_row[header.index("centroid_lat")] == "")

    audit_exports = c.get("/api/audit?limit=50&q=occupancy.export").json()
    check("export downloads are audited like any other read of precise coordinates",
          len(audit_exports) >= 3, f"{len(audit_exports)} entries")

    stations_gj = c.get(f"/api/reserves/{rid}/stations/export/geojson").json()
    check("station list exports as real GeoJSON points too",
          stations_gj["type"] == "FeatureCollection"
          and len(stations_gj["features"]) == 36
          and stations_gj["features"][0]["geometry"]["type"] == "Point",
          f"type={stations_gj.get('type')} "
          f"features={len(stations_gj.get('features', []))} "
          f"geom0={(stations_gj.get('features') or [{}])[0].get('geometry', {}).get('type')}")

    # ── Camtrap DP: the community exchange format, not a private schema ──
    zip_resp = c.get(f"/api/runs/{run_id}/export/camtrapdp")
    check("Camtrap DP export serves as a downloadable zip",
          zip_resp.headers["content-type"].startswith("application/zip")
          and "attachment" in zip_resp.headers.get("content-disposition", ""))
    zf = zipfile.ZipFile(io.BytesIO(zip_resp.content))
    names = set(zf.namelist())
    check("package has exactly the three data tables plus its descriptor",
          names == {"datapackage.json", "deployments.csv", "media.csv", "observations.csv"},
          str(names))

    pkg = json.loads(zf.read("datapackage.json"))
    check("descriptor references the real Camtrap DP profile, not a made-up one",
          "camtrap-dp" in pkg.get("profile", "") and len(pkg.get("resources", [])) == 3)

    dep_rows = list(csv.DictReader(io.StringIO(zf.read("deployments.csv").decode())))
    check("deployments carry every field the spec marks required",
          len(dep_rows) > 0 and all(
              r["deploymentID"] and r["latitude"] and r["longitude"]
              and r["deploymentStart"] and r["deploymentEnd"] for r in dep_rows),
          f"{len(dep_rows)} deployment rows")

    media_rows = list(csv.DictReader(io.StringIO(zf.read("media.csv").decode())))
    check("media rows carry every field the spec marks required",
          len(media_rows) > 0 and all(
              r["mediaID"] and r["deploymentID"] and r["timestamp"] and r["filePath"]
              and r["fileMediatype"] for r in media_rows),
          f"{len(media_rows)} media rows")
    check("media count roughly matches this run's real frame count",
          abs(len(media_rows) - counts["total"]) <= 5,
          f"{len(media_rows)} media rows vs {counts['total']} frames")

    obs_rows = list(csv.DictReader(io.StringIO(zf.read("observations.csv").decode())))
    check("observations carry every field the spec marks required",
          len(obs_rows) > 0 and all(
              r["observationID"] and r["deploymentID"] and r["eventStart"]
              and r["eventEnd"] and r["observationLevel"] and r["observationType"]
              for r in obs_rows), f"{len(obs_rows)} observation rows")
    check("tiger detections get a real scientific name, not just a species code",
          any(r["scientificName"] == "Panthera tigris" for r in obs_rows))
    check("confirmed identifications carry the individual's ID through to the export",
          any(r["individualID"] for r in obs_rows))
    # run_03's own blank frames were already restored to 'pending' by the
    # quarantine-undo test above by this point in the suite, so this checks
    # the module directly with a synthetic blank row instead of relying on
    # the shared run's current (already-mutated) state.
    from edge.exports import camtrapdp as camtrapdp_module
    fake_dep_lookup = {"ST1": [{"activity_id": "act1", "start_date": "2020-01-01", "end_date": None}]}
    fake_blank_obs = camtrapdp_module.observations_table(
        [], [{"image_id": "im1", "station_id": "ST1", "captured_at": "2026-01-01T00:00:00"}],
        fake_dep_lookup)
    check("blank frames are represented in the export, not silently dropped",
          len(fake_blank_obs) == 1 and fake_blank_obs[0]["observationType"] == "blank")

    zip_analyst = c_analyst.get(f"/api/runs/{run_id}/export/camtrapdp")
    zf_a = zipfile.ZipFile(io.BytesIO(zip_analyst.content))
    dep_analyst = list(csv.DictReader(io.StringIO(zf_a.read("deployments.csv").decode())))
    check("an analyst's Camtrap DP export has no precise station coordinates either",
          all(r["latitude"] == "" and r["longitude"] == "" for r in dep_analyst))

    audit_ctdp = c.get("/api/audit?limit=50&q=camtrapdp.export").json()
    check("Camtrap DP downloads are audited too", len(audit_ctdp) >= 2, f"{len(audit_ctdp)}")

    # ── alerts: the core of the product ─────────────────────────────────
    raised = c.get(f"/api/runs/{run_id}/alerts?suppressed=false").json()
    held = c.get(f"/api/runs/{run_id}/alerts?suppressed=true").json()
    check("alerts raised", len(raised["items"]) >= 4, f"{len(raised['items'])}")
    check("alerts suppressed", len(held["items"]) >= 4, f"{len(held['items'])}")

    kinds = {a["type"] for a in raised["items"]}
    check("buffer-ward movement is raised", "buffer_ward" in kinds)
    check("buffer-ward is the top severity",
          any(a["type"] == "buffer_ward" and a["severity"] == "act"
              for a in raised["items"]),
          "it is the alert that precedes conflict")

    held_by_type = {a["type"]: a for a in held["items"]}
    check("absence suppressed when cameras died",
          held_by_type["absence"]["effort_coverage"] < 0.6,
          f"coverage {held_by_type['absence']['effort_coverage']}")
    check("suppression explains itself",
          all(a["suppress_reason"] for a in held["items"]))
    check("new-station suppressed for a camera installed this cycle",
          "installed this cycle" in held_by_type["new_station"]["suppress_reason"])

    # ── all four rules actually engage, not just the ones with an easy win ─
    all_types = kinds | set(held_by_type)
    check("all four rule types produced a candidate this run",
          {"centroid_shift", "new_station", "buffer_ward", "absence"}.issubset(all_types),
          str(sorted(all_types)))
    check("one individual can trip two different rules in one cycle",
          any(a["type"] == "centroid_shift" for a in raised["items"]
              if a["ind_id"] == next(x["ind_id"] for x in raised["items"]
                                     if x["type"] == "buffer_ward")),
          "the buffer-ward individual should also show the centroid shift that goes with it")
    check("centroid shift is suppressed for having too few events, specifically",
          "events" in held_by_type["centroid_shift"]["suppress_reason"].lower(),
          held_by_type["centroid_shift"]["suppress_reason"])
    check("a genuine new-station capture is NOT suppressed",
          any(a["type"] == "new_station" for a in raised["items"]),
          "PENCH-011's case: a pre-existing station it simply hadn't used before")

    # Guarded rather than a bare next(): when this went missing it raised
    # StopIteration and killed the whole run with no indication of which
    # scenario had gone or why. A named failure that lists what DID fire is
    # the difference between a five-minute diagnosis and an hour of it.
    absences = [a for a in raised["items"] if a["type"] == "absence"]
    check("the absence scenario still fires (PENCH-002, cameras all healthy)",
          bool(absences),
          "raised: " + ", ".join(sorted(f"{a['type']}/{a['ind_id']}"
                                        for a in raised["items"])))
    real_absence = absences[0]
    check("absence IS raised when effort was good",
          real_absence["effort_coverage"] >= 0.6,
          f"coverage {real_absence['effort_coverage']}")
    check("a dead range and a healthy range score very differently on coverage",
          real_absence["effort_coverage"] - held_by_type["absence"]["effort_coverage"] > 0.5,
          f"{real_absence['effort_coverage']} vs {held_by_type['absence']['effort_coverage']}")

    # ── the effort primitive itself, not just its downstream alert effect ──
    window_end = (datetime.fromisoformat(run["started_at"]) + timedelta(days=30)).isoformat()
    dead = effort.station_days(["PN-C-008"], run["started_at"], window_end)
    alive = effort.station_days(["PN-C-026"], run["started_at"], window_end)
    check("a station that failed mid-cycle logs far fewer camera-days than one that didn't",
          dead < alive / 2, f"dead={dead} alive={alive} over the same 30-day window")

    low_id = next(a for a in held["items"] if a["ind_id"].startswith("PENCH-P"))
    check("alert confidence never exceeds the identification beneath it",
          low_id["confidence"] <= 0.5, f"{low_id['confidence']}")

    # ── the eight scenarios again, from nothing but generated data ─────────
    # The checks above prove the demo pipeline produces the right alerts
    # end to end. This proves the engine itself is correct, independent of
    # everything upstream of it: tests/scenarios/test_alert_scenarios.py
    # builds its own tiny reserve directly against repo.py (no images, no
    # ingest, no triage) and reproduces all eight rows of the spec table
    # in CLAUDE.md from that alone (AUDIT_AND_REVISED_PLAN.md Prompt 6).
    from tests.scenarios.test_alert_scenarios import run_scenarios
    scenario_passed, scenario_failed = run_scenarios()
    check("all eight alert scenarios reproduce from synthetic, generated data alone "
          "-- not just the demo seed's own hardcoded rows",
          len(scenario_failed) == 0 and len(scenario_passed) == 8,
          f"{len(scenario_passed)} passed, {len(scenario_failed)} failed: {scenario_failed}")

    # ── audit immutability ──────────────────────────────────────────────
    try:
        conn.execute("UPDATE audit_log SET actor='x' WHERE log_id=1")
        conn.commit()
        check("audit_log blocks UPDATE", False, "update succeeded")
    except Exception:
        # SQLite keeps the failed write transaction open until rollback.
        # Without this, the next authenticated request runs on FastAPI's
        # worker thread and correctly finds the test's own main-thread
        # transaction holding the database write lock.
        conn.rollback()
        check("audit_log blocks UPDATE", True)
    try:
        conn.execute("DELETE FROM audit_log WHERE log_id=1")
        conn.commit()
        check("audit_log blocks DELETE", False, "delete succeeded")
    except Exception:
        conn.rollback()
        check("audit_log blocks DELETE", True)

    # ── sync reports honestly: two separate truths, not one blurred ─────
    s = c.get(f"/api/sync/status?reserve_id={rid}").json()
    check("bundle sync is genuinely enabled once a secret is configured",
          s["bundle_sync_enabled"] is True and s["bundle_sync_reason"] is None)
    check("the central tier is honestly still not there, unrelated to bundle sync",
          s["central_tier_enabled"] is False and bool(s["central_tier_reason"]))
    check("this node reports its own persistent identity",
          bool(s["node_id"]) and len(s["node_id"]) >= 8)

    # node_identity is a singleton at the database level, not by
    # convention (AUDIT_AND_REVISED_PLAN.md P1-5) -- a second row must be
    # rejected outright, since origin_node is exactly what sync merges on
    # and an ambiguous identity would corrupt every node this one syncs with
    conn = repo.connect()
    second_identity_raised = False
    try:
        conn.execute("INSERT INTO node_identity (id, node_id, lamport_counter) "
                     "VALUES (2, 'impostor', 0)")
        conn.commit()
    except Exception:
        # As with the audit-trigger probes above, release SQLite's failed
        # write transaction before the next authenticated request is handled
        # on a TestClient worker thread.
        conn.rollback()
        second_identity_raised = True
    check("a second node identity is rejected at the database level, not just by convention",
          second_identity_raised)

    entity_tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name IN ('entities','single_flank_individuals')")}
    check("the entity model's table and view exist after migration",
          entity_tables == {"entities", "single_flank_individuals"}, str(entity_tables))

    detection_columns = {r["name"] for r in conn.execute("PRAGMA table_info(detections)")}
    crop_columns = {r["name"] for r in conn.execute("PRAGMA table_info(flank_crops)")}
    check("species and side model evidence is persisted separately from pose quality",
          {"species_conf", "species_source", "species_model_version"} <= detection_columns
          and {"side_confidence", "side_source", "side_model_version"} <= crop_columns,
          f"detections={sorted(detection_columns)} crops={sorted(crop_columns)}")

    # ── the M-STrIPES adapter refuses rather than guesses a schema ──────
    from edge.exports import mstripes
    refusals = 0
    for fn, args in ((mstripes.import_stations, ("x",)),
                     (mstripes.import_patrol_tracks, ("x",)),
                     (mstripes.export_observations, ([],))):
        try:
            fn(*args)
        except mstripes.SchemaNotAvailable as e:
            refusals += 1 if str(e) else 0
    check("every M-STrIPES adapter function refuses with a real reason, "
          "not a fabricated result", refusals == 3, f"{refusals}/3")

    # ── UI is served, and reaches for nothing off-machine ───────────────
    page = c.get("/").text
    check("page served", "PUG" in page)
    ui = Path(__file__).resolve().parents[2] / "edge/ui"
    js = (ui / "app.js").read_text(encoding="utf-8")
    css = (ui / "app.css").read_text(encoding="utf-8")
    # map.js was NOT checked before this, which was the worst possible file to
    # omit: it is the one that decides where map tiles come from. A CDN tile
    # URL there would have passed every check in this suite and shown a grey
    # rectangle at the demo.
    mapjs = (ui / "map.js").read_text(encoding="utf-8")
    for name, text in (("page", page), ("script", js), ("stylesheet", css),
                       ("map script", mapjs)):
        offenders = [t for t in ("http://", "https://", "cdn.", "googleapis",
                                 "unpkg", "jsdelivr", "tile.openstreetmap",
                                 "arcgisonline", "openstreetmap")
                     if t in text]
        check(f"{name} fetches nothing off this machine", not offenders,
              str(offenders))

    # Every first-party UI file, not just the four named above -- a new file
    # must not be able to reach the network merely by not being on a list.
    for f in sorted(ui.rglob("*.js")) + sorted(ui.rglob("*.css")) + sorted(ui.rglob("*.html")):
        if "vendor" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        bad = [t for t in ("http://", "https://") if t in text]
        check(f"ui/{f.relative_to(ui).as_posix()} has no off-machine URL", not bad, str(bad))

    # ── the map: Leaflet is vendored, the tiles are on disk ─────────────
    leaflet_js = ui / "vendor/leaflet/leaflet.js"
    leaflet_css = ui / "vendor/leaflet/leaflet.css"
    check("Leaflet is vendored into the repo, not fetched from a CDN",
          leaflet_js.exists() and leaflet_css.exists(),
          "edge/ui/vendor/leaflet/")
    lf = leaflet_js.read_text(encoding="utf-8", errors="replace")
    # the SVG namespace is an XML identifier that createElementNS matches on,
    # never a URL that is fetched -- it is the one permitted exception
    stray = [u for u in re.findall(r"https?://[^\s'\"\)]+", lf)
             if u != "http://www.w3.org/2000/svg"]
    check("the vendored Leaflet bundle references no off-machine URL",
          not stray, str(stray[:3]))
    check("the vendored Leaflet ships its own icons, so leaflet.css resolves "
          "locally", (ui / "vendor/leaflet/images/marker-icon.png").exists())

    tiles = ui / "tiles"
    tile_files = list(tiles.rglob("*.jpg")) if tiles.exists() else []
    check("the offline basemap tile pyramid is present",
          len(tile_files) > 500, f"{len(tile_files)} tiles")
    zooms = sorted({int(t.parent.parent.name) for t in tile_files}) if tile_files else []
    check("the pyramid covers a usable zoom range, not one flat level",
          len(zooms) >= 4, f"z{zooms}")

    manifest = json.loads((ui / "img/basemap-pench.json").read_text(encoding="utf-8"))
    check("the basemap manifest records the tile template the UI reads",
          manifest.get("tiles", "").startswith("/ui/"), manifest.get("tiles"))
    check("the manifest's deepest zoom is one that was actually downloaded",
          manifest.get("max_native_zoom") in zooms,
          f"manifest says z{manifest.get('max_native_zoom')}, on disk {zooms}")

    # served, not merely present on disk
    sample = tile_files[len(tile_files) // 2]
    z, x, y = sample.parent.parent.name, sample.parent.name, sample.stem
    tile_res = c.get(f"/ui/tiles/{z}/{x}/{y}.jpg")
    check("a basemap tile is actually served over HTTP",
          tile_res.status_code == 200 and tile_res.content[:3] == bytes((0xFF, 0xD8, 0xFF)),
          f"{tile_res.status_code}, {len(tile_res.content)} bytes")
    check("the vendored Leaflet is served over HTTP",
          c.get("/ui/vendor/leaflet/leaflet.js").status_code == 200)

    # ── the world basemap: vectors, so zooming out has somewhere to go ──
    # The satellite pyramid covers the reserve only. Without this layer the
    # map ran out of data past the reserve boundary, which is not a map --
    # and a raster world would be both enormous and impossible to recolour
    # for the dark theme.
    geo = ui / "geo"
    for layer in ("ocean", "countries", "states", "lakes"):
        f = geo / f"{layer}.geojson"
        check(f"world basemap layer '{layer}' ships with the app", f.exists(),
              str(f))
        if not f.exists():
            continue
        gj = json.loads(f.read_text(encoding="utf-8"))
        check(f"'{layer}' carries real geometry, not an empty stub",
              len(gj.get("features", [])) > 0, f"{len(gj.get('features', []))} features")
        served = c.get(f"/ui/geo/{layer}.geojson")
        check(f"'{layer}' is served over HTTP", served.status_code == 200,
              str(served.status_code))

    world_bytes = sum(f.stat().st_size for f in geo.glob("*.geojson")) if geo.exists() else 0
    check("the whole world costs a few MB, not a tile pyramid's worth",
          0 < world_bytes < 12 * 1024 * 1024, f"{world_bytes/1024/1024:.2f} MB")
    check("state/province borders are included, not just country outlines",
          len(json.loads((geo / "states.geojson").read_text(encoding="utf-8"))["features"]) > 100,
          "India's state boundaries are what make the zoomed-out view legible")

    # The two pre-toned rasters the SVG map used are superseded by the tile
    # pyramid; leaving them behind would ship 636 KB nothing reads.
    # The map's palette must be driven by the same signal app.js writes --
    # dark sets data-theme="dark", light REMOVES the attribute. When map.js
    # fell back to prefers-color-scheme for the missing-attribute case, light
    # mode on a dark-OS machine left the world map green while the page went
    # white, and the theme button appeared not to affect the map at all.
    # Assert on the MECHANISM, not on the word: an earlier version of this
    # check grepped for "prefers-color-scheme" and failed on the comment
    # explaining why the code must not use it. matchMedia is the only way to
    # reach the OS preference from script, so its absence is the real
    # guarantee.
    check("the map reads the theme from the attribute app.js actually sets, "
          "never from the operating system",
          "matchMedia" not in mapjs and 'getAttribute(\'data-theme\')' in mapjs,
          "map.js must not consult the OS colour scheme")
    # A signed-out session hides the application by COVERING it, not by
    # unmounting it, so anything inside a view that competes at page level
    # will draw straight through the login screen. That is exactly what
    # happened: the map's layer bar and zoom column (z-index 1000) sat on top
    # of the tiger after logging out, because .auth-overlay carried a second,
    # contradictory z-index of 200.
    zs = [int(m) for m in re.findall(r"\.auth-overlay\s*\{[^}]*?z-index:\s*(\d+)", css, re.S)]
    check("the sign-in overlay has one z-index, and it outranks the app's "
          "own floating chrome", zs and len(set(zs)) == 1 and min(zs) >= 9999,
          f"declared z-indexes: {zs}")
    check("the map card is its own stacking context, so its controls cannot "
          "escape onto other screens",
          "isolation: isolate" in css,
          "otherwise map furniture competes with every overlay in the app")
    check("scrollbars are themed rather than left as OS chrome",
          "--sb-thumb" in css and "::-webkit-scrollbar" in css,
          "an unstyled scrollbar is a white stripe down a near-black UI")
    # ── photographs must actually load ─────────────────────────────────
    # Every thumbnail is loading="lazy" and was hidden with display:none
    # until an onload handler added .loaded. That is a deadlock -- a
    # display:none image has no layout box, so the lazy observer never
    # intersects it, so it never fetches, so onload never fires. Nothing in
    # the app displayed a tiger photograph; the .stripe-thumb gradient made
    # the empty frames look like small tawny photos, which is why it went
    # unnoticed. Officers were identifying tigers by reading catalogue IDs.
    check("thumbnails are not hidden behind an onload that lazy loading "
          "prevents from firing",
          ".stripe-thumb .real-crop { display: block" in css,
          "display:none + loading=lazy never resolves")
    check("a thumbnail with no photo says so by default, rather than only "
          "on an error that may never fire",
          'class="no-photo${compact}">No photo on file' in js,
          "a request that neither loads nor errors left an unexplained blank")
    # A crop is the stripes with the context cut away. The frame it came from
    # says where the animal was and what else was in shot, and a reviewer
    # deciding an identity is entitled to it.
    ind_with_crop = next((i["ind_id"] for i in c.get(
        f"/api/individuals?reserve_id={rid}").json()
        if repo.latest_crop_path(i["ind_id"])), None)
    if not ind_with_crop:
        # Say so out loud. Three checks that quietly do not run look exactly
        # like three checks that passed, and the seeded catalogue has no crop
        # files on disk -- only a real ingest produces them.
        print("  (skipping source-frame checks: no individual in this fixture has "
              "a crop image on disk — run an ingest to exercise them)")
    if ind_with_crop:
        crop_res = c.get(f"/api/individuals/{ind_with_crop}/thumbnail")
        src_res = c.get(f"/api/individuals/{ind_with_crop}/source", follow_redirects=True)
        check("an individual's source frame is reachable from its thumbnail",
              src_res.status_code == 200 and src_res.content[:3] == bytes((0xFF, 0xD8, 0xFF)),
              f"{src_res.status_code}, {len(src_res.content)} bytes")
        check("the source frame is the whole photo, not the crop again",
              len(src_res.content) > len(crop_res.content),
              f"crop {len(crop_res.content)} vs frame {len(src_res.content)} bytes")
        # The redirect target carries the persons_restricted refusal; a second
        # file server here would be a second place to forget it.
        raw = c.get(f"/api/individuals/{ind_with_crop}/source", follow_redirects=False)
        check("the source route redirects to the one image route that enforces "
              "the person-frame refusal",
              raw.status_code in (307, 308)
              and "/api/images/" in raw.headers.get("location", ""),
              f"{raw.status_code} -> {raw.headers.get('location')}")

    # "Locate on Map" has three ways to silently do nothing, all of them hit
    # in use: the focus marker lived in a layer that defaults off; setting
    # location.hash to the hash already in the bar fires no hashchange, so
    # nothing re-renders; and `main` is the scrolling element, so resetting
    # window.scrollY leaves the map scrolled out of view.
    # Acknowledge wrote acknowledged_by/acknowledged_at and an audit entry
    # from the day it was added, and the UI never read either column back --
    # so the list re-rendered identically and the button looked ornamental.
    # An invisible effect is indistinguishable from no effect.
    check("acknowledging an alert changes what the operator sees",
          "acknowledged_at" in js and "is-acknowledged" in js
          and ".alert-card-rich.is-acknowledged" in css,
          "the button wrote to the database but nothing on screen moved")

    # stripeRail() draws bands from a hash of the id. On a screen that asks
    # someone to recognise an animal, that is an invented pattern presented
    # as a photograph.
    # Only real call sites: skip the definition, and skip prose mentions like
    # "see flankThumb()'s comment", which the previous version of this check
    # counted as dishonest calls and failed on.
    dishonest = []
    for line in js.splitlines():
        if "flankThumb(" not in line or "function flankThumb" in line:
            continue
        arg = line.split("flankThumb(", 1)[1]
        if arg.startswith(")"):          # a bare mention in a comment
            continue
        call = arg.split(")", 1)[0]
        if not call.rstrip().endswith("true"):
            dishonest.append(line.strip()[:70])
    check("no screen falls back to a generated stripe pattern in place of a "
          "photograph", not dishonest, f"non-honest flankThumb calls: {dishonest[:3]}")

    check("alerts reach the map through the same helper as the catalogue",
          "locateOnMap(b.dataset.ind)" in js,
          "a raw hash assignment does nothing when the map is already open")
    check("alerts reach a tiger profile without guessing at render timing",
          "openTigerProfile" in js and "}, 200);" not in js.split("openTigerProfile")[0][-400:],
          "the old path waited 200ms and hoped the DOM existed")

    # Re-asking for the tiger you are ALREADY focused on has to move the
    # camera. The fly-to only fired on focus !== lastFocus, so after panning
    # away, pressing "Inspect on Map" again for the same animal did nothing --
    # precisely the case where you most want it. It looked like it "only
    # worked for slight deviations" because a small pan left the animal on
    # screen anyway.
    check("an explicit locate re-centres even when the tiger is already "
          "focused",
          "data?.recenter" in mapjs and "S.mapRecenter = true" in js,
          "focus !== lastFocus alone ignores a repeat request")
    # ...but it must be one-shot, or an ordinary data refresh would drag the
    # view back and undo the user's own panning.
    check("the recentre request is consumed, so a routine refresh leaves the "
          "view alone", "S.mapRecenter = false" in js,
          "otherwise every poll yanks the map back to the focused tiger")

    check("focusing an individual is drawn outside the toggleable layers",
          "pug-focus" in mapjs and "focus-halo" in mapjs,
          "a focused tiger must not depend on Home Ranges being switched on")
    check("locating an individual goes through one helper, not a raw hash "
          "assignment", "window.locateOnMap" in js
          and "location.hash='#map';\"" not in js,
          "assigning the current hash fires no hashchange and re-renders nothing")
    check("view changes reset the scroller that actually scrolls",
          "scrollViewToTop" in js and "querySelector('main')" in js,
          "main carries overflow-y:auto; window.scrollTo is a no-op here")

    check("any photo can be opened full size",
          "photo-viewer" in js and ".photo-viewer" in css,
          "a 34px thumbnail cannot settle whether two tigers are the same")

    # The human shortlist is per individual: match() ranks catalogue
    # ENTITIES, and one tiger can hold several photos of one flank, so the
    # same animal occupied several slots in the review list.
    from edge.pipeline.identify import _shortlist
    dupes = [{"ind_id": "A", "score": 0.8}, {"ind_id": "A", "score": 0.7},
             {"ind_id": "B", "score": 0.6}, {"ind_id": "A", "score": 0.5},
             {"ind_id": "C", "score": 0.4}]
    short = _shortlist(dupes, 5)
    ids = [r["ind_id"] for r in short]
    # ── collapsed pose must not become a catalogue entry ───────────────
    # Observed on a real upload: a clean frame, detector confident at 0.93
    # over an 824x586 box, and the 2-keypoint regressor put shoulder and hip
    # 132 px apart -- 16% of the animal. Rectifying from that produced a
    # 188x143 patch of fence, which was enrolled as a tiger's stripe
    # pattern. quality_gate() scored it 1.0, because it scores keypoint
    # VISIBILITY, not whether two points are a plausible distance apart on
    # the animal they belong to. The detection box is the independent
    # evidence of scale.
    import numpy as _np
    from edge.pipeline import identify as _ident
    # A gradient, not zeros: on a black frame both rectangles sample black and
    # compare equal no matter where they were taken from, so the check passed
    # nothing. Here each pixel encodes its own position, so two different
    # sampled regions cannot come out identical.
    _yy, _xx = _np.mgrid[0:1080, 0:1920]
    _frame = _np.dstack([(_xx // 8) % 256, (_yy // 8) % 256,
                         ((_xx + _yy) // 8) % 256]).astype(_np.uint8)
    _box = (0.1155, 0.363, 0.4292, 0.5424)          # the real detection
    _collapsed = {"right_shoulder": (639.0, 776.0, 2), "right_hip": (519.0, 721.0, 2)}
    _icfg = config.CONFIG.identify

    _no_box = _ident.rectify_flank(_frame, _collapsed, "R", _icfg)
    _with_box = _ident.rectify_flank(_frame, _collapsed, "R", _icfg, _box)
    check("a collapsed shoulder/hip pair is caught against the detection box",
          _no_box is not None and _with_box is not None
          and not _np.array_equal(_no_box, _with_box),
          "the box fallback must produce a different rectangle than the "
          "collapsed pose did")

    # a healthy pose spanning most of the animal must be left alone
    _healthy = {"right_shoulder": (300.0, 700.0, 2), "right_hip": (1000.0, 700.0, 2)}
    check("a healthy pose is still rectified from the keypoints, not overridden",
          _np.array_equal(_ident.rectify_flank(_frame, _healthy, "R", _icfg),
                          _ident.rectify_flank(_frame, _healthy, "R", _icfg, _box)),
          "the fallback must engage only when the pose collapses")

    check("the body-span floor is a named threshold, not a literal in the "
          "pipeline", hasattr(config.CONFIG.identify, "min_body_span_ratio"),
          "CLAUDE.md rule 2")

    # The fallback must survive a config object that predates the field.
    # It did not: reading cfg.min_body_span_ratio directly raised
    # AttributeError inside stage3's per-detection try, which counted every
    # one as "unreadable" -- an entire 54-frame import produced 0 tigers and
    # told the operator that 29 frames could not be read.
    class _OldIdentifyCfg:
        rect_body_depth_ratio = 0.6
        rect_margin_ratio = 0.15
    _old = _OldIdentifyCfg()
    try:
        _ident.rectify_flank(_frame, _collapsed, "R", _old, _box)
        _survived = True
    except AttributeError:
        _survived = False
    check("the pose fallback tolerates a config built before its threshold "
          "existed", _survived,
          "a missing config field must not crash identification")

    check("the review shortlist offers each tiger once, at its best score",
          len(ids) == len(set(ids)) and short[0]["score"] == 0.8,
          str([(r["ind_id"], r["score"]) for r in short]))

    check("the satellite layer states its own coverage limit",
          "mapImageryNote" in page and "reserve only" in mapjs,
          "a lit button over blank imagery reads as broken")

    check("the superseded stitched basemap rasters are gone",
          not (ui / "img/basemap-dark.jpg").exists()
          and not (ui / "img/basemap-light.jpg").exists())
    check("the page loads Leaflet from this machine",
          "/ui/vendor/leaflet/leaflet.js" in page and "/ui/vendor/leaflet/leaflet.css" in page)

    # ── /api/dev/seed: last, since it replaces the whole database ───────
    # Runs each seeder as the real subprocess it is in production, proving
    # the fix for a real bug found by hand: repo.py caches one SQLite
    # connection per thread, and this suite's own prior requests leave
    # several such connections open, so without repo.close_all() (not
    # close(), which only reaches the calling thread) Windows refuses to
    # even delete the old database file -- confirmed by reproducing the
    # PermissionError before the fix existed.
    bad = c.post("/api/dev/seed", json={"which": "not-a-real-option"})
    check("an unknown seed option is refused, not silently ignored",
          bad.status_code == 400)

    blanked = c.post("/api/dev/seed", json={"which": "blank"}).json()
    check("the blank option runs and reports success", blanked.get("ok") is True,
          str(blanked))
    after_blank = c.get("/api/individuals?reserve_id=PENCH-MH").json()
    check("the blank option genuinely empties the catalogue, not just returns ok",
          after_blank == [], f"{len(after_blank)} individuals left")

    reseeded = c.post("/api/dev/seed", json={"which": "demo"}).json()
    check("the demo option runs and reports success", reseeded.get("ok") is True,
          str(reseeded))
    after_demo = c.get("/api/individuals?reserve_id=PENCH-MH").json()
    check("the demo option genuinely restores the 13-tiger spec fixture, "
          "not just returns ok", len(after_demo) == 13, f"{len(after_demo)} individuals")

    # ── restoring live data must not restore IDENTITIES ────────────────
    # Reported from use: clicking "show my live data" signed the operator out
    # and their admin password stopped working. A SQLite file carries the
    # users and sessions tables alongside the survey data, so swapping the
    # data swapped the credentials back to whoever they were when the
    # snapshot was taken -- a button labelled as a VIEW locked an admin out
    # of their own machine. Restoring is about data; who may log in is a
    # property of the machine now, not of the snapshot.
    scratch = Path(tempfile.mkdtemp(prefix="pugmark_restore_"))
    try:
        live_db, snap_db = scratch / "live.db", scratch / "snap.db"
        for dest in (live_db, snap_db):
            src_c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
            dst_c = sqlite3.connect(dest)
            try:
                src_c.backup(dst_c)
            finally:
                src_c.close(); dst_c.close()

        for path, mark in ((snap_db, "SNAPSHOT-HASH"), (live_db, "CURRENT-HASH")):
            conn = sqlite3.connect(path)
            conn.execute("UPDATE users SET pwd_hash=? WHERE username='admin'", (mark,))
            conn.commit(); conn.close()

        real_db = config.DB_PATH
        try:
            config.DB_PATH = live_db
            outcome = repo_ext.restore(snap_db)
        finally:
            config.DB_PATH = real_db

        conn = sqlite3.connect(live_db)
        after_hash = conn.execute(
            "SELECT pwd_hash FROM users WHERE username='admin'").fetchone()[0]
        conn.close()
        check("restoring live data keeps the CURRENT login credentials, not "
              "the snapshot's", after_hash == "CURRENT-HASH",
              f"admin hash after restore: {after_hash}")
        check("restore reports which accounts it preserved",
              outcome.get("accounts_preserved") is True
              and outcome.get("users_kept", 0) > 0,
              str({k: outcome.get(k) for k in ("accounts_preserved", "users_kept")}))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print("\n".join(f"  ok   {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {f}" for f in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
