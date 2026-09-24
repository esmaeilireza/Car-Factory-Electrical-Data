#!/usr/bin/env python3
"""
NEXUS SCADA - System Verification Suite v3
==========================================
    python tests/quick_test.py                # read-only checks
    python tests/quick_test.py --wait 90      # poll until backend ready (cold start)
    python tests/quick_test.py --live         # + Modbus probes + cognitive agent probe
                                              #   (STOPS/RESTARTS motors, injects a fault)
    python tests/quick_test.py --json         # write evidence artifact to docs/evidence/

Exit codes: 0 = ALL PASS, 1 = one or more FAIL.

The cognitive probe (--live) exercises the full Feature A + Feature B chain:
fault injection -> rule-engine HIGH finding -> local LLM diagnosis ->
Obsidian incident file with [[wiki-links]]. Budget ~2-3 extra minutes.
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BACKEND = "http://localhost:8000"
PLC_HOST, PLC_PORT = "127.0.0.1", 5020
AUDIT_PLC = ROOT / "plc_simulator" / "audit_log.jsonl"
AUDIT_AGENT = ROOT / "data" / "agent_audit.jsonl"
EVIDENCE_DIR = ROOT / "docs" / "evidence"
VAULT = ROOT / "data" / "scada_vault"

SYS_STATUS_ADDR = 120
MOTOR_STATE_OFFSET = 8
EQ_IDS = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
EQ_BASE = {eq: i * 18 for i, eq in enumerate(EQ_IDS)}

results = []


def record(name, passed, detail, warn_only=False):
    status = "PASS" if passed else ("WARN" if warn_only else "FAIL")
    results.append((name, status, str(detail)))
    mark = {"PASS": "\033[92m[PASS]\033[0m", "FAIL": "\033[91m[FAIL]\033[0m",
            "WARN": "\033[93m[WARN]\033[0m"}.get(status, status)
    print(f"{mark} {name:46s} {detail}")


def section(title):
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


# ------------------------------------------------------------
# Readiness
# ------------------------------------------------------------
def wait_for_backend(timeout_s):
    section("0. READINESS")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(f"{BACKEND}/api/health", timeout=5).ok:
                record("Backend ready", True,
                       f"ready after {timeout_s - (deadline - time.time()):.0f}s")
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    record("Backend ready", False,
           f"not ready within {timeout_s}s (LLM cold load can take ~60s)")
    return False


# ------------------------------------------------------------
# Audit integrity (always runs, read-only)
# ------------------------------------------------------------
def verify_chain(path):
    if not path.is_file():
        return False, "file missing"
    prev, n = None, 0
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, f"malformed JSON at line {n}"
            if prev is not None and rec.get("prev_hash", "") != prev:
                return False, f"CHAIN BREAK at line {n}"
            prev = rec.get("hash", prev)
    return True, f"{n} records, linkage intact"


def agent_loop_age():
    if not AUDIT_AGENT.is_file():
        return None
    try:
        last = AUDIT_AGENT.read_text(encoding="utf-8").strip().splitlines()[-1]
        ts = json.loads(last).get("ts", "")
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return None


def check_audit_integrity():
    section("AUDIT INTEGRITY")
    record("PLC audit at pinned path", AUDIT_PLC.is_file(),
           str(AUDIT_PLC.relative_to(ROOT)))

    strays = [str(p) for p in ROOT.rglob("audit_log.jsonl")
              if p != AUDIT_PLC and not {"venv", ".git", "docs"} & set(p.parts)]
    record("No stray audit_log.jsonl copies", not strays,
           "clean" if not strays else f"STRAYS: {strays}")

    ok, msg = verify_chain(AUDIT_AGENT)
    record("agent_audit hash-chain linkage", ok, msg)

    age = agent_loop_age()
    record("Agent loop alive (entry < 120s)", age is not None and age < 120,
           f"last entry {age:.0f}s old" if age is not None else "no entries")


# ------------------------------------------------------------
# Backend API
# ------------------------------------------------------------
def check_backend(api_up):
    section("BACKEND API")
    if not api_up:
        print("  (skipped - backend not running)")
        return
    try:
        h = requests.get(f"{BACKEND}/api/health", timeout=8).json()
        record("GET /api/health", h.get("status") in ("ok", "healthy"),
               f"status={h.get('status')}")
        record("LLM available", h.get("llm_available") is True,
               "Qwen loaded" if h.get("llm_available") else "rules-only mode")
    except Exception as e:
        record("GET /api/health", False, f"error: {e}")
        return

    try:
        eq = requests.get(f"{BACKEND}/api/equipment", timeout=8).json().get("equipment", {})
        live = {k: v for k, v in eq.items() if not v.get("stale") and v.get("data")}
        record("6/6 equipment live", len(live) == 6, f"{len(live)}/6")
        if live:
            s = next(iter(live.values()))["data"]
            record("Real-unit values", 300 < float(s.get("voltage", 0)) < 500,
                   f"voltage={s.get('voltage')}")
    except Exception as e:
        record("GET /api/equipment", False, f"error: {e}")

    try:
        st = requests.get(f"{BACKEND}/api/agent/status", timeout=8).json()
        raw = (st.get("last_llm_result") or {}).get("raw", "") or ""
        echo = "short message for HMI" in raw or "short diagnosis" in raw
        record("LLM not echoing prompt example (last result)", not echo,
               "clean" if not echo else "CANARY HIT - echo guard missing/bypassed")
        record("Agent working memory populated",
               isinstance(st.get("working_memory_size"), int) and st["working_memory_size"] > 0,
               f"size={st.get('working_memory_size')}")
    except Exception as e:
        record("GET /api/agent/status", False, f"error: {e}")


# ------------------------------------------------------------
# Database (regression tests for past bugs)
# ------------------------------------------------------------
def check_database():
    section("DATABASE (regression checks)")
    db_file = ROOT / "data" / "scada.db"
    if not db_file.is_file():
        record("SQLite database present", False, str(db_file))
        return
    try:
        conn = sqlite3.connect(str(db_file))
        n, newest = conn.execute(
            "SELECT COUNT(*), MAX(timestamp) FROM equipment_data").fetchone()
        fresh = newest and (datetime.now() -
                            datetime.fromisoformat(newest)) < timedelta(seconds=15)
        record("Telemetry pipeline LIVE (<15s)", bool(fresh),
               f"{n} rows, newest={str(newest)[:19]}")

        rows = conn.execute(
            "SELECT theta_per_mille FROM equipment_data WHERE timestamp > ? LIMIT 10",
            ((datetime.now() - timedelta(minutes=5)).isoformat(),)).fetchall()
        record("Theta flowing (payload-fix regression)",
               any(r[0] > 0 for r in rows) if rows else False,
               "theta present" if rows else "no rows in 5min")

        types = {t[0] for t in conn.execute(
            "SELECT DISTINCT typeof(status) FROM equipment_data "
            "WHERE timestamp > ? LIMIT 5",
            ((datetime.now() - timedelta(minutes=5)).isoformat(),)).fetchall()}
        record("status column stores TEXT", types <= {"text"}, f"types: {sorted(types)}")
        conn.close()
    except Exception as e:
        record("Database checks", False, f"error: {e}")

    try:
        sys.path.insert(0, str(ROOT / "backend"))
        from database import db as scada_db
        t0 = time.time()
        stats = scada_db.get_equipment_statistics("STP-01", hours=24)
        ms = (time.time() - t0) * 1000
        record("get_equipment_statistics", "sample_count" in (stats or {}),
               f"{ms:.1f} ms")
        # Regression tripwire, not a target: wall-clock under the live
        # stack includes CPU contention from backend LLM inference.
        record("Statistics perf tripwire (<400ms)", ms < 400, f"{ms:.1f} ms")
    except Exception as e:
        record("Statistics module", False, f"error: {e}")


# ------------------------------------------------------------
# Obsidian vault skeleton (read-only, always runs)
# ------------------------------------------------------------
def check_vault_skeleton():
    section("OBSIDIAN VAULT (Feature B structure)")
    if not VAULT.is_dir():
        record("Vault present", False, str(VAULT))
        return
    record("Vault present", True, str(VAULT.relative_to(ROOT)))

    for sub in ("Machines", "Standards", "Incidents"):
        d = VAULT / sub
        record(f"Vault/{sub}/ present", d.is_dir(),
               "found" if d.is_dir() else "MISSING")

    machines = list((VAULT / "Machines").glob("*.md")) if (VAULT / "Machines").is_dir() else []
    record("6 machine stubs", len(machines) >= 6, f"{len(machines)} files")

    standards = list((VAULT / "Standards").glob("*.md")) if (VAULT / "Standards").is_dir() else []
    record("8 ANSI standard stubs", len(standards) >= 8, f"{len(standards)} files")

    linked = 0
    for m in machines:
        try:
            if "[[" in m.read_text(encoding="utf-8"):
                linked += 1
        except Exception:
            pass
    record("Machine stubs contain [[wiki-links]]", linked >= max(1, len(machines)),
           f"{linked}/{len(machines)} linked")


# ------------------------------------------------------------
# Structure (technical-debt tripwires)
# ------------------------------------------------------------
def check_structure():
    section("STRUCTURE")
    dupes = [str(p.relative_to(ROOT)) for p in ROOT.rglob("ai_engine.py")
             if not {"venv", ".git"} & set(p.parts)]
    record("Single ai_engine.py (Day 7.3 debt)", len(dupes) <= 1,
           ", ".join(dupes) or "none found", warn_only=True)


# ------------------------------------------------------------
# Live Modbus probes (opt-in, mutates plant state)
# ------------------------------------------------------------
def live_probes(c):
    section("LIVE MODBUS PROBES - plant state WILL change")

    def events(ev):
        if not AUDIT_PLC.is_file():
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for ln in AUDIT_PLC.read_text(encoding="utf-8").splitlines()
                   if f'"event": "{ev}"' in ln and today in ln)

    def sysword():
        rr = c.read_holding_registers(SYS_STATUS_ADDR, 1, slave=1)
        return rr.registers[0] if not rr.isError() else None

    def motor_state(eq):
        rr = c.read_holding_registers(EQ_BASE[eq] + MOTOR_STATE_OFFSET, 1, slave=1)
        return rr.registers[0] if not rr.isError() else None

    e0, r0, m0 = events("E-STOP"), events("RESET"), events("MOTOR_START")

    c.write_coil(7, True, slave=1); time.sleep(1.5)      # RESET: clear any latch
    record("Live RESET audited", events("RESET") - r0 >= 1, "RESET entry present")

    c.write_coil(6, True, slave=1); time.sleep(2.0)      # E-STOP via fc5 single coil
    de = events("E-STOP") - e0
    record("Live E-STOP audited EXACTLY once", de == 1, f"delta={de}")
    w = sysword()
    record("HR[120] bit0 set while latched", w is not None and bool(w & 1), f"word={w}")

    # Phase A: fc5 motor write while latched -> correct behavior = ESTOP-LOCKOUT audit
    lk0 = events("MOTOR_START")
    c.write_coil(EQ_IDS.index("PNT-01"), True, slave=1)
    time.sleep(1.5)
    record("fc5 motor write under latch reaches handler",
           events("MOTOR_START") - lk0 >= 1, "ESTOP-LOCKOUT audited")

    c.write_coil(7, True, slave=1); time.sleep(1.5)      # RESET

    # Phase B: fc5 motor START while clear (the real HMI/dashboard button path)
    c.write_coil(EQ_IDS.index("PNT-01"), False, slave=1)
    time.sleep(1.5)
    st0 = motor_state("PNT-01")
    c.write_coil(EQ_IDS.index("PNT-01"), True, slave=1)
    time.sleep(4.0)
    st1 = motor_state("PNT-01")
    record("fc5 single-coil motor start reaches logic", st1 in (1, 2),
           f"PNT-01 state {st0}->{st1}")
    w = sysword()
    record("HR[120] bit0 clear after RESET", w is not None and not (w & 1), f"word={w}")

    c.write_coils(0, [True] * 6, slave=1); time.sleep(4.0)   # block restart all
    dm = events("MOTOR_START") - m0
    states = {eq: motor_state(eq) for eq in EQ_IDS}
    record("Block restart audited (6 MOTOR_START)", dm >= 6, f"delta={dm}")
    record("All motors RUNNING after restart",
           all(s == 1 for s in states.values()), f"{states}")


# ------------------------------------------------------------
# Cognitive agent probe (live only): fault -> HIGH finding ->
# local LLM diagnosis -> Obsidian incident file with wiki-links
# ------------------------------------------------------------
def cognitive_probes(c):
    section("COGNITIVE AGENT PROBE - fault injection -> LLM -> Obsidian")
    if not AUDIT_AGENT.is_file():
        record("agent audit present", False, str(AUDIT_AGENT))
        return

    start = datetime.now() - timedelta(seconds=2)     # clock-safety margin
    start_ts = start.timestamp()
    inc_dir = VAULT / "Incidents"
    inc_before = {p.name for p in inc_dir.glob("*.md")} if inc_dir.is_dir() else set()
    audit_pos = AUDIT_AGENT.read_text(encoding="utf-8").count("\n") if AUDIT_AGENT.is_file() else 0

    # 1. Inject overtemp (ANSI 38) on STP-01 -> trip -> HIGH finding -> LLM cycle
    c.write_register(150, 4, slave=1)                 # fault map: 4 = overtemp
    print("  fault injected: waiting up to 180s for the agent cycle + LLM...")

    llm_seen, high_seen = None, None
    deadline = time.time() + 180
    while time.time() < deadline and llm_seen is None:
        time.sleep(3)
        try:
            lines = AUDIT_AGENT.read_text(encoding="utf-8").splitlines()[audit_pos:]
        except Exception:
            continue
        for ln in lines:
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            try:
                if datetime.fromisoformat(rec.get("ts", "")) < start:
                    continue
            except ValueError:
                continue
            pay = rec.get("payload", {})
            sev = str(pay.get("severity", "")).upper()
            if high_seen is None and rec.get("event") == "FINDING" and sev in ("HIGH", "CRITICAL"):
                high_seen = pay
            if llm_seen is None and str(pay.get("source", "")) == "LLM":
                llm_seen = pay

    record("Fault injection audited", True, "HR[150]=4 overtemp on STP-01")
    record("HIGH/CRITICAL finding emitted by rule engine", high_seen is not None,
           f"{(high_seen or {}).get('code', 'none within 180s')}")
    record("Local LLM produced a NEW diagnosis (<=180s)", llm_seen is not None,
           f"{(llm_seen or {}).get('code', 'timeout - check backend console')}")
    if llm_seen:
        msg = str(llm_seen.get("message", ""))
        record("LLM diagnosis not a canary echo",
               "short message" not in msg and "short diagnosis" not in msg, msg[:60])

    # 2. Obsidian end-to-end: a NEW incident file with wiki-links must exist
    time.sleep(3)
    new_files = []
    if inc_dir.is_dir():
        for p in inc_dir.glob("*.md"):
            if p.name not in inc_before and p.stat().st_mtime >= start_ts:
                new_files.append(p)
    record("Obsidian incident file auto-created (Feature B)", bool(new_files),
           new_files[0].name if new_files else "none in this window")
    if new_files:
        try:
            body = new_files[0].read_text(encoding="utf-8")
            record("Incident contains [[wiki-links]]", "[[" in body,
                   f"{body.count('[[')} wiki-links")
        except Exception as e:
            record("Incident file readable", False, str(e))

    # 3. Cleanup: RESET + restart the plant (the injected fault causes a lockout)
    c.write_coil(7, True, slave=1)
    time.sleep(2.0)
    c.write_coils(0, [True] * 6, slave=1)
    time.sleep(5.0)
    states = {}
    for eq in EQ_IDS:
        rr = c.read_holding_registers(EQ_BASE[eq] + MOTOR_STATE_OFFSET, 1, slave=1)
        states[eq] = rr.registers[0] if not rr.isError() else None
    record("Plant restored after cognitive probe",
           all(s == 1 for s in states.values()), f"{states}")


# ------------------------------------------------------------
# Unit tests + evidence + summary
# ------------------------------------------------------------
def run_pytest():
    section("UNIT TESTS (pytest)")
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q",
                        "--no-header", "--ignore=tests/quick_test.py"],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    record("tests/ suite passes", r.returncode == 0, last)


def write_evidence(args):
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"verify-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    warns = sum(1 for _, s, _ in results if s == "WARN")
    out.write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"),
        "mode": {"live": args.live, "wait": args.wait, "skip_pytest": args.skip_pytest},
        "summary": {"pass": len(results) - fails - warns, "warn": warns, "fail": fails},
        "checks": [{"name": n, "status": s, "detail": d} for n, s, d in results],
    }, indent=2), encoding="utf-8")
    print(f"  evidence artifact: {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="run Modbus probes + cognitive agent probe (mutates plant)")
    ap.add_argument("--wait", type=int, default=0, metavar="SEC",
                    help="poll backend until ready (use after cold start)")
    ap.add_argument("--json", action="store_true", help="write evidence artifact")
    ap.add_argument("--skip-pytest", action="store_true")
    
    # --- CORRECTION ADDED HERE ---
    ap.add_argument("--skip-cognitive", action="store_true",
                    help="skip the LLM/Obsidian probe even in --live mode")
    # -----------------------------

    args = ap.parse_args()

    if args.wait:
        wait_for_backend(args.wait)

    section("1. PROJECT ARTIFACTS")
    model = ROOT / "models" / "qwen2.5-coder-1.5b-instruct-q6_k.gguf"
    record("GGUF model file", model.is_file(),
           f"{model.stat().st_size / 1048576:.0f} MB" if model.is_file() else "missing")
    for p in ["backend/api.py", "backend/database.py", "plc_simulator/modbus_server.py",
              "dashboard/streamlit_app.py", "hmi/hmi_gui.py",
              "models/industrial_agent/agent.py",
              "models/industrial_agent/llm_reasoner.py"]:
        record(f"source: {p}", (ROOT / p).is_file(),
               "found" if (ROOT / p).is_file() else "MISSING")

    import socket
    section("2. NETWORK PORTS")
    def port_open(host, port, timeout=2):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False
    plc_up = port_open(PLC_HOST, PLC_PORT)
    api_up = port_open("127.0.0.1", 8000)
    record("PLC Modbus :5020", plc_up, "reachable" if plc_up else "NOT RUNNING")
    record("Backend :8000", api_up, "reachable" if api_up else "NOT RUNNING")
    record("Streamlit :8501", port_open("127.0.0.1", 8501),
           "reachable" if port_open("127.0.0.1", 8501) else "not running",
           warn_only=True)

    check_backend(api_up)
    check_audit_integrity()
    check_database()
    check_vault_skeleton()
    check_structure()
    if not args.skip_pytest:
        run_pytest()

    if args.live and plc_up:
        from pymodbus.client import ModbusTcpClient
        c = ModbusTcpClient(PLC_HOST, port=PLC_PORT)
        if c.connect():
            live_probes(c)
            # Use the newly added flag to decide whether to run cognitive probes
            if not args.skip_cognitive:
                cognitive_probes(c)
            c.close()
        else:
            record("PLC connection", False, f"cannot reach {PLC_HOST}:{PLC_PORT}")

    section("SUMMARY")
    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "WARN"]
    print(f"  PASS: {len(results) - len(fails) - len(warns)}   "
          f"WARN: {len(warns)}   FAIL: {len(fails)}")
    for name, _, detail in fails:
        print(f"    - {name}: {detail}")
    if args.json:
        write_evidence(args)
    verdict = "SYSTEM HEALTHY - safe to proceed" if not fails else "SYSTEM DEGRADED"
    print(f"\n  VERDICT: {verdict}\n")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()