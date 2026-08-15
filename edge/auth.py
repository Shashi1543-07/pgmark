"""Password hashing, session tokens, recovery codes, and lockout math.

Pure functions only -- no SQL, no request handling, nothing that touches
`edge/db/repo.py` or FastAPI. Rule 1 (CLAUDE.md): all SQL lives in repo.py,
so persistence of anything this module produces belongs there, not here.
That split also means this module is directly unit-testable without a
database or a running server.

Two different secrets get hashed here, deliberately with two different
algorithms:

  * Passwords and recovery codes -- both human-facing secrets a person
    might reuse or that need brute-force resistance if the database is
    ever copied -- use Argon2id (`hash_secret`/`verify_secret`).
  * Session tokens are already 256 bits of `secrets.token_urlsafe` output.
    They are never memorised or reused by a person, so hashing them with
    Argon2id would only add latency to every authenticated request for no
    real security benefit; a plain SHA-256 digest is the right tool
    (`hash_token`) -- the token itself is the brute-force resistance.
"""
from __future__ import annotations

import hashlib
import secrets
import string
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from edge import config

_hasher = PasswordHasher()

# Unambiguous on a printed sheet and when typed back in by hand: no 0/O,
# 1/I/l, no punctuation that a font could render confusingly.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_secret(secret: str) -> str:
    """Argon2id, for a password or a recovery code -- anything a human
    might have chosen, memorised, or written down."""
    return _hasher.hash(secret)


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Never raises. Argon2Error covers a wrong secret, a corrupted hash,
    or a hash from an incompatible parameter set -- all three mean 'not
    verified', not 'crash the login route'."""
    try:
        return _hasher.verify(secret_hash, secret)
    except Argon2Error:
        return False


def needs_rehash(secret_hash: str) -> bool:
    """True if this hash was made with older Argon2id parameters than
    the ones this build would use today -- call after a successful
    verify_secret() and re-hash+store if so, so parameter upgrades reach
    real accounts on their next login instead of never."""
    try:
        return _hasher.check_needs_rehash(secret_hash)
    except Argon2Error:
        return False


def generate_temp_password() -> str:
    """A one-time, printed, machine-generated password -- never typed by
    a human before first login, so readability doesn't matter the way
    the recovery code's does. 24 characters of secrets.token_urlsafe
    (about 144 bits) comfortably clears min_password_length."""
    return secrets.token_urlsafe(18)


def generate_recovery_code() -> str:
    """XXXX-XXXX-XXXX-XXXX-XXXX-XXXX by default (edge/config.py's Auth
    dataclass controls the exact shape) -- long enough to resist guessing,
    short enough to copy off a printed sheet without transcription errors
    being likely."""
    cfg = config.CONFIG.auth
    groups = ["".join(secrets.choice(_RECOVERY_ALPHABET)
                       for _ in range(cfg.recovery_code_group_len))
              for _ in range(cfg.recovery_code_groups)]
    return "-".join(groups)


def normalise_recovery_code(code: str) -> str:
    """Case, hyphens, and whitespace shouldn't matter when someone is retyping a
    code off a printed sheet; the alphabet itself has no case-collisions
    to worry about (no lowercase letters ever generated)."""
    return code.strip().upper().replace(" ", "").replace("-", "")


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lockout_duration(failed_attempts: int) -> timedelta | None:
    """None below the threshold (not locked yet). Once at/above it, index
    into Auth.lockout_minutes by how far past the threshold this attempt
    is, clamped to the last (longest) entry -- progressively longer locks
    that still expire on their own, never a permanent lock (that is a
    separate, explicit, admin-only disable)."""
    cfg = config.CONFIG.auth
    if failed_attempts < cfg.failed_attempts_before_lock:
        return None
    idx = min(failed_attempts - cfg.failed_attempts_before_lock,
               len(cfg.lockout_minutes) - 1)
    return timedelta(minutes=cfg.lockout_minutes[idx])


_WEAK_PASSWORDS = {
    "password", "pugmark", "pench", "tiger", "reserve", "12345678",
    "123456789", "qwertyuiop", "changeme", "letmein", "welcome",
}


def is_weak_password(password: str, username: str = "") -> str | None:
    """Returns a reason string if the password should be rejected, else
    None. Deliberately modest: a minimum-length check plus a small
    deployment-specific denylist, not an offline breached-password
    corpus (there isn't room to ship or maintain one here) -- long enough
    and not an obvious guess is the honest bar this can actually hold,
    not "unbreakable"."""
    cfg = config.CONFIG.auth
    if len(password) < cfg.min_password_length:
        return f"Must be at least {cfg.min_password_length} characters."
    lowered = password.lower()
    if lowered in _WEAK_PASSWORDS or lowered.strip(string.punctuation) in _WEAK_PASSWORDS:
        return "Too common a password for this deployment."
    if username and username.lower() in lowered:
        return "Must not contain the username."
    return None
