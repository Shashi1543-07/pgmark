-- Wires the users/sessions tables (schema since 0001/0005, never used by
-- any code) up to a real login system. Plain ALTER TABLE ADD COLUMN is
-- safe here -- unlike 0006's CHECK-constraint widening, nothing here
-- touches an existing constraint, so no table rebuild is needed.

ALTER TABLE users ADD COLUMN recovery_code_hash TEXT;
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until TEXT;
ALTER TABLE users ADD COLUMN last_login_at TEXT;

-- sessions.role already exists (0005) but is a cache only -- authorization
-- always resolves the CURRENT users.role via session_user()'s join, so a
-- role change takes effect on the demoted/promoted user's very next
-- request rather than waiting for their session to expire on its own.
ALTER TABLE sessions ADD COLUMN revoked_at TEXT;

-- Auth events (login/logout/lockout/password change/recovery/user
-- management) are ordinary audit_log rows -- entity_type='user',
-- entity_id=username -- not a second parallel log table. audit_log is
-- already the project's one append-only event log (CLAUDE.md rule 4).
