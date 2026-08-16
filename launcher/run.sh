#!/usr/bin/env bash
# Pugmark edge node. No network required.
set -e
cd "$(dirname "$0")/.."
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export YOLO_OFFLINE=1
python3 -m tools.seed_demo 2>/dev/null || true
exec python3 -m uvicorn edge.app:app --host 127.0.0.1 --port 7860
