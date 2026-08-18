"""PUGMARK edge node — HTTP surface.

The edge node is a complete, standalone application. It never requires the
central tier, never requires the internet, and never blocks on either.

Binding is 127.0.0.1 by default and deliberately: this process holds precise
locations of individual tigers, which is exactly what a poaching network
would want. Exposing it on 0.0.0.0 is an explicit, logged decision.
"""
from __future__ import annotations

import io
import json
import os
import string
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles

from edge import auth, config
from edge.db import repo
from edge.db import repo_ext
from edge.exports import camtrapdp
from edge.exports import csv as csv_export
from edge.exports import geojson as geojson_export
from edge.sync import bundle as bundle_sync

UI_DIR = Path(__file__).resolve().parent / "ui"

app = FastAPI(title="Pugmark", version=config.APP_VERSION, docs_url="/api/docs")


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/ui/"):
        response.headers["Cache-Control"] = "no-store"
    return response


SESSION_COOKIE = "pugmark_session"


@app.middleware("http")
async def resolve_session(request: Request, call_next):
    token = request.cookies.get(SESSION_COOKIE)
    request.state.user = repo.session_user(auth.hash_token(token)) if token else None
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    applied = repo.migrate()
    if applied:
        repo.audit("schema.migrate", after={"applied": applied,
                                            "version": repo.schema_version()})

    # Ensure admin user is seeded so node is immediately usable offline
    adm = repo.ensure_admin(must_change=True)
    if adm["created"]:
        repo.audit("auth.user_created", actor="system", entity_type="user",
                   entity_id="admin", after={"role": "admin", "startup_seeded": True})
        print("\n" + "=" * 68)
        print("  PUGMARK EDGE NODE · INITIAL ADMIN ACCOUNT CREATED")
        print("=" * 68)
        print(f"  Username:      admin")
        print(f"  Temp Password: {adm['temp_password']}")
        print(f"  Recovery Code: {adm['recovery_code']}")
        print("-" * 68)
        print("  Please log in with this temporary password. You will be prompted")
        print("  to set your permanent password on first login.")
        print("=" * 68 + "\n")

    # Clear any lockout on server startup so restarts allow recovery
    conn = repo.connect()
    conn.execute(
        "UPDATE users SET locked_until=NULL, failed_login_attempts=0 "
        "WHERE locked_until IS NOT NULL OR failed_login_attempts > 0")
    conn.commit()

    from edge import jobs  # noqa: PLC0415
    reaped = jobs.reap_stale()
    if reaped:
        repo.audit("startup.jobs_reaped", after={"count": reaped})

    threads = getattr(config.CONFIG.triage, "torch_threads", 0)
    if threads:
        try:
            import torch  # noqa: PLC0415
            torch.set_num_threads(int(threads))
        except ImportError:
            pass


def _generalise(lat: float | None, lon: float | None, role: str) -> tuple:
    if lat is None or lon is None:
        return lat, lon
    if role not in config.CONFIG.privacy.generalise_coords_for_roles:
        return lat, lon
    step = config.CONFIG.privacy.grid_cell_km / 111.0
    return round(lat / step) * step, round(lon / step) * step


def current_user(request: Request) -> dict:
    user = request.state.user
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


def require_role(*roles: str):
    def _check(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "insufficient permission")
        return user
    return _check


_DUMMY_HASH = auth.hash_secret("no such account, this hash is never a match")
_LOGIN_FAIL = "Invalid username or password."
_RECOVERY_FAIL = "Invalid username or recovery code."

from edge.routes_scale import register as _register_scale_routes  # noqa: E402
_register_scale_routes(app)


# ── meta ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "app_version": config.APP_VERSION,
        "schema_version": repo.schema_version(),
        "offline": True,
    }


@app.get("/api/config")
def get_config() -> dict:
    return config.CONFIG.to_dict()


# ── auth ────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(response: Response, username: str = Body(...), password: str = Body(...)):
    """Validates user credentials, enforcing lockout thresholds with structured
    attempt feedback while preserving timing indistinguishability for unknown accounts."""
    max_attempts = config.CONFIG.auth.failed_attempts_before_lock
    user = repo.user_by_username(username)
    if not user:
        auth.verify_secret(password, _DUMMY_HASH)
        return JSONResponse(
            status_code=401,
            content={
                "detail": _LOGIN_FAIL,
                "attempts_remaining": None,
                "max_attempts": max_attempts,
                "locked": False,
            },
        )
    if user["disabled"]:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Account is disabled. Please contact an administrator.",
                "attempts_remaining": 0,
                "max_attempts": max_attempts,
                "locked": False,
                "disabled": True,
            },
        )
    if user["locked_until"] and user["locked_until"] > repo.now():
        locked_until_dt = datetime.fromisoformat(user["locked_until"])
        remaining_sec = max(1, int((locked_until_dt - datetime.now(timezone.utc)).total_seconds()))
        return JSONResponse(
            status_code=401,
            content={
                "detail": f"Account is temporarily locked. Try again in {remaining_sec}s, or reset via recovery code.",
                "attempts_remaining": 0,
                "max_attempts": max_attempts,
                "locked": True,
                "locked_until": user["locked_until"],
                "lockout_seconds": remaining_sec,
            },
        )
    if not auth.verify_secret(password, user["pwd_hash"]):
        fail_res = repo.record_login_failure(username)
        attempts = fail_res["attempts"]
        locked_until = fail_res["locked_until"]
        attempts_remaining = max(0, max_attempts - attempts)
        if locked_until:
            locked_until_dt = datetime.fromisoformat(locked_until)
            remaining_sec = max(1, int((locked_until_dt - datetime.now(timezone.utc)).total_seconds()))
            return JSONResponse(
                status_code=401,
                content={
                    "detail": f"Account locked due to {attempts} failed attempts. Try again in {remaining_sec}s, or reset via recovery code.",
                    "attempts_remaining": 0,
                    "max_attempts": max_attempts,
                    "locked": True,
                    "locked_until": locked_until,
                    "lockout_seconds": remaining_sec,
                },
            )
        return JSONResponse(
            status_code=401,
            content={
                "detail": _LOGIN_FAIL,
                "attempts_remaining": attempts_remaining,
                "max_attempts": max_attempts,
                "locked": False,
            },
        )

    repo.record_login_success(username)

    # Transparent Argon2id parameter upgrade. auth.needs_rehash() existed
    # with a docstring telling the caller to do exactly this and was never
    # called from anywhere -- so if the hashing parameters were ever
    # hardened (more memory or time cost), every existing account would
    # have kept its weaker hash forever, and the upgrade would only have
    # applied to accounts created afterwards. The password is in hand and
    # already verified at this exact point; it is the only moment a
    # re-hash is possible without asking the operator for anything.
    try:
        if auth.needs_rehash(user["pwd_hash"]):
            repo.upgrade_password_hash(username, auth.hash_secret(password))
    except Exception:
        # A failed upgrade must never cost a valid user their login -- the
        # existing hash still verified, so the session below is legitimate.
        pass

    token = auth.generate_session_token()
    repo.create_session(username, user["role"], auth.hash_token(token), user_agent=None)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=False, max_age=config.CONFIG.auth.session_absolute_hours * 3600, path="/")
    return {
        "username": username, "role": user["role"],
        "display_name": user["display_name"],
        "must_change_password": bool(user["must_change_password"]),
    }


@app.post("/api/auth/logout")
def logout(response: Response, user: dict = Depends(current_user)) -> dict:
    repo.revoke_session(user["token_hash"], actor=user["username"])
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(current_user)) -> dict:
    return {
        "username": user["username"], "role": user["role"],
        "display_name": user["display_name"],
        "must_change_password": bool(user["must_change_password"]),
    }


@app.post("/api/auth/change-password")
def change_password(
    response: Response,
    current_password: str = Body(...),
    new_password: str = Body(...),
    user: dict = Depends(current_user),
) -> dict:
    row = repo.user_by_username(user["username"])
    if not row or not auth.verify_secret(current_password, row["pwd_hash"]):
        raise HTTPException(401, "Current password is incorrect.")
    weak = auth.is_weak_password(new_password, user["username"])
    if weak:
        raise HTTPException(400, weak)
    repo.set_password(user["username"], auth.hash_secret(new_password),
                       must_change=False, actor=user["username"])
    token = auth.generate_session_token()
    repo.create_session(user["username"], row["role"], auth.hash_token(token), user_agent=None)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=False, max_age=config.CONFIG.auth.session_absolute_hours * 3600, path="/")
    return {
        "ok": True,
        "username": user["username"],
        "role": row["role"],
        "display_name": row["display_name"],
        "relogin_required": False,
    }


@app.post("/api/auth/forgot-password")
def forgot_password(
    username: str = Body(...),
    recovery_code: str = Body(...),
    new_password: str = Body(...),
) -> dict:
    user = repo.user_by_username(username)
    if not user or not user["recovery_code_hash"]:
        auth.verify_secret(recovery_code, _DUMMY_HASH)
        raise HTTPException(401, _RECOVERY_FAIL)
    if not auth.verify_secret(auth.normalise_recovery_code(recovery_code),
                               user["recovery_code_hash"]):
        repo.record_login_failure(username)
        raise HTTPException(401, _RECOVERY_FAIL)

    weak = auth.is_weak_password(new_password, username)
    if weak:
        raise HTTPException(400, weak)

    repo.record_login_success(username)
    repo.set_password(username, auth.hash_secret(new_password),
                       must_change=False, actor=username)
    new_code = auth.generate_recovery_code()
    repo.set_recovery_code(username, auth.hash_secret(auth.normalise_recovery_code(new_code)), actor=username)
    return {"ok": True, "recovery_code": new_code}


@app.get("/api/auth/users")
def list_auth_users(user: dict = Depends(require_role("admin"))) -> list[dict]:
    return repo.list_users()


@app.post("/api/auth/users")
def create_auth_user(
    username: str = Body(...),
    display_name: str | None = Body(None),
    role: str = Body(...),
    user: dict = Depends(require_role("admin")),
) -> dict:
    if repo.user_by_username(username):
        raise HTTPException(409, "That username already exists.")
    if role not in config.CONFIG.auth.roles:
        raise HTTPException(400, f"Unknown role. Must be one of: {', '.join(config.CONFIG.auth.roles)}")
    temp_password = auth.generate_temp_password()
    recovery_code = auth.generate_recovery_code()
    repo.create_user(username, display_name, role, auth.hash_secret(temp_password),
                      actor=user["username"])
    repo.set_recovery_code(username, auth.hash_secret(auth.normalise_recovery_code(recovery_code)), actor=user["username"])
    return {"username": username, "temp_password": temp_password, "recovery_code": recovery_code}


@app.post("/api/auth/users/{username}/disable")
def disable_auth_user(username: str, user: dict = Depends(require_role("admin"))) -> dict:
    if username == user["username"]:
        raise HTTPException(400, "Cannot disable your own account.")
    target = repo.user_by_username(username)
    if not target:
        raise HTTPException(404, "No such user.")
    repo.disable_user(username, actor=user["username"])
    return {"ok": True}


@app.post("/api/auth/users/{username}/enable")
def enable_auth_user(username: str, user: dict = Depends(require_role("admin"))) -> dict:
    target = repo.user_by_username(username)
    if not target:
        raise HTTPException(404, "No such user.")
    repo.enable_user(username, actor=user["username"])
    return {"ok": True}


@app.delete("/api/auth/users/{username}")
@app.post("/api/auth/users/{username}/delete")
def delete_auth_user(username: str, user: dict = Depends(require_role("admin"))) -> dict:
    if username == user["username"]:
        raise HTTPException(400, "Cannot delete your own account.")
    target = repo.user_by_username(username)
    if not target:
        raise HTTPException(404, "No such user.")
    repo.delete_user(username, actor=user["username"])
    return {"ok": True}


@app.post("/api/auth/users/{username}/reset-credentials")
def reset_auth_user_credentials(username: str, user: dict = Depends(require_role("admin"))) -> dict:
    target = repo.user_by_username(username)
    if not target:
        raise HTTPException(404, "No such user.")
    result = repo.reset_user_credentials(username, actor=user["username"])
    return result


@app.get("/api/reserves")
def get_reserves(user: dict = Depends(current_user)) -> list[dict]:
    return repo.reserves()


# ── dev / demo data ─────────────────────────────────────────────────────

_SEED_MODULES = {
    "bulk": ["tools.seed_bulk"],
    "demo": ["tools.seed_demo", "--reset"],
    "blank": ["tools.reset_blank"],
}


@app.post("/api/dev/seed")
def dev_seed(payload: dict = Body(...), user: dict = Depends(require_role(*config.PERMISSIONS["dev_seed"]))) -> dict:
    which = payload.get("which")
    args = _SEED_MODULES.get(which)
    if not args:
        raise HTTPException(400, "which must be 'bulk', 'demo', or 'blank'")

    # Loading seed data replaces the whole database, and would otherwise
    # destroy whatever the operator fed in by hand (real test uploads,
    # confirmed/merged tigers). Snapshot the live state first -- but only
    # on an actual live->seed transition, not on every seed load, or
    # loading a second seed on top of the first would overwrite the real
    # live snapshot with a snapshot of seed data instead.
    #
    # ...and only if there is anything there worth keeping. This second
    # guard is not hypothetical: 'blank' deliberately takes no backup (it
    # is the permanent option) but does set the mode back to 'live', so a
    # blank -> demo sequence -- exactly what tests/live/test_routes.py does
    # at the end of every run -- saw mode='live', treated the freshly
    # emptied database as precious, and wrote it over a real snapshot,
    # destroying a day of catalogued tigers. An empty database never
    # outranks an existing backup.
    backed_up = False
    repo.close_all()
    try:
        if which in ("bulk", "demo") and repo_ext.data_mode() == "live" \
                and repo_ext.live_data_is_worth_keeping():
            repo_ext.backup(repo_ext._live_backup_path())
            backed_up = True
        result = subprocess.run(
            [sys.executable, "-m", *args], cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=300)
    finally:
        repo.close_all()
    if result.returncode != 0:
        raise HTTPException(500, f"seed failed:\n{result.stderr[-3000:]}")
    repo_ext.set_data_mode("seeded" if which in ("bulk", "demo") else "live")
    repo.audit("dev.seed", actor=user["username"],
               after={"which": which, "live_data_backed_up": backed_up})
    return {"ok": True, "which": which, "output": result.stdout.strip(),
            "live_data_backed_up": backed_up}


@app.post("/api/dev/restore-live")
def dev_restore_live(user: dict = Depends(require_role(*config.PERMISSIONS["dev_seed"]))) -> dict:
    """Swap seeded demo data back out for whatever live data was snapshotted
    the last time /api/dev/seed backed it up. Never touches the snapshot
    file itself -- restoring is repeatable within a session (load a demo,
    go back to live, load a different demo, go back to live again) without
    losing the original live data partway through."""
    backup_path = repo_ext._live_backup_path()
    if not backup_path.is_file():
        raise HTTPException(404, "no saved live data to restore -- nothing has been backed up yet")
    repo.close_all()
    try:
        # preserve_accounts=True: this swaps the DATA back, not the identities.
        # Without it the operator was signed out and their password reverted to
        # whatever it had been when the snapshot was taken -- a button labelled
        # as a view locked an admin out of their own machine.
        outcome = repo_ext.restore(backup_path)
    finally:
        repo.close_all()
    repo_ext.set_data_mode("live")
    repo.audit("dev.restore_live", actor=user["username"],
               after={"accounts_preserved": outcome.get("accounts_preserved"),
                      "users_kept": outcome.get("users_kept")})
    return {"ok": True, **outcome}


@app.get("/api/dev/data-mode")
def dev_data_mode(user: dict = Depends(current_user)) -> dict:
    return {"mode": repo_ext.data_mode(), "live_backup_available": repo_ext._live_backup_path().is_file()}


# ── runs ────────────────────────────────────────────────────────────────

@app.get("/api/runs")
def get_runs(reserve_id: str | None = None, user: dict = Depends(current_user)) -> list[dict]:
    return repo.runs(reserve_id)


@app.post("/api/fs/native-browse")
def native_browse_folder(user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return {"available": False, "path": None}
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Select a camera-trap folder")
        root.destroy()
    except tkinter.TclError:
        return {"available": False, "path": None}
    return {"available": True, "path": path or None}


@app.get("/api/fs/browse")
def browse_folders(path: str | None = None, user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    if not path:
        if os.name == "nt":
            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            return {"path": None, "parent": None,
                    "entries": [{"name": d, "path": d} for d in drives]}
        path = "/"
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"not a directory: {path}")
    try:
        children = sorted((e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")),
                           key=lambda e: e.name.lower())
    except PermissionError:
        children = []
    parent = None if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent,
            "entries": [{"name": e.name, "path": str(e)} for e in children]}


@app.post("/api/runs")
def start_run(payload: dict = Body(...), user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    reserve_id, root_path = payload.get("reserve_id"), payload.get("root_path")
    if not reserve_id or not root_path:
        raise HTTPException(400, "reserve_id and root_path required")
    from edge.pipeline import ingest
    try:
        result = ingest.preflight_ingest(reserve_id, root_path, payload.get("cycle_label"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**preflight(result["run_id"], user=user), **result}


@app.post("/api/runs/{run_id}/confirm")
def confirm_run(run_id: str, payload: dict = Body(default={}), user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    from edge.pipeline import ingest
    try:
        return ingest.confirm_ingest(run_id, payload.get("station_assignments"),
                                      payload.get("skip_folders"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(current_user)) -> dict:
    r = repo.run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    r["counts"] = repo.run_counts(run_id)
    r["timestamp_sources"] = repo.timestamp_sources(run_id)
    r["flags"] = repo.run_flags(run_id)
    r["alerts"] = repo.alert_counts(run_id)
    return r


@app.get("/api/runs/{run_id}/preflight")
def preflight(run_id: str, user: dict = Depends(current_user)) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return {
        "run_id": run_id,
        "counts": repo.run_counts(run_id),
        "timestamp_sources": repo.timestamp_sources(run_id),
        "flags": repo.run_flags(run_id),
    }


# ── triage ──────────────────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/triage")
def triage(run_id: str, user: dict = Depends(current_user)) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return {
        "summary": repo.quarantine_summary(run_id),
        "counts": repo.run_counts(run_id),
        "sample": repo.quarantine_sample(run_id),
    }


@app.post("/api/runs/{run_id}/triage/run")
def run_triage(run_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    from edge.pipeline import triage as triage_pipeline
    try:
        return triage_pipeline.run_triage(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/runs/{run_id}/quarantine/restore")
def restore(run_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    from edge.pipeline import triage as triage_pipeline
    return {"restored": triage_pipeline.restore(run_id, user["username"])}


@app.get("/api/runs/{run_id}/images")
def get_run_images(run_id: str, status: str, user: dict = Depends(current_user)) -> list[dict]:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return repo.images_by_status(run_id, status)


@app.post("/api/runs/{run_id}/images/{image_id}/identify")
def identify_run_image(run_id: str, image_id: str,
                        user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    run = repo.run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    image = repo.image(image_id)
    if not image or image["run_id"] != run_id:
        raise HTTPException(404, "image not found in this run")
    from edge.pipeline import identify_upload  # noqa: PLC0415
    try:
        return identify_upload.process_upload(
            image["orig_path"], run["reserve_id"], image["station_id"], user["username"])
    except FileNotFoundError:
        raise HTTPException(400, "Stage 3 weights are not present in edge/models.")


# ── individuals ─────────────────────────────────────────────────────────

@app.get("/api/individuals")
def get_individuals(reserve_id: str, user: dict = Depends(current_user)) -> list[dict]:
    return repo.individuals(reserve_id)


@app.get("/api/individuals/{ind_id}")
def get_individual(ind_id: str, user: dict = Depends(current_user)) -> dict:
    ind = repo.individual(ind_id)
    if not ind:
        raise HTTPException(404, "individual not found")
    role = user["role"]
    actor = user["username"]
    caps = repo.individual_captures(ind_id)
    for c in caps:
        c["lat"], c["lon"] = _generalise(c.get("lat"), c.get("lon"), role)
    if role in config.CONFIG.privacy.generalise_coords_for_roles:
        repo.audit("location.read.generalised", actor=actor,
                   entity_type="individual", entity_id=ind_id)
    else:
        repo.audit("location.read.precise", actor=actor,
                   entity_type="individual", entity_id=ind_id)
    ind["captures"] = caps
    ind["sides_seen"] = sorted({c["side"] for c in caps if c["side"] in ("L", "R")})
    return ind


@app.get("/api/individuals/{ind_id}/thumbnail")
def get_individual_thumbnail(ind_id: str, user: dict = Depends(current_user)) -> FileResponse:
    path = repo.latest_crop_path(ind_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "no real crop on file for this individual")
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/individuals/{ind_id}/promote")
def promote(ind_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["individual_promote"]))) -> dict:
    if not repo.promote_individual(ind_id, user["username"]):
        raise HTTPException(400, "not provisional, or not found")
    return {"ok": True}


@app.get("/api/crops/{crop_id}/image")
def get_crop_image(crop_id: str, user: dict = Depends(current_user)) -> FileResponse:
    path = repo.crop_path(crop_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "no real crop image on file for this crop")
    p = Path(path)
    media_type = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(p, media_type=media_type)


@app.get("/api/crops/{crop_id}/source")
def get_crop_source(crop_id: str, user: dict = Depends(current_user)):
    """The full captured frame a crop was cut from.

    A redirect to /api/images/{image_id}/file rather than a second file
    server: that route carries the refusal for frames the triage cascade
    routed to persons_restricted, and a parallel implementation here would
    be a second place for that guard to be forgotten. One privacy check,
    one route serving original frames.
    """
    image_id = repo.source_image_id_for_crop(crop_id)
    if not image_id:
        raise HTTPException(404, "no source frame recorded for this crop")
    return RedirectResponse(f"/api/images/{image_id}/file", status_code=307)


@app.get("/api/individuals/{ind_id}/source")
def get_individual_source(ind_id: str, user: dict = Depends(current_user)):
    """The full frame behind the photo that represents this individual."""
    image_id = repo.source_image_id_for_individual(ind_id)
    if not image_id:
        raise HTTPException(404, "no source frame recorded for this individual")
    return RedirectResponse(f"/api/images/{image_id}/file", status_code=307)


@app.get("/api/quarantine/{image_id}/image")
def get_quarantined_frame(image_id: str, user: dict = Depends(current_user)) -> FileResponse:
    """Serve a frame that triage moved into quarantine.

    Blank Frames exists so a human can check what the machine discarded. It
    could not show a single one of them: quarantine moves the file, and the
    only frame-serving route reads images.orig_path, which by then points at
    a file that is no longer there. Every preview 404'd.

    The person-frame refusal from /api/images/{id}/file applies here too. A
    frame classified as containing a person should not be reachable through a
    second door just because it also happens to sit in quarantine.
    """
    row = repo_ext.quarantine_file(image_id)
    if not row:
        raise HTTPException(404, "not a quarantined frame")
    if (row.get("status") or "").lower() == "person":
        repo.audit("person_image.access_refused", actor=user["username"],
                   entity_type="image", entity_id=image_id,
                   after={"role": user["role"], "reason": "restricted person frame"})
        raise HTTPException(403, "This frame was withheld because it contains a person.")

    raw = row.get("quarantine_path")
    if not raw:
        raise HTTPException(404, "no quarantine path recorded for this frame")
    path = Path(raw)
    if not path.is_absolute():
        # stored relative to the install root, e.g. "quarantine/<run>/<id>.JPG"
        for base in (config.DATA_DIR, config.DATA_DIR.parent):
            candidate = (base / raw).resolve()
            if candidate.is_file():
                path = candidate
                break
    if not path.is_file():
        raise HTTPException(
            404, "the quarantined file is not on this machine -- the database row "
                 "exists but the image was never written here (seeded data), or the "
                 "quarantine folder has been moved")
    media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(path, media_type=media)


@app.get("/api/images/{image_id}/file")
def get_image_file(image_id: str, user: dict = Depends(current_user)) -> FileResponse:
    """Serve an original captured frame.

    Refuses frames the triage cascade routed to persons_restricted. That
    check is the whole reason this reads `status`: without it, a frame
    containing a person was blurred for the UI, filed as restricted, and
    then still handed over in full resolution to anyone with any login --
    including the `field` and `analyst` roles that BLUEPRINT.md section 10
    says must never see person images at all. The blurred derivative
    exists on disk and is what any legitimate person-presence feature
    should use; the original is not reachable through this route by
    anybody, which is the conservative reading of the problem statement's
    "privacy safeguards for images capturing humans".
    """
    row = repo_ext.image_for_serving(image_id)
    if not row:
        raise HTTPException(404, "image file not found on disk")

    if (row.get("status") or "").lower() == "person":
        # Logged on the REFUSED path too -- an attempt to reach a restricted
        # frame is itself the auditable event.
        repo_ext.note_person_image_access(image_id)
        repo.audit("person_image.access_refused", actor=user["username"],
                   entity_type="image", entity_id=image_id,
                   after={"role": user["role"], "reason": "restricted person frame"})
        raise HTTPException(
            403, "This frame was withheld because it contains a person. "
                 "Human presence is reported as counts by station and date, "
                 "not as browsable images.")

    if not row.get("orig_path") or not Path(row["orig_path"]).exists():
        raise HTTPException(404, "image file not found on disk")

    # Blueprint section 10: audit reads, not just writes.
    repo.audit("image.read", actor=user["username"], entity_type="image",
               entity_id=image_id, after={"role": user["role"]})
    p = Path(row["orig_path"])
    media_type = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(p, media_type=media_type)



# ── review ──────────────────────────────────────────────────────────────

@app.get("/api/review")
def get_review(limit: int = 50, user: dict = Depends(current_user)) -> dict:
    return {"open": repo.review_count(), "items": repo.review_open(limit)}


@app.post("/api/review/{queue_id}/decide")
def decide(queue_id: str, payload: dict = Body(...), user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
    new_individual = bool(payload.get("new_individual"))
    actor = user["username"]
    ind_id = payload.get("ind_id")

    q = repo._one(repo.connect().execute(
        "SELECT rq.*, c.rect_ok, im.reserve_id FROM review_queue rq"
        " LEFT JOIN flank_crops c ON c.crop_id = rq.crop_id"
        " LEFT JOIN detections  d ON d.det_id = c.det_id"
        " LEFT JOIN images      im ON im.image_id = d.image_id"
        " WHERE rq.queue_id=?", (queue_id,)))
    if not q:
        raise HTTPException(404, "queue item not found")
    if not q.get("rect_ok"):
        # No embedding exists for this crop -- species or physical side was
        # never confirmed, so there is no match to confirm and no biometric
        # signature to enrol as a new individual. Checked before creating a
        # provisional individual below, not after, so a refusal here never
        # leaves an orphaned, unassigned catalogue row behind it.
        raise HTTPException(400, "this crop has no biometric embedding -- species or "
                             "physical side was never confirmed for it, so there is no "
                             "match to confirm or new individual to enrol. Use "
                             "POST /api/review/{queue_id}/dismiss instead.")

    if new_individual:
        res_id = q.get("reserve_id") or "PENCH-MH"
        ind_id = repo.create_provisional_individual(res_id, actor)
    elif not ind_id:
        raise HTTPException(400, "ind_id required")

    try:
        result = repo.review_decide(queue_id, ind_id, actor, new_individual)
    except KeyError:
        raise HTTPException(404, "queue item not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    result["ind_id"] = ind_id

    from edge.pipeline import postprocess  # noqa: PLC0415
    if q.get("crop_id"):
        try:
            result["recomputed"] = postprocess.after_review_decision(q["crop_id"], actor)
        except Exception:
            pass
    return result


@app.post("/api/review/{queue_id}/dismiss")
def dismiss_review(queue_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
    """Close a review item that has no biometric decision to make -- see
    repo.review_dismiss()'s docstring. Creates no assignment and enrols
    nothing; the underlying image keeps whatever terminal status Stage 3
    already gave it."""
    try:
        return repo.review_dismiss(queue_id, user["username"])
    except KeyError:
        raise HTTPException(404, "queue item not found")


@app.post("/api/review/{queue_id}/confirm-side")
def confirm_side(queue_id: str, payload: dict = Body(...),
                 user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
    """A human looked at the real photo and can tell which flank it is, for
    a crop the side classifier could not resolve. Only valid when species
    was already confirmed tiger and no side is already known -- a crop
    refused for unknown species, or one where the side WAS determined but
    the pose model still failed, has nothing this route can complete."""
    side = payload.get("side")
    if side not in ("L", "R"):
        raise HTTPException(400, "side must be 'L' or 'R'")
    q = repo._one(repo.connect().execute(
        "SELECT rq.crop_id, c.rect_ok, c.side crop_side, d.species FROM review_queue rq"
        " LEFT JOIN flank_crops c ON c.crop_id = rq.crop_id"
        " LEFT JOIN detections  d ON d.det_id = c.det_id"
        " WHERE rq.queue_id=?", (queue_id,)))
    if not q:
        raise HTTPException(404, "queue item not found")
    if q.get("rect_ok") or q.get("species") != "tiger" or q.get("crop_side") in ("L", "R"):
        raise HTTPException(400, "this item is not eligible for a side correction")
    from edge.pipeline import identify_upload  # noqa: PLC0415
    try:
        return identify_upload.complete_side_unknown(q["crop_id"], side, user["username"])
    except (KeyError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))



# ── identify: a raw photograph in, a catalogue/review/audit entry out ────

@app.post("/api/identify/upload")
async def identify_upload_route(
    file: UploadFile = File(...),
    reserve_id: str = Form(...),
    station_id: str | None = Form(None),
    user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"])),
) -> dict:
    actor = user["username"]
    if not repo.reserve(reserve_id):
        raise HTTPException(404, "reserve not found")

    raw_name = Path(file.filename or "upload.jpg").name
    ext = Path(raw_name).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    clean_name = f"{Path(raw_name).stem[:50]}{ext}"

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(413, "upload exceeds maximum allowed size (50 MB)")

    dest = config.UPLOADS_DIR / f"{repo.new_id('up_')}_{clean_name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    from edge.pipeline import identify_upload  # noqa: PLC0415, F811
    try:
        return identify_upload.process_upload(str(dest), reserve_id, station_id, actor)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))


# ── occupancy and alerts ────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/occupancy")
def get_occupancy(run_id: str, user: dict = Depends(current_user)) -> list[dict]:
    role = user["role"]
    actor = user["username"]
    rows = repo.occupancy(run_id)
    generalised = role in config.CONFIG.privacy.generalise_coords_for_roles
    for r in rows:
        r["centroid_lat"], r["centroid_lon"] = _generalise(
            r.get("centroid_lat"), r.get("centroid_lon"), role)
        if generalised:
            r["hull_wkt"] = None
    if rows:
        repo.audit(f"location.read.{'generalised' if generalised else 'precise'}",
                   actor=actor, entity_type="run", entity_id=run_id, note="occupancy")
    return rows


@app.get("/api/runs/{run_id}/occupancy/export.geojson")
def export_occupancy_geojson(run_id: str, user: dict = Depends(current_user)) -> Response:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    role = user["role"]
    actor = user["username"]
    generalised = role in config.CONFIG.privacy.generalise_coords_for_roles
    rows = repo.occupancy(run_id)
    repo.audit(f"location.read.{'generalised' if generalised else 'precise'}",
               actor=actor, entity_type="run", entity_id=run_id, note="occupancy.export.geojson")
    body = geojson_export.occupancy_feature_collection(rows, include_geometry=not generalised)
    return Response(json.dumps(body, indent=2), media_type="application/geo+json",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}_occupancy.geojson"'})


@app.get("/api/runs/{run_id}/occupancy/export.csv")
def export_occupancy_csv(run_id: str, user: dict = Depends(current_user)) -> Response:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    role = user["role"]
    actor = user["username"]
    generalised = role in config.CONFIG.privacy.generalise_coords_for_roles
    rows = repo.occupancy(run_id)
    repo.audit(f"location.read.{'generalised' if generalised else 'precise'}",
               actor=actor, entity_type="run", entity_id=run_id, note="occupancy.export.csv")
    body = csv_export.occupancy_csv(rows, include_locations=not generalised)
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}_occupancy.csv"'})


@app.get("/api/stations/export.geojson")
def export_stations_geojson(reserve_id: str, user: dict = Depends(current_user)) -> Response:
    role = user["role"]
    rows = repo.stations(reserve_id)
    for r in rows:
        r["lat"], r["lon"] = _generalise(r["lat"], r["lon"], role)
    body = geojson_export.stations_feature_collection(rows)
    return Response(json.dumps(body, indent=2), media_type="application/geo+json",
                    headers={"Content-Disposition": f'attachment; filename="{reserve_id}_stations.geojson"'})


@app.get("/api/runs/{run_id}/export/camtrapdp")
def export_camtrapdp(run_id: str, user: dict = Depends(current_user)) -> Response:
    run = repo.run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    reserve = repo.reserve(run["reserve_id"])
    role = user["role"]
    actor = user["username"]
    generalised = role in config.CONFIG.privacy.generalise_coords_for_roles
    repo.audit(f"location.read.{'generalised' if generalised else 'precise'}",
               actor=actor, entity_type="run", entity_id=run_id, note="camtrapdp.export")
    files = camtrapdp.build_package(
        reserve, run,
        repo.station_activity_for_reserve(run["reserve_id"]),
        repo.images_for_run(run_id),
        repo.detections_for_run(run_id),
        include_locations=not generalised,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}_camtrapdp.zip"'})


@app.get("/api/runs/{run_id}/alerts")
def get_alerts(run_id: str, suppressed: bool = False, user: dict = Depends(current_user)) -> dict:
    return {
        "counts": repo.alert_counts(run_id),
        "items": repo.alerts(run_id, suppressed),
    }


@app.post("/api/alerts/{alert_id}/acknowledge")
def ack(alert_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["alert_ack"]))) -> dict:
    if not repo.acknowledge_alert(alert_id, user["username"]):
        raise HTTPException(400, "already acknowledged, or not found")
    return {"ok": True}


# ── audit and ops ───────────────────────────────────────────────────────

@app.get("/api/audit")
def get_audit(limit: int = 200, q: str | None = None, user: dict = Depends(require_role(*config.PERMISSIONS["audit_read"]))) -> list[dict]:
    return repo.audit_tail(limit, q)


@app.get("/api/ops")
def ops(reserve_id: str, user: dict = Depends(current_user)) -> dict:
    return {
        "drift": repo.drift_indicators(reserve_id),
        "schema_version": repo.schema_version(),
        "app_version": config.APP_VERSION,
    }





# ── sync ────────────────────────────────────────────────────────────────

@app.get("/api/sync/status")
def sync_status(reserve_id: str | None = None, user: dict = Depends(current_user)) -> dict:
    sync_secret = config.get_sync_secret()
    secret_set = bool(sync_secret)
    return {
        "node_id": repo.node_id(),
        "bundle_sync_enabled": secret_set,
        "bundle_sync_reason": None if secret_set else
            "no PUGMARK_SYNC_SECRET configured on this node",
        "pending_rows": repo.pending_sync_count(reserve_id) if reserve_id else None,
        "central_tier_enabled": False,
        "central_tier_reason": "central tier not configured on this node -- "
            "designed (blueprint §3), not built",
    }


@app.get("/api/sync/bundle")
def get_bundle(reserve_id: str, since_lamport: int = 0, user: dict = Depends(require_role(*config.PERMISSIONS["sync_manage"]))) -> Response:
    if not repo.reserve(reserve_id):
        raise HTTPException(404, "reserve not found")
    try:
        bundle = bundle_sync.build_bundle(reserve_id, since_lamport, config.get_sync_secret())
    except ValueError as e:
        raise HTTPException(400, str(e))
    row_count = sum(len(v) for v in bundle["rows"].values())
    repo.audit("sync.bundle.build", actor=user["username"], entity_type="reserve",
               entity_id=reserve_id, after={"rows": row_count, "up_to_lamport": bundle["up_to_lamport"]})
    fname = f"{reserve_id}_{bundle['up_to_lamport']}.pugmark-bundle.json"
    return Response(json.dumps(bundle, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/sync/bundle/apply")
async def apply_bundle_route(file: UploadFile = File(...), user: dict = Depends(require_role(*config.PERMISSIONS["sync_manage"]))) -> dict:
    try:
        bundle = json.loads(await file.read())
    except json.JSONDecodeError:
        raise HTTPException(400, "not a valid bundle file")
    try:
        stats = bundle_sync.apply_bundle(bundle, config.get_sync_secret())
    except ValueError as e:
        raise HTTPException(400, str(e))
    repo.audit("sync.bundle.apply", actor=user["username"], entity_type="reserve",
               entity_id=stats["reserve_id"], after=stats)
    return stats


@app.get("/api/sync/key")
def get_sync_key_info(user: dict = Depends(require_role(*config.PERMISSIONS["sync_manage"]))) -> dict:
    sec = config.get_sync_secret()
    import hashlib
    masked = f"{sec[:4]}...{sec[-4:]}" if len(sec) >= 8 else "***"
    fingerprint = hashlib.sha256(sec.encode()).hexdigest()[:12]
    return {
        "sync_secret_masked": masked,
        "sync_secret": sec,
        "fingerprint": fingerprint,
        "key_length": len(sec),
        "node_id": repo.node_id(),
        "file_path": str(config.DATA_DIR / "sync_secret.txt"),
    }


@app.post("/api/sync/key")
def update_sync_key(payload: dict = Body(default={}), user: dict = Depends(require_role(*config.PERMISSIONS["user_manage"]))) -> dict:
    new_sec = payload.get("new_secret", "")
    saved = config.set_sync_secret(new_sec)
    repo.audit("sync.key.rotate", actor=user["username"], entity_type="system",
               entity_id="sync_secret", note="rotated or imported sync key")
    return {"ok": True, "sync_secret": saved}


@app.get("/api/sync/key/download")
def download_sync_key_file(user: dict = Depends(require_role(*config.PERMISSIONS["sync_manage"]))) -> Response:
    sec = config.get_sync_secret()
    return Response(sec, media_type="text/plain",
                    headers={"Content-Disposition": 'attachment; filename="sync_secret.txt"'})


# ── static UI ───────────────────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")


@app.exception_handler(404)
def not_found(_request, exc):  # noqa: ANN001
    return JSONResponse({"error": getattr(exc, "detail", "not found")}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 68)
    print("  PUGMARK · Autonomous Edge Intelligence Node")
    print("  Starting server on http://127.0.0.1:7860")
    print("=" * 68 + "\n")
    uvicorn.run("edge.app:app", host="127.0.0.1", port=7860, reload=False)

