"""Verify that a field release is self-contained and air-gap safe.

This tool never downloads, installs, or repairs an asset. It is intentionally
strict: a release with absent or unhashed model files must fail here, before it
is copied to a field laptop where networking is not an available recovery
path.

Run after the build system has copied approved model binaries into
edge/models/ and generated its manifest:

    python -m tools.verify_offline_release
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from edge import config


OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "YOLO_OFFLINE")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict:
    try:
        raw = json.loads(config.MODEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        return {"_error": f"invalid JSON: {exc}"}
    return raw if isinstance(raw, dict) else {"_error": "manifest root must be an object"}


def verify() -> list[str]:
    """Return every release-blocking defect without mutating the bundle."""
    failures: list[str] = []
    for name in OFFLINE_ENV:
        if os.environ.get(name) != "1":
            failures.append(f"{name} must equal 1 (got {os.environ.get(name)!r})")

    manifest = load_manifest()
    if manifest.get("schema_version") != 1:
        failures.append("edge/models/manifest.json must declare schema_version 1")
    entries = manifest.get("files")
    if not isinstance(entries, dict):
        failures.append("manifest files must be an object keyed by bundle-relative path")
        entries = {}

    for label, path in config.runtime_model_paths().items():
        try:
            relative = path.relative_to(config.MODELS_DIR).as_posix()
        except ValueError:
            failures.append(f"{label}: runtime path escapes edge/models: {path}")
            continue
        entry = entries.get(relative)
        if not path.is_file():
            failures.append(f"{label}: missing {path}")
            continue
        if not isinstance(entry, dict):
            failures.append(f"{label}: {relative} is not recorded in the manifest")
            continue
        expected = entry.get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            failures.append(f"{label}: {relative} has no valid SHA-256 in the manifest")
            continue
        if sha256(path) != expected.lower():
            failures.append(f"{label}: SHA-256 mismatch for {relative}")
    return failures


def main() -> int:
    failures = verify()
    if failures:
        print("OFFLINE RELEASE VERIFICATION FAILED")
        print("The field installer must not continue. Supply a complete, signed local model bundle.")
        print("\n".join(f"  FAIL {item}" for item in failures))
        return 1
    print("offline release verified: all runtime weights are local, present, and hash-checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
