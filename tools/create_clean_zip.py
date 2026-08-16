"""Creates an ultra-compact, clean source zip of the PUGMARK codebase for AI agents.

Includes:
  - edge/ (core logic, DB migrations, pipeline stages, export formats, sync, UI frontend)
  - tools/ (CLI seed, reset, evaluation, training, dataset scripts)
  - tests/ (full unit, integration, and live route test suites)
  - docs/ & root docs (architecture blueprint, ROUTING_AUDIT, CLAUDE.md, AGENT_TASKS, specs)
  - Configuration files (pyproject.toml, requirements.txt, .gitignore, README.md)

Excludes:
  - Binary model weights (*.ts, *.onnx, *.pt, *.pth, *.bin, *.safetensors, *.tflite)
  - Image assets & datasets (*.jpg, *.png, *.jpeg, *.webp, data/raw, test packs)
  - SQLite databases (*.db, *.sqlite)
  - Caches, git & zip archives (__pycache__, .git, *.zip, .pytest_cache)

Run:
    python -m tools.create_clean_zip
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP_OUT = ROOT / "pugmark-lightweight-codebase.zip"

EXCLUDE_DIRS = {
    ".git",
    ".claude",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "venv",
    ".venv",
    "manual_test_500",
    "manual_test_100",
    "manual_test_photos",
    "camera_trap_pack",
    "eccv_18_all_images_sm",
    "testing data",
    "pugmark-v0.1.1-clean-source",
    "backups",
    "quarantine",
    "uploads",
    "crops",
    "raw",
    "models",  # Exclude heavy weights (ts/onnx); model code lives in edge/pipeline/
}

EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".zip",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-shm",
    ".db-wal",
    ".ts",
    ".onnx",
    ".pt",
    ".pth",
    ".bin",
    ".safetensors",
    ".tflite",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".mp4",
    ".avi",
}

# Specific paths to exclude
EXCLUDE_PREFIXES = [
    str(ROOT / "data"),
    str(ROOT / "testing data"),
    str(ROOT / "manual_test_500"),
    str(ROOT / "manual_test_100"),
    str(ROOT / "manual_test_photos"),
    str(ROOT / "camera_trap_pack"),
    str(ROOT / "pugmark-v0.1.1-clean-source"),
]


def should_include(file_path: Path) -> bool:
    # Check extension
    for ext in EXCLUDE_EXTENSIONS:
        if file_path.name.lower().endswith(ext):
            return False

    # Check directory parts
    for part in file_path.parts:
        if part in EXCLUDE_DIRS:
            return False

    # Check path prefixes
    str_path = str(file_path.resolve())
    for prefix in EXCLUDE_PREFIXES:
        if str_path.startswith(prefix):
            return False

    return True


def create_zip() -> Path:
    print("=" * 68)
    print("  PUGMARK · PACKAGING LIGHTWEIGHT AGENT-READY CODEBASE")
    print("=" * 68)

    file_count = 0
    total_uncompressed_bytes = 0

    if ZIP_OUT.exists():
        ZIP_OUT.unlink()

    with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, dirs, files in os.walk(ROOT):
            # Prune excluded directories in-place for speed
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]

            for file_name in sorted(files):
                file_path = Path(root) / file_name
                if not should_include(file_path):
                    continue

                rel_path = file_path.relative_to(ROOT)
                zf.write(file_path, arcname=str(rel_path))
                file_count += 1
                total_uncompressed_bytes += file_path.stat().st_size

    zip_size_kb = ZIP_OUT.stat().st_size / 1024
    uncompressed_kb = total_uncompressed_bytes / 1024

    print(f"  [+] Packaged {file_count} essential code/doc files.")
    print(f"  [+] Uncompressed source size: {uncompressed_kb:.1f} KB")
    print(f"  [+] Ultra-compact zip size:   {zip_size_kb:.1f} KB")
    print(f"  [+] Output location: {ZIP_OUT.name}")
    print("=" * 68)
    return ZIP_OUT


if __name__ == "__main__":
    create_zip()

