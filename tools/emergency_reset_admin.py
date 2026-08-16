"""Emergency administrator credentials reset tool.

Filesystem-access-only -- runnable only by someone with physical/shell access
to the range-office laptop itself. Resets the admin password + recovery code
directly in the SQLite database and prints them once.

    python -m tools.emergency_reset_admin
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import auth, config  # noqa: E402
from edge.db import repo  # noqa: E402


def reset_user(username: str = "admin", custom_password: str | None = None) -> dict:
    config.ensure_dirs()
    repo.migrate()

    user = repo.user_by_username(username)
    temp_pwd = custom_password or auth.generate_temp_password()
    rec_code = auth.generate_recovery_code()
    pwd_hash = auth.hash_secret(temp_pwd)
    rec_hash = auth.hash_secret(auth.normalise_recovery_code(rec_code))
    must_change = 0 if custom_password else 1

    if user:
        conn = repo.connect()
        conn.execute(
            "UPDATE users SET pwd_hash=?, recovery_code_hash=?, must_change_password=?,"
            " failed_login_attempts=0, locked_until=NULL, disabled=0"
            " WHERE username=?",
            (pwd_hash, rec_hash, must_change, username),
        )
        conn.commit()
        repo.revoke_all_sessions_for_user(username, actor="emergency_reset")
        repo.audit("auth.emergency_reset", actor="emergency_reset", entity_type="user",
                   entity_id=username, note=f"credentials reset via CLI for {username}")
    else:
        role = "admin" if username == "admin" else "director"
        display_name = "Administrator" if username == "admin" else username
        repo.insert("users", dict(
            username=username, display_name=display_name, role=role,
            pwd_hash=pwd_hash, created_at=repo.now(), disabled=0,
            recovery_code_hash=rec_hash, must_change_password=must_change,
            failed_login_attempts=0, locked_until=None, last_login_at=None))
        repo.audit("auth.user_created", actor="emergency_reset", entity_type="user",
                   entity_id=username, after={"role": role, "emergency_created": True})

    print("\n" + "=" * 68)
    print(f"  PUGMARK CREDENTIAL RESET — {username.upper()}")
    print("=" * 68)
    print(f"  Username:      {username}")
    print(f"  Password:      {temp_pwd}")
    print(f"  Recovery Code: {rec_code}")
    print("-" * 68)
    if must_change:
        print("  Status:        Temporary password generated.")
        print("                 You will be prompted to set your new permanent password on login.")
    else:
        print("  Status:        Custom permanent password configured successfully.")
    print("  All monitoring databases and telemetry remain intact.")
    print("=" * 68 + "\n")
    return {"username": username, "temp_password": temp_pwd, "recovery_code": rec_code}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pugmark offline credential reset tool")
    parser.add_argument("username", nargs="?", default="admin", help="Username to reset (default: admin)")
    parser.add_argument("--password", "-p", default=None, help="Optional custom password to set directly")
    args = parser.parse_args()
    reset_user(args.username, args.password)

