# Offline authentication for Pugmark

## Context

Right now there is no authentication at all. Every route that cares about
identity — precise vs. generalised tiger coordinates, who made a review
decision, who ran a pipeline — reads it from a bare, client-supplied
`role`/`actor` string parameter with no credential behind it. `?role=director`
in the URL is the entire access control today. This matters more than usual
for this app specifically: the data being protected is precise GPS locations
of individual tigers, which CLAUDE.md's own module docstring already names as
"exactly what a poaching network would want."

The schema for this was actually scaffolded once already — a `users` table
(with `pwd_hash`) since migration 0001, and a `sessions` table (with
`token_hash`) since migration 0005 — but deliberately left unwired, on the
stated reasoning that "half-implementing this, so a route looks authenticated
and is not, would be worse than leaving it visibly open." That reasoning was
correct at the time. This plan is the other half: actually wire it.

**Confirmed scope** (settled with the user before writing this plan):
- Single laptop only — browser and server are the same machine, bound to
  `127.0.0.1`. No HTTPS/internal-CA work; that only matters for a multi-machine
  LAN deployment, which isn't happening here.
- Seed exactly one initial account: a single admin, random temporary password
  + recovery code, printed once to the terminal. The admin creates every other
  account afterward through the app itself.

**Verified current state** (audited directly, not assumed):
- `users`/`sessions` schema exists (migrations `0001`, `0005`) but zero
  `repo.py` functions touch either table — pure dead schema. Next migration
  is `0007`.
- `role` is read as an unauthenticated `Query("director", ...)` on 8 routes
  (`edge/app.py:378,510,530,543,556,566,625`, `edge/routes_scale.py:240`),
  gating coordinate precision via `_generalise()` / `config.privacy
  .generalise_coords_for_roles`. A dead `_role()` dependency exists at
  `edge/app.py:107` but is never wired via `Depends()`.
- `actor` is read as an unauthenticated `Body`/`Form`/`payload.get` string on
  ~15 state-changing routes (review decide/claim, promote/merge, alert
  acknowledge, quarantine restore, identify upload, sync apply, job
  cancel/resume, pipeline triggers) — pure audit attribution, never validated.
- No password/session/JWT library is installed; no login/cookie code exists
  anywhere in `edge/`. This is a blank slate on application logic.
- The frontend has zero role-awareness — no login screen, no stored session
  state. `api()`'s fetch call has no `credentials` override, so same-origin
  cookies should flow automatically once a session cookie exists.
- One middleware precedent exists (`no_cache_ui`, `edge/app.py:45`) to follow
  the shape of.
- Routes are registered in two places needing consistent coverage: directly
  in `edge/app.py`, and via `_register_scale_routes(app)` from
  `edge/routes_scale.py`.
- Background jobs (`edge/jobs.py`) already run on their own thread,
  independent of the HTTP request that started them — confirms the
  "logout must not interrupt a 50K job" requirement is naturally satisfied,
  not something new to build; only needs a verification pass, not new code.
- The full test suite (166 live + 49 unit + 8 scenario + 38 megafix-verify)
  currently calls protected routes with no session, or a bare `role=`/`actor=`
  param expecting it to be honored. **This will break almost the whole live
  suite** unless the suite itself authenticates first — this is a first-class
  part of the plan, not a footnote.

## Design decisions

**RBAC enforcement**: explicit `Depends()` on each protected route, not a
blanket middleware gate. Middleware's only job is to resolve `request.state
.user` (a `UserCtx` or `None`) from the session cookie — cheap, one indexed
lookup, runs for every request. Actual enforcement is a `Depends(require_role
(...))` added to each route's signature, mirroring how `_role()`/`_generalise
()` already work today, just backed by a real session instead of a client
string. This touches ~25-30 route signatures across two files — mechanical,
not risky per-instance, but real breadth. Rejected the middleware-only /
path-prefix-allowlist alternative: different routes need different role sets,
not just authenticated-or-not, and that granularity belongs at the route.

**`sessions.role` is a cache, never authoritative**: resolve role fresh from
`users.role` on every request for actual authorization decisions. If an admin
demotes someone mid-session, the demotion takes effect on their very next
request, not after their session happens to expire. The extra join is
negligible at this scale (a handful of accounts, SQLite, local disk).

**No HTTP-reachable emergency recovery.** If both password and recovery code
are lost, that's `python -m tools.emergency_reset_admin` — a script runnable
only by someone with filesystem access to the laptop itself, which resets the
admin's password + recovery code directly in the DB and prints them once.
Building an in-app "cryptographic challenge" flow for this would be security
theater for a single-physical-laptop threat model: physical custody of the
machine already implies more trust than any online challenge could add.

**Auth events reuse the existing `audit_log`**, not a new
`password_reset_events` table. `repo.audit(action="auth.login_success", ...)`
etc., `entity_type="user"`, `entity_id=username` — this table is already the
project's one append-only event log (CLAUDE.md rule 4); a second parallel
logging table would just be two places to look instead of one.

**Test suite re-authenticates once at the top.** `TestClient` (httpx-based)
persists cookies across requests within one client instance already. Seed
scripts (`tools/seed_demo.py`, `tools/reset_blank.py`, `tools/seed_bulk.py`)
gain a shared helper that creates the admin account; in production this
generates a random password. Tests call `repo.create_user(...)` directly
(bypassing HTTP, not a security hole — it's test setup) with a *known* test
password, then `POST /api/auth/login` once at suite start so every subsequent
`c.get(...)`/`c.post(...)` in that suite carries a valid session automatically.
Route calls that currently pass `?role=analyst` or `actor=...` to simulate a
different identity get rewritten to log in as a real user of that role
instead — proving the server now actually enforces it, not just accepts it.

## Implementation, in order

### 1. Migration `edge/db/migrations/0007_auth_hardening.sql`
Extend `users`: `recovery_code_hash TEXT`, `must_change_password INTEGER NOT
NULL DEFAULT 1`, `failed_login_attempts INTEGER NOT NULL DEFAULT 0`,
`locked_until TEXT`, `last_login_at TEXT`. (`disabled` already exists — reuse
it, don't add a redundant `is_active`.) Extend `sessions`: `revoked_at TEXT`.
Follow the same rebuild-and-copy pattern already used in `0006_images_vehicle
_status.sql` if SQLite can't `ALTER TABLE ADD COLUMN` cleanly here (it can,
for plain column adds with no new CHECK constraint — only the vehicle-status
change needed a full rebuild because it widened an existing CHECK).

### 2. `edge/auth.py` — pure crypto/token logic, no SQL (Rule 1)
Argon2id hash/verify (via `argon2-cffi`, added to `requirements.txt`).
Session token generation (`secrets.token_urlsafe`) and its storage hash
(sha256 is fine here — it's a high-entropy random token, not a human
password, so it doesn't need Argon2's cost). Recovery-code generation in a
transcribable format (grouped, unambiguous alphabet). Lockout math (attempt
count → backoff duration). No database access in this file at all — every
function takes/returns plain values, gets tested directly with no DB.

### 3. `edge/db/repo.py` additions (Rule 1: this is where the SQL lives)
`create_user`, `user_by_username`, `set_password`, `record_login_success`,
`record_login_failure` (increments/resets `failed_login_attempts`, sets
`locked_until`), `create_session`, `session_user` (session→user join,
resolves *current* `users.role`), `revoke_session`, `revoke_all_sessions_for
_user`, `rotate_recovery_code`, `list_users`, `disable_user`.

### 4. Middleware + dependencies in `edge/app.py`
A new `@app.middleware("http")` function (same shape as `no_cache_ui`) that
reads the session cookie, calls `repo.session_user(...)`, attaches the result
to `request.state.user`. Two `Depends()` helpers: `current_user(request)`
(401 if `request.state.user` is None or the session is revoked/expired) and
`require_role(*roles)` (a dependency factory, 403 if the resolved role isn't
in the allowed set). These replace every existing `role: str = Query(...)`
and `actor: str = Body(...)` parameter — the route derives both from
`Depends(current_user)` now, never from client input.

### 5. Auth routes, new section in `edge/app.py`
`POST /api/auth/login` (generic "invalid username or password" on any
failure — wrong password, locked, disabled, or unknown username all look
identical, no enumeration), `POST /api/auth/logout`, `POST /api/auth/change
-password`, `POST /api/auth/forgot-password` (recovery code → new password,
rotates the code, revokes all sessions), `GET /api/auth/me`. Admin-only:
`GET/POST /api/auth/users`, `POST /api/auth/users/{username}/disable`.

### 6. Permission matrix, grounded in the routes that actually exist
A concrete dict in `edge/config.py` mapping capability → allowed roles,
covering: pipeline/triage/stage3/postprocess triggers (admin, director,
biologist), review decide/claim (admin, director, biologist), individual
promote/merge (admin, director), alert acknowledge (admin, director),
precise-vs-generalised coordinate routes (existing `generalise_coords_for
_roles` config, now driven by the authenticated role), `/api/dev/seed`
(admin-only — this becomes a real risk once real users exist), user
management (admin-only), audit log read (admin, director; restricted for
analyst per the existing pattern).

### 7. UI (`edge/ui/index.html`, `edge/ui/app.js`, `edge/ui/app.css`)
A login view shown whenever `GET /api/auth/me` returns 401, replacing the
whole shell until authenticated. A forced first-login password-change screen
when `must_change_password` is true. A forgot-password flow (username →
recovery code → new password). Topbar gets "logged in as X (role) · Log out".
Admin-only Users screen (list/create/disable) — likely a new nav item or
folded into System Health, gated so the nav link itself doesn't render for
non-admins (a nicety; the real gate is already server-side per rule 7).

### 8. Seeding
`tools/seed_demo.py`, `tools/seed_bulk.py`, `tools/reset_blank.py` all gain a
call to a shared "ensure one admin exists" helper. Production path: random
password + recovery code, printed once. Test path: `tests/live/test_routes
.py` calls `repo.create_user(...)` directly with a known password, then logs
in once at suite start.

### 9. Emergency recovery tool
`tools/emergency_reset_admin.py` — filesystem-access-only, resets the admin's
password + recovery code, prints once. No HTTP route.

## Verification

Run the existing full suite after each numbered step that could affect it,
not just at the end — `python -m tests.live.test_routes`, `python -m tests
.scenarios.test_alert_scenarios`, each `tests/unit/test_*.py`, `python -m
tools.verify_megafix`. All must stay green, with the live suite's
role/actor-dependent checks rewritten to authenticate as a real user instead
of passing the parameter directly.

New checks to add to `tests/live/test_routes.py`, in the same real-effect
style as the existing ones (not just "route exists"):
- Wrong password is rejected; correct password succeeds.
- 5 failed attempts locks the account; correct password still fails while
  locked.
- A `role=` query parameter the client sends is now ignored — a logged-in
  `field` user hitting a route gated to `director` gets 403 regardless of
  what `role=` they append to the URL.
- A `field`-role session cannot create a user, disable a user, or hit
  `/api/dev/seed`.
- Logging out does not stop a running background job — start a job, log out,
  confirm via a fresh login that the job kept progressing.
- A revoked session (post-logout, post-password-change) is rejected on its
  next request, not just future ones.
- Forgot-password with the right recovery code succeeds and the *old* code
  no longer works afterward (rotation actually happened).
- No endpoint reveals whether a given username exists (same generic message
  for unknown-user vs. wrong-password vs. locked, both at login and at
  forgot-password).
