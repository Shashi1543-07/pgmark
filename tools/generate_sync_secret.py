"""Generate or load a persistent PUGMARK_SYNC_SECRET for this node.

Run once per installation (or whenever the secret file is missing).
The secret is stored in data/sync_secret.txt so it survives restarts.
All range-office laptops that need to exchange bundles MUST share the
same secret -- copy sync_secret.txt from one to the others via USB.

    python -m tools.generate_sync_secret

"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import config  # noqa: E402

SECRET_FILE = config.DATA_DIR / "sync_secret.txt"


def main() -> str:
    config.ensure_dirs()
    if SECRET_FILE.exists():
        existing = SECRET_FILE.read_text().strip()
        print("=" * 60)
        print("PUGMARK SYNC SECRET (already exists)")
        print("=" * 60)
        print(f"Secret: {existing}")
        print()
        print("This file already exists at:")
        print(f"  {SECRET_FILE}")
        print()
        print("To share sync between laptops, copy this file to the")
        print("same path on every range-office laptop.")
        print("=" * 60)
        return existing

    secret = secrets.token_urlsafe(32)
    SECRET_FILE.write_text(secret)
    print("=" * 60)
    print("PUGMARK SYNC SECRET GENERATED")
    print("=" * 60)
    print(f"Secret: {secret}")
    print()
    print("Saved to:")
    print(f"  {SECRET_FILE}")
    print()
    print("IMPORTANT: To enable USB bundle sync between laptops,")
    print("copy this file to the SAME path on every range-office")
    print("laptop. All nodes sharing data must have identical secrets.")
    print("=" * 60)
    return secret


if __name__ == "__main__":
    main()
