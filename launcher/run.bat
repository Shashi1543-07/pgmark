@echo off
REM Pugmark edge node. No network required.
cd /d "%~dp0.."
python -m tools.seed_demo
start "" http://127.0.0.1:7860
python -m uvicorn edge.app:app --host 127.0.0.1 --port 7860
