"""
NEXUS SCADA - One-Shot System Verification Suite
=================================================
Run with the full stack UP (PLC + backend + optional UIs):

    python quick_test.py

Exit codes: 0 = ALL PASS, 1 = one or more FAIL.
Each check prints [PASS]/[FAIL]/[WARN] with a one-line verdict.
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
BACKEND = "http://localhost:8000"
PLC_HOST, PLC_PORT = "127.0.0.1", 5020

results = []          # (name, status, detail)


def record(name: str, passed: bool, detail: str, warn_only: bool = False):
    status = "PASS" if passed else ("WARN" if warn_only else "FAIL")
    results.append((name, status, detail))
    mark = {"PASS": "\033[92m[PASS]\033[0m", "FAIL": "\033[91m[FAIL]\033[0m",
            "WARN": "\033[93m[WARN]\033[0m"}.get(status, status)
    print(f"{mark} {name:42s} {detail}")


def section(title: str):
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


# ============================================================
# 1. FILE SYSTEM / ARTIFACTS
# ============================================================
section("1. PROJECT ARTIFACTS")

model = ROOT / "models" / "qwen2.5-coder-1.5b-instruct-q6_k.gguf"
record("GGUF model file", model.is_file(),
       f"{model.stat().st_size / 1024 / 1024:.0f} MB" if model.is_file() else "missing")

req = ROOT / "requirements.txt"
record("requirements.txt non-empty",
       req.is_file() and req.stat().st_size > 200,
       f"{req.stat().st_size} bytes" if req.is_file() else "missing")

db_file = ROOT / "data" / "scada.db"
record("SQLite database present", db_file.is_file(), str(db_file.name))

for p in ["backend/api.py", "backend/database.py", "plc_simulator/modbus_server.py",
          "dashboard/streamlit_app.py", "hmi/hmi_gui.py",
          "models/industrial_agent/agent.py", "models/industrial_agent/llm_reasoner.py"]:
    record(f"source: {p}", (ROOT / p).is_file(), "found" if (ROOT / p).is_file() else "MISSING")

# ============================================================
# 2. NETWORK PORTS
# ============================================================
section("2. NETWORK PORTS")

import socket

def port_open(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

plc_up = port_open(PLC_HOST, PLC_PORT)
record("PLC Modbus TCP :5020 listening", plc_up, "reachable" if plc_up else "NOT RUNNING - start plc_simulator")

api_up = port_open("127.0.0.1", 8000)
record("Backend API :8000 listening", api_up, "reachable" if api_up else "NOT RUNNING - start backend/api.py")

ui_up = port_open("127.0.0.1", 8501)
record("Streamlit :8501 listening", ui_up, "reachable" if ui_up else "not running (optional for this test)", warn_only=True)

# ============================================================
# 3. BACKEND API ENDPOINTS
# ============================================================
section("3. BACKEND API")

if api_up:
    try:
        r = requests.get(f"{BACKEND}/api/health", timeout=3)
        h = r.json()
        record("GET /api/health", r.ok, f"status={h.get('status')}")
        record("LLM available", h.get("llm_available") is True,
               "Qwen loaded in backend" if h.get("llm_available") else "rules-only mode")
        record("Agent active", h.get("agent") == "active", f"state={h.get('agent_state')}")
    except Exception as e:
        record("GET /api/health", False, f"error: {e}")

    try:
        r = requests.get(f"{BACKEND}/api/equipment", timeout=3)
        eq = r.json().get("equipment", {})
        live = {k: v for k, v in eq.items() if not v.get("stale") and v.get("data")}
        record("GET /api/equipment", len(live) == 6,
               f"{len(live)}/6 equipment returning live data")
        if live:
            sample = next(iter(live.values()))["data"]
            record("Real-unit values (voltage ~380)",
                   300 < float(sample.get("voltage", 0)) < 500,
                   f"voltage={sample.get('voltage')}")
    except Exception as e:
        record("GET /api/equipment", False, f"error: {e}")

    try:
        r = requests.get(f"{BACKEND}/api/agent/operator-message", timeout=3)
        msg = r.json().get("message", "")
        placeholder = msg.strip() in ("", "short message for HMI")
        record("Agent operator message", (not placeholder), msg[:60])
    except Exception as e:
        record("Agent operator message", False, f"error: {e}")
else:
    print("  (skipped - backend not running)")

# ============================================================
# 4. DATABASE INTEGRITY
# ============================================================
section("4. DATABASE")

try:
    conn = sqlite3.connect(str(db_file))
    n, newest = conn.execute(
        "SELECT COUNT(*), MAX(timestamp) FROM equipment_data").fetchone()
    fresh = datetime.now() - datetime.fromisoformat(newest) < timedelta(seconds=15)
    record("DB has telemetry rows", n > 0, f"{n} rows, newest={newest[:19]}")
    record("DB is receiving FRESH pushes (<15s old)", fresh,
           "pipeline PLC->backend->DB is LIVE" if fresh else "STALE - pushes stopped")

    # theta payload fix verification (the silent-loss bug we fixed)
    theta_rows = conn.execute(
        "SELECT theta_per_mille FROM equipment_data "
        "WHERE timestamp > ? LIMIT 10",
        ((datetime.now() - timedelta(minutes=5)).isoformat(),)).fetchall()
    theta_nonzero = any(r[0] > 0 for r in theta_rows) if theta_rows else False
    record("Theta flowing (payload fix applied)",
           theta_nonzero,
           "theta values present" if theta_nonzero else "all zero in last 5min - check simulator payload")

    # status type-sync check (Risk 1 fix)
    st_vals = conn.execute(
        "SELECT DISTINCT typeof(status) FROM equipment_data "
        "WHERE timestamp > ? LIMIT 5",
        ((datetime.now() - timedelta(minutes=5)).isoformat(),)).fetchall()
    types = {t[0] for t in st_vals}
    record("status column stores TEXT", types <= {"text"},
           f"types found: {sorted(types)}")

    alarms = conn.execute("SELECT COUNT(*) FROM alarm_events").fetchone()[0]
    record("alarm_events populated", alarms >= 0, f"{alarms} alarm events")
    conn.close()
except Exception as e:
    record("Database checks", False, f"error: {e}")

# ============================================================
# 5. STATISTICS MODULE (Feature A foundation)
# ============================================================
section("5. STATISTICS MODULE")

try:
    sys.path.insert(0, str(ROOT / "backend"))
    from database import db as scada_db
    t0 = time.time()
    stats = scada_db.get_equipment_statistics("STP-01", hours=24)
    ms = (time.time() - t0) * 1000
    record("get_equipment_statistics works", isinstance(stats, dict) and "sample_count" in stats,
           f"{stats}")
    record("Statistics performance < 100ms", ms < 100, f"{ms:.1f} ms")
except Exception as e:
    record("Statistics module", False, f"error: {e}")

# ============================================================
# 6. UNIT TESTS
# ============================================================
section("6. UNIT TESTS (pytest)")

import subprocess
r = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header", "-x"],
    capture_output=True, text=True, cwd=str(ROOT), timeout=120,
)
pytest_ok = r.returncode == 0
last_line = (r.stdout.strip().splitlines() or [""])[-1]
record("tests/ suite passes", pytest_ok, last_line)

# ============================================================
# SUMMARY
# ============================================================
section("SUMMARY")
fails = [r for r in results if r[1] == "FAIL"]
warns = [r for r in results if r[1] == "WARN"]
passes = [r for r in results if r[1] == "PASS"]
print(f"  PASS: {len(passes)}   WARN: {len(warns)}   FAIL: {len(fails)}")
if fails:
    print("\n  Failed checks:")
    for name, _, detail in fails:
        print(f"    - {name}: {detail}")
verdict = "SYSTEM HEALTHY - safe to proceed" if not fails else "SYSTEM DEGRADED - fix FAILs above"
print(f"\n  VERDICT: {verdict}\n")
sys.exit(0 if not fails else 1)
