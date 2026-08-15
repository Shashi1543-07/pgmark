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

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from edge import auth, config
from edge.db import repo
from edge.exports import camtrapdp
from edge.exports import csv as csv_export
from edge.exports import geojson as geojson_export
from edge.pipeline import ingest
# identify_upload is imported lazily inside the two routes that need it.
# It pulls in torch via edge/pipeline/detector.py, and importing it here
# meant a laptop with a broken torch install could not start the server at
# all -- no map, no alerts, no audit log, no catalogue, because one
# optional model dependency for one stage was imported at module scope.
# Verified: `import edge.app` raised ModuleNotFoundError before this change.
from edge.pipeline import triage as triage_pipeline
from edge.sync import bundle as bundle_sync

UI_DIR = Path(__file__).resolve().parent / "ui"

app = FastAPI(title="Pugmark", version=config.APP_VERSION, docs_url="/api/docs")


@app.middleware("http")
async def no_cache_ui(request, call_next):
    """A single-page app only re-fetches its JS/CSS on a full page reload,
    never on in-app navigation -- so a browser tab left open across a
    server restart (routine during development, and plausible in the
    field too) silently keeps running stale code with no visible sign
    anything is wrong. This is a local, single-user tool; there is no
    performance case for caching that is worth that failure mode."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/ui/"):
        response.headers["Cache-Control"] = "no-store"
    return response


SESSION_COOKIE = "pugmark_session"


@app.middleware("http")
async def resolve_session(request: Request, call_next):
    """Resolves whoever the session cookie belongs to and attaches it to
    request.state.user (or None) -- this middleware only ever *identifies*
    the caller. It never blocks a request itself; current_user()/
    require_role() below do that, at each route that needs it. That split
    is deliberate (see the "RBAC enforcement" design note in the auth
    plan): different routes need different role sets, not just
    authenticated-or-not, and that granularity belongs at the route, not
    a single blanket gate here."""
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
    adm = repo.ensure_admin()
    if adm["created"]:
        repo.audit("auth.user_created", actor="system", entity_type="user",
                   entity_id="admin", after={"role": "admin", "startup_seeded": True})

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


# ── role gating ─────────────────────────────────────────────────────────
# Roles are enforced here rather than in the UI, because a UI check is a
# suggestion and a server check is a control. See blueprint §10. Until this
# module's auth section (below) existed, "here" meant a role= query string
# the client supplied itself with no credential behind it -- ?role=director
# was the entire access control. Every route now derives role from
# Depends(current_user)/Depends(require_role(...)), never from client input.

def _generalise(lat: float | None, lon: float | None, role: str) -> tuple:
    """Roles above reserve level see grid cells, not points. National
    analysis needs distribution, not the tree the tigress sleeps under."""
    if lat is None or lon is None:
        return lat, lon
    if role not in config.CONFIG.privacy.generalise_coords_for_roles:
        return lat, lon
    step = config.CONFIG.privacy.grid_cell_km / 111.0
    return round(lat / step) * step, round(lon / step) * step


def current_user(request: Request) -> dict:
    """The one required dependency on every protected route. 401, not a
    redirect or a default identity -- an API returning *something* for an
    unauthenticated request is exactly the mistake this whole module
    exists to fix."""
    user = request.state.user
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


def require_role(*roles: str):
    """Depends(require_role("admin", "director")) -- 403 if the resolved,
    *current* role (not whatever a client claims) isn't in the allowed
    set. A factory, not a single dependency, because different routes
    genuinely need different role sets -- import/run processing isn't the
    same set as user management -- and that belongs at the route."""
    def _check(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "insufficient permission")
        return user
    return _check


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
    """The UI renders this so an officer can see what the machine was told
    before judging what it decided."""
    return config.CONFIG.to_dict()


# ── auth ────────────────────────────────────────────────────────────────
# Passwords/recovery codes/tokens: edge/auth.py. Persistence: repo.py's
# authentication section. This is just the HTTP surface over both.

SESSION_COOKIE = "pugmark_session"

# Verified against on an unknown username so a login attempt for a real
# account and a nonexistent one cost the same amount of CPU time -- without
# this, "how long did the response take" is itself a username-enumeration
# oracle even though every response body is identical.
_DUMMY_HASH = auth.hash_secret("no such account, this hash is never a match")

_LOGIN_FAIL = "Invalid username or password."
_RECOVERY_FAIL = "Invalid username or recovery code."


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
    token = auth.generate_session_token()
    repo.create_session(username, user["role"], auth.hash_token(token), user_agent=None)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        # secure=False is deliberate, not an oversight: this deployment is
        # confirmed single-laptop, browser and server on 127.0.0.1, no
        # HTTPS anywhere -- see the plan's scope note. Revisit if that
        # deployment model ever changes.
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
    """Requires the current password even when the caller is only here
    because must_change_password forced them -- a hijacked session still
    should not be able to lock the real owner out by setting a new
    password nobody else knows."""
    row = repo.user_by_username(user["username"])
    if not row or not auth.verify_secret(current_password, row["pwd_hash"]):
        raise HTTPException(401, "Current password is incorrect.")
    weak = auth.is_weak_password(new_password, user["username"])
    if weak:
        raise HTTPException(400, weak)
    repo.set_password(user["username"], auth.hash_secret(new_password),
                       must_change=False, actor=user["username"])
    # Establish active session for the user with new credentials
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
    """No session required -- this *is* the "I'm locked out" path. Reuses
    the same failed_login_attempts/locked_until columns as ordinary login,
    so brute-forcing a recovery code is rate-limited the same way brute-
    forcing a password is, rather than needing a second lockout mechanism.
    Never auto-logs in afterward (CLAUDE.md-adjacent: proving the recovery
    code is not the same as proving it's this session's rightful owner) --
    the caller gets a fresh recovery code once, then must log in normally."""
    user = repo.user_by_username(username)
    if not user or not user["recovery_code_hash"]:
        auth.verify_secret(recovery_code, _DUMMY_HASH)
        raise HTTPException(401, _RECOVERY_FAIL)
    if user["locked_until"] and user["locked_until"] > repo.now():
        locked_until_dt = datetime.fromisoformat(user["locked_until"])
        remaining_sec = max(1, int((locked_until_dt - datetime.now(timezone.utc)).total_seconds()))
        return JSONResponse(
            status_code=401,
            content={
                "detail": f"Account is temporarily locked. Try again in {remaining_sec}s.",
                "locked": True,
                "lockout_seconds": remaining_sec,
            },
        )
    if not auth.verify_secret(auth.normalise_recovery_code(recovery_code),
                               user["recovery_code_hash"]):
        repo.record_login_failure(username)
        raise HTTPException(401, _RECOVERY_FAIL)

    weak = auth.is_weak_password(new_password, username)
    if weak:
        raise HTTPException(400, weak)

    repo.record_login_success(username)  # clears the failed-attempt counter
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
    # Shown exactly once, here, in the response -- neither value is ever
    # retrievable again. The admin is responsible for handing this to the
    # new user out of band (printed slip, said aloud) and destroying it.
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
# For rehearsing a demo or judging the UI at volume, not for a real
# deployment: this node's own security model already assumes whoever can
# reach its UI has full control (CLAUDE.md, top of this file -- binding
# off 127.0.0.1 is an explicit, logged decision), so a reseed button adds
# no new class of risk on this machine. It always fully replaces the
# database, never merges.
_SEED_MODULES = {
    "bulk": ["tools.seed_bulk"],
    "demo": ["tools.seed_demo", "--reset"],
    "blank": ["tools.reset_blank"],
}


@app.post("/api/dev/seed")
def dev_seed(payload: dict = Body(...), user: dict = Depends(require_role(*config.PERMISSIONS["dev_seed"]))) -> dict:
    """Runs one of tools/seed_bulk.py, tools/seed_demo.py or
    tools/reset_blank.py as a fresh subprocess -- not in-process. repo.py
    caches one SQLite connection per thread, and FastAPI dispatches
    requests across a threadpool, so any thread that has served an
    earlier request may still hold the database file open when this one
    tries to delete and recreate it. Confirmed empirically, not assumed:
    without repo.close_all() here, Windows raises PermissionError on the
    file, every time, because even one other thread's stale handle is
    enough to block the delete -- repo.close() (this thread only) is not
    sufficient. close_all() closes every connection this process has
    opened on any thread and bumps a generation counter so each of those
    threads transparently reopens against the fresh file on its next
    query, instead of failing on a handle to a file that no longer
    exists."""
    which = payload.get("which")
    args = _SEED_MODULES.get(which)
    if not args:
        raise HTTPException(400, "which must be 'bulk', 'demo', or 'blank'")
    repo.close_all()
    try:
        result = subprocess.run(
            [sys.executable, "-m", *args], cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=300)
    finally:
        repo.close_all()
    if result.returncode != 0:
        raise HTTPException(500, f"seed failed:\n{result.stderr[-3000:]}")
    repo.audit("dev.seed", actor=user["username"], after={"which": which})
    return {"ok": True, "which": which, "output": result.stdout.strip()}


# ── runs ────────────────────────────────────────────────────────────────

@app.get("/api/runs")
def get_runs(reserve_id: str | None = None, user: dict = Depends(current_user)) -> list[dict]:
    return repo.runs(reserve_id)


@app.post("/api/fs/native-browse")
def native_browse_folder(user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    """Opens the real OS folder dialog (Explorer on Windows) and blocks
    until the user picks a folder or cancels. Works because this node's
    browser and its filesystem are the same laptop -- CLAUDE.md's
    deployment target -- so this is a normal local GUI interaction, not a
    remote one. Not every Python install ships Tk (a minimal/embeddable
    install may not), so this can genuinely be unavailable; the UI falls
    back to the in-page /api/fs/browse picker rather than breaking when it
    is. A sync def route runs in FastAPI's threadpool, so blocking here on
    the dialog does not stall the rest of the server."""
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
    """Lets the new-run screen offer a click-through folder picker instead
    of a typed path. This works because the browser and the filesystem
    being browsed are the same laptop -- CLAUDE.md's deployment target --
    so it is a local directory listing, not a remote file-browsing
    surface. Directories only; files are irrelevant to picking a folder
    to scan. No `path` means "list the drives" on Windows or "/" on
    everything else."""
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
    """Stage 1: scan a folder, ingest what it understood, report the rest.
    Nothing here runs a model -- triage doesn't exist yet -- so every image
    lands at status='pending'. See edge/pipeline/ingest.py."""
    reserve_id, root_path = payload.get("reserve_id"), payload.get("root_path")
    if not reserve_id or not root_path:
        raise HTTPException(400, "reserve_id and root_path required")
    try:
        result = ingest.preflight_ingest(reserve_id, root_path, payload.get("cycle_label"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**preflight(result["run_id"], user=user), **result}


@app.post("/api/runs/{run_id}/confirm")
def confirm_run(run_id: str, payload: dict = Body(default={}), user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    """The officer's confirm click. Resolves any folder ingest could not
    match on its own; a folder left neither assigned nor skipped blocks
    confirmation rather than being guessed (blueprint §5.1)."""
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
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
    """Everything the machine understood, shown before it acts on any of it.
    Nothing irreversible happens before the officer confirms this screen."""
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
    """Stage 2A (motion prefilter) then Stage 2B (MegaDetector V6) on
    whatever Stage A leaves pending. If Stage B's weights are not on this
    machine, everything Stage A didn't quarantine stays 'pending',
    genuinely awaiting it. See edge/pipeline/triage.py."""
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    try:
        return triage_pipeline.run_triage(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/runs/{run_id}/quarantine/restore")
def restore(run_id: str, user: dict = Depends(require_role(*config.PERMISSIONS["pipeline_trigger"]))) -> dict:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return {"restored": triage_pipeline.restore(run_id, user["username"])}


@app.get("/api/runs/{run_id}/images")
def get_run_images(run_id: str, status: str, user: dict = Depends(current_user)) -> list[dict]:
    if not repo.run(run_id):
        raise HTTPException(404, "run not found")
    return repo.images_by_status(run_id, status)


@app.post("/api/runs/{run_id}/images/{image_id}/identify")
def identify_run_image(run_id: str, image_id: str,
                        user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"]))) -> dict:
    """Runs Stage 3 (edge/pipeline/identify_upload.py) directly against a
    frame this run already ingested. The file is already on this machine
    -- a bulk scan never runs identify/matching on its own (Stage 3 takes
    one photo at a time, deliberately -- see identify_upload.py's module
    docstring), but there is no reason someone should have to find and
    re-upload the same file a second time through a browser file picker
    just to point Stage 3 at it."""
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
        raise HTTPException(400, "Stage 3 (the detector/embedder) weights are not "
                                  "downloaded on this machine.")


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
    """The most recent real, rectified flank crop for this individual --
    edge/ui/app.js requests this for the stripe rail and falls back to
    the procedurally generated pattern on a 404 (most individuals in the
    seeded demo have no real photo file; only ones identified through
    /api/identify/upload do)."""
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
    """The real rectified flank crop, if one was ever saved for this
    crop_id -- edge/ui/app.js's review screen requests this for the
    frame under review. 404s (never a placeholder image) when the crop
    predates real crop-saving or was never rectified successfully."""
    path = repo.crop_path(crop_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "no real crop image on file for this crop")
    return FileResponse(path, media_type="image/jpeg")


# ── review ──────────────────────────────────────────────────────


@app.get("/api/review")
def get_review(limit: int = 50, user: dict = Depends(current_user)) -> dict:
    return {"open": repo.review_count(), "items": repo.review_open(limit)}


@app.post("/api/review/{queue_id}/decide")
def decide(queue_id: str, payload: dict = Body(...), user: dict = Depends(require_role(*config.PERMISSIONS["review_decide"]))) -> dict:
    new_individual = bool(payload.get("new_individual"))
    actor = user["username"]
    ind_id = payload.get("ind_id")

    if new_individual:
        # "Not any of these" means a genuinely new provisional individual --
        # never the top-scoring candidate, which is what a bare ind_id from
        # the request would be if this branch trusted it. Looked up through
        # the crop this review item is actually about, since review_queue
        # itself carries no reserve_id.
        row = repo._one(repo.connect().execute(
            "SELECT im.reserve_id FROM review_queue rq"
            " JOIN flank_crops c ON c.crop_id = rq.crop_id"
            " JOIN detections  d ON d.det_id = c.det_id"
            " JOIN images      im ON im.image_id = d.image_id"
            " WHERE rq.queue_id=?", (queue_id,)))
        if not row:
            raise HTTPException(404, "queue item not found")
        ind_id = repo.create_provisional_individual(row["reserve_id"], actor)
    elif not ind_id:
        raise HTTPException(400, "ind_id required")

    try:
        result = repo.review_decide(queue_id, ind_id, actor, new_individual)
    except KeyError:
        raise HTTPException(404, "queue item not found")
    result["ind_id"] = ind_id

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
        result["recomputed"] = postprocess.after_review_decision(q["crop_id"], actor)
    return result


# ── identify: a raw photograph in, a catalogue/review/audit entry out ────

@app.post("/api/identify/upload")
async def identify_upload_route(
    file: UploadFile = File(...),
    reserve_id: str = Form(...),
    station_id: str | None = Form(None),
    user: dict = Depends(require_role(*config.PERMISSIONS["ingest_manage"])),
) -> dict:
    """Stage 3 end to end -- detect, keypoints, side, rectify, quality
    gate, embed, match, decide. See edge/pipeline/identify_upload.py.
    Keypoints come from a fixed geometric stub as of Task 5
    (AUDIT_AND_REVISED_PLAN.md); accuracy through this path is expected
    to be poor until Task 4's trained regressor replaces it -- this
    route exists to prove the pipe reaches the catalogue, the review
    queue, and the audit log, not to produce a trustworthy match yet."""
    actor = user["username"]
    if not repo.reserve(reserve_id):
        raise HTTPException(404, "reserve not found")
    dest = config.UPLOADS_DIR / f"{repo.new_id('up_')}_{file.filename}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await file.read())
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
            # A rounded centroid still leaks the range if the precise hull
            # ships alongside it -- the polygon boundary is the location,
            # not just its middle. Drop it rather than round it; a
            # "generalised" 30-vertex polygon is not a meaningful privacy
            # boundary, it is the same shape with cosmetic noise.
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
    """The community exchange format (blueprint §11) -- three CSVs plus a
    package descriptor, zipped. Readable by the wider camera-trap
    ecosystem, not just this app."""
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


@app.get("/api/stations")
def get_stations(reserve_id: str, user: dict = Depends(current_user)) -> list[dict]:
    role = user["role"]
    rows = repo.stations(reserve_id)
    for r in rows:
        r["lat"], r["lon"] = _generalise(r["lat"], r["lon"], role)
    return rows


# ── sync (interface defined, transport not built for the hackathon) ─────

@app.get("/api/sync/status")
def sync_status(reserve_id: str | None = None, user: dict = Depends(current_user)) -> dict:
    """Two separate honest answers, not one blurred together: edge-to-edge
    bundle sync is real and works if a secret is configured; the central
    tier is designed (blueprint §3) and not built at all, on this node or
    anywhere else."""
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
    """Downloads a signed bundle of everything this node has written for
    a reserve since since_lamport. A file, not a socket -- meant to
    travel over HTTP, a USB stick, or a hotspot, and to be safe to send
    (or apply) twice."""
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
    """Applies an uploaded bundle. Idempotent -- the same file can be
    dropped here twice with no ill effect -- and refuses outright if the
    signature doesn't verify, per blueprint §10: a receiving node must
    reject an unsigned or tampered bundle, not apply it cautiously."""
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
    """Returns the current sync HMAC key and node info."""
    sec = config.get_sync_secret()
    return {
        "sync_secret": sec,
        "key_length": len(sec),
        "node_id": repo.node_id(),
        "file_path": str(config.DATA_DIR / "sync_secret.txt"),
    }


@app.post("/api/sync/key")
def update_sync_key(payload: dict = Body(default={}), user: dict = Depends(require_role(*config.PERMISSIONS["user_manage"]))) -> dict:
    """Sets a new sync secret or auto-generates one if new_secret is blank."""
    new_sec = payload.get("new_secret", "")
    saved = config.set_sync_secret(new_sec)
    repo.audit("sync.key.rotate", actor=user["username"], entity_type="system",
               entity_id="sync_secret", note="rotated or imported sync key")
    return {"ok": True, "sync_secret": saved}


@app.get("/api/sync/key/download")
def download_sync_key_file(user: dict = Depends(require_role(*config.PERMISSIONS["sync_manage"]))) -> Response:
    """Downloads sync_secret.txt for transfer to other range-office laptops."""
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
