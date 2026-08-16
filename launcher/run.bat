@echo off
REM Pugmark edge node. No network required.
cd /d "%~dp0.."
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
set YOLO_OFFLINE=1
python -m tools.seed_demo
REM Generate a sync secret if one doesn't exist yet (enables Share Data tab).
REM Copy data\sync_secret.txt to other range-office laptops to enable cross-node bundle sync.
if not exist "data\sync_secret.txt" (
    python -m tools.generate_sync_secret
)
start "" http://127.0.0.1:7860
python -m uvicorn edge.app:app --host 127.0.0.1 --port 7860
