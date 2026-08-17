# Security and data governance

**Precise, current tiger locations are exactly what a poaching network
wants.** A system that aggregates every individual's coordinates, home
range and movement trend into one queryable database is, built carelessly,
the most useful thing such a network could steal. This file states what
this node actually enforces, what it deliberately refuses to do, and what
is configured but *not yet* enforced — the last section matters most,
because a privacy control that only exists in a config file is a claim,
not a control.

Scope: the edge node (`edge/`). The central tier is not built
(`/api/sync/status` reports `enabled: false` and says why).

---

## 1. Exposure surface

| Property | Value | Why |
|---|---|---|
| Bind address | `127.0.0.1` only | The process holds precise tiger locations. It is not reachable from the network; `launcher/run.bat` and `run.sh` both pin the host. Exposing `0.0.0.0` would be an explicit, logged decision. |
| Internet | none, ever | No CDN, no webfont, no tile server. `tests/live/test_routes.py` greps the served HTML/CSS/JS and fails the build if any off-machine URL appears. |
| Transport | HTTP on loopback | `secure=False` on the session cookie is deliberate: there is no TLS on `127.0.0.1`, and setting the flag would silently break the session. It is not a substitute for TLS on any deployment that ever leaves loopback. |

## 2. Authentication

- **Argon2id** password hashing (`edge/auth.py`); tokens stored as hashes,
  never in plaintext.
- **No default credentials.** First launch mints a random admin password
  and forces a change on first login.
- Admin-created accounts receive a **generated** temp password (~144 bits)
  plus a recovery code; the admin never chooses the password, so a weak
  one cannot be set that way.
- User-chosen passwords are validated (`auth.is_weak_password`): minimum
  length from `config.auth.min_password_length`, a small deployment
  denylist, and no username substring. Deliberately modest — this is the
  honest bar an offline node can hold, not "unbreakable".
- **Lockout** after `failed_attempts_before_lock` with escalating windows,
  and timing-indistinguishable responses for unknown accounts.
- Sessions carry both an **absolute** lifetime and an **idle** timeout;
  cookies are `HttpOnly` and `SameSite=Lax`.

## 3. Authorisation

Enforced **server-side only** (CLAUDE.md rule 7). A UI check is a
suggestion; a server check is a control. The matrix lives in
`edge/config.py::PERMISSIONS`.

| Capability | Roles | Rationale |
|---|---|---|
| `user_manage` | admin | Account control. |
| `dev_seed` | admin | **Irreversible** — wipes every reserve, run, tiger and alert on the node. |
| `sync_manage` | director, admin | Builds a signed bundle of the catalogue for transport off this machine. The export path is the exfiltration path. |
| `ops_manage` | director, admin | Whole-database backup — a complete copy of everything the node knows. |
| everything operational | all roles | A field officer who cannot run triage or answer the review queue cannot do their job. The sensitive parts of what they see are gated at the point of disclosure instead (below). |

## 4. What is disclosed, and to whom

**Coordinate generalisation.** Roles listed in
`config.privacy.generalise_coords_for_roles` (currently `analyst`) receive
centroids snapped to a `grid_cell_km` cell rather than points, in the API,
the GeoJSON export, the CSV export and the Camtrap DP package. National
analysis needs distribution, not the tree the tigress sleeps under.

**Person images are never served.** The triage cascade routes any frame
containing a person out of the tiger pipeline into `persons_restricted`,
storing a blurred derivative. There is **no route that serves an original
person frame to anybody**, at any role. `/api/images/{image_id}/file`
refuses `status='person'` outright with a 403 explaining that human
presence is reported as counts by station and date, not as browsable
images — the operationally useful form, and the form that does not
accidentally build a surveillance tool aimed at forest-dwelling
communities.

**Audit reads, not just writes.** Every read of precise coordinates, every
export download, every image fetch, and every *refused* attempt to reach a
restricted person frame writes to `audit_log` with the actor and role. In
a poaching investigation, "who looked at PENCH-014's locations last month"
is the question that matters. `audit_log` is append-only, enforced by SQL
triggers — `UPDATE` and `DELETE` against it are rejected by the database
itself, not by convention.

## 5. Findings from the 2026-08-17 audit

Fixed in this pass:

1. **Original person frames were served to any logged-in account.**
   `_restrict_person()` correctly blurred the frame, filed it to
   `persons_restricted` and set `status='person'` — but the `images` row
   kept pointing at the untouched original and `/api/images/{image_id}/file`
   selected `orig_path` with no status check. Every privacy mechanism was
   built; that one query never asked. The route now refuses person frames,
   increments `access_count`, and audits the refusal.
2. **Destructive and exfiltration capabilities were granted to every
   role.** `dev_seed` (erases the node), `sync_manage` (exports the
   catalogue off-machine) and `ops_manage` (full database copy) were all
   `ALL_ROLES`. The gate was genuinely server-side; the policy behind it
   granted almost everything to everyone. Now restricted per §3.
3. **The live test suite asserted the flaw.** It checked that a `field`
   user *could* reach `/api/dev/seed`. That assertion is inverted, and
   three checks were added covering the person-frame refusal and the two
   newly restricted capabilities.
4. **The test suite locked operators out of their own node.** It
   overwrites the admin password with a known test value so it can
   authenticate — harmless against a scratch database, but it is also the
   documented verification gate every session is told to run. It now
   captures the real password hash first and restores it in a
   `finally` block.
5. **Seeding could destroy the operator's own snapshot.** Loading demo
   data backs up live data first, but `blank` takes no backup by design
   while still resetting the mode to `live` — so a `blank` → `demo`
   sequence wrote an empty database over a real snapshot. An empty
   database can no longer outrank an existing backup.

Checked and found sound: no SQL injection (every dynamic fragment
interpolates table/column names and `?` placeholders; values are always
parameters), no hardcoded credentials, no debug mode, no frontend/backend
route mismatches, no unauthenticated access to any data route, and person
frames provably cannot reach the crop pipeline.

## 6. Known gaps — configured but NOT enforced

**Retention is not implemented.** `config.privacy` declares
`person_image_retention_days = 90` and `wildlife_image_retention_days =
3650`. **No code reads these values and nothing is ever purged.** Any
statement that this node deletes person imagery after 90 days would be
false today. Implementing it means deleting files on disk, which is not a
change to make under deadline pressure without a dry-run mode and a
restore path — it is recorded here rather than quietly shipped as a claim.

**Encryption at rest is not implemented.** BLUEPRINT.md §10 calls for
encrypting the database and image store, on the grounds that a range
office laptop is a stealable object. The database is currently plain
SQLite and images are plain files. Full-disk encryption on the host is the
practical mitigation until this exists.

**Two routes leak internal error detail** to the client
(`seed failed: <stderr>`, `backup failed: <exception>`). Both are now
admin/director-gated, so the audience is small, but the messages are
verbatim internals.

**Person-presence reporting does not exist yet.** The blueprint's intended
replacement for browsable person images — counts by station and date — is
described in the refusal message but is not built as a screen. The data
(`persons_restricted`, with `station_id` and `captured_at` on the parent
image) is present to build it from.

## 7. Retention defaults, as configured

| Class | Default | Enforced? |
|---|---|---|
| Restricted person images | 90 days | **No** — see §6 |
| Wildlife images | 3650 days (10 years) | **No** — see §6 |
| Quarantined blanks | purgeable with explicit sign-off | Restore is implemented and demonstrable; scheduled purge is not |

Report a security issue by opening an entry in the project's issue
tracker, or, for a live deployment, to the reserve's IT contact. This node
is single-machine and offline; there is no remote disclosure channel.
