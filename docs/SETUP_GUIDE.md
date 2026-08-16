# PUGMARK — Team Setup & Onboarding Guide

> **Automated camera-trap triage and individual tiger movement intelligence.**  
> Designed to run completely offline on CPU laptops at range offices.

---

## 📋 Prerequisites

| Component | Requirement | Notes |
|---|---|---|
| **Python** | Version 3.10 or 3.11 | Make sure "Add Python to PATH" is enabled |
| **Git** | Any recent version | For cloning and pulling updates |
| **Operating System** | Windows 10/11, Ubuntu Linux, or macOS | Cross-platform compatibility |

---

## 🚀 Step-by-Step Setup Instructions

### 1. Clone the Repository
```bash
git clone <repository-url>
cd pugmark-v0.1.1
```

### 2. Create and Activate Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\activate
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Seed Demo Data & Initialize Admin Account
Initializes the SQLite schema, seeds the Pench demonstration reserve (36 stations, 3 cycles, 10 alert scenarios), and creates admin credentials:
```bash
python -m tools.seed_demo
```

*(Optional) Set a custom admin password immediately:*
```bash
python -m tools.emergency_reset_admin admin "Admin@12345"
```

### 5. Launch the Server

**Option A — Launcher Scripts:**
```bash
# Windows
launcher\run.bat

# Linux / macOS
chmod +x launcher/run.sh
./launcher/run.sh
```

**Option B — Direct Command:**
```bash
python -m uvicorn edge.app:app --host 127.0.0.1 --port 7860
```

### 6. Access the Web Interface
Open your browser and visit:  
👉 **[http://127.0.0.1:7860](http://127.0.0.1:7860)**

- **Username:** `admin`
- **Password:** Password displayed in terminal during seeding or set via `emergency_reset_admin`

---

## 🧪 Verification & Test Suite

Run these commands to verify that all database layers, pipelines, and endpoints are healthy:

```bash
# 1. Run unit test suite
python -m pytest tests/unit -q --tb=short

# 2. Run live HTTP route integration tests
python -m tests.live.test_routes

# 3. Run alert scenario engine tests
python -m tests.scenarios.test_alert_scenarios
```
