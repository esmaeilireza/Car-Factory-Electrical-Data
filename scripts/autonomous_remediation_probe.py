#!/usr/bin/env python3
"""
NEXUS SCADA - Autonomous Remediation Probe (Final Version)
Uses HR[199]=0xA5A5 Safety Arm Handshake.
Confirms injection via PLC Audit Log, not register readback.
"""
import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from pymodbus.client import ModbusTcpClient
except Exception as e:
    print(f"FATAL: PyModbus unavailable: {e}")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "http://localhost:8000"
PLC_HOST, PLC_PORT = "127.0.0.1", 5020
AUDIT_PLC = ROOT / "plc_simulator" / "audit_log.jsonl"
AUDIT_AGENT = ROOT / "data" / "agent_audit.jsonl"
VAULT = ROOT / "data" / "scada_vault"

# --- PLC Constants (From Source Code Analysis) ---
SAFETY_ARM_REG = 199
SAFETY_ARM_VAL = 0xA5A5
FAULT_BASE = 150
LOAD_BASE = 160
MOTOR_OFF = 8

DEFAULT_EQ = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]


def rh(c: Any, addr: int) -> Optional[int]:
    """Read Holding Register with signature tolerance."""
    attempts = [
        lambda: c.read_holding_registers(addr, 1, slave=1),
        lambda: c.read_holding_registers(addr, 1, unit_id=1),
        lambda: c.read_holding_registers(addr, 1),
    ]
    for fn in attempts:
        try:
            r = fn()
            if hasattr(r, "isError") and r.isError():
                continue
            regs = getattr(r, "registers", None)
            if regs is not None:
                return int(regs[0])
            if isinstance(r, int):
                return r
        except Exception:
            continue
    return None


def wh(c: Any, addr: int, value: int) -> bool:
    """Write Holding Register with signature tolerance."""
    attempts = [
        lambda: c.write_register(addr, int(value), slave=1),
        lambda: c.write_register(addr, int(value), unit_id=1),
        lambda: c.write_register(addr, int(value)),
    ]
    for fn in attempts:
        try:
            r = fn()
            if not (hasattr(r, "isError") and r.isError()):
                return True
        except Exception:
            continue
    return False


def wc(c: Any, addr: int, value: bool) -> bool:
    """Write Coil with signature tolerance."""
    attempts = [
        lambda: c.write_coil(addr, bool(value), slave=1),
        lambda: c.write_coil(addr, bool(value), unit_id=1),
        lambda: c.write_coil(addr, bool(value)),
    ]
    for fn in attempts:
        try:
            r = fn()
            if not (hasattr(r, "isError") and r.isError()):
                return True
        except Exception:
            continue
    return False


def wcs(c: Any, start: int, values: List[bool]) -> bool:
    """Write Coils with signature tolerance."""
    attempts = [
        lambda: c.write_coils(start, values, slave=1),
        lambda: c.write_coils(start, values, unit_id=1),
        lambda: c.write_coils(start, values),
    ]
    for fn in attempts:
        try:
            r = fn()
            if not (hasattr(r, "isError") and r.isError()):
                return True
        except Exception:
            continue
    return False


def get_line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return path.read_text(encoding="utf-8", errors="ignore").count("\n")
    except Exception:
        return 0


def wait_for_plc_audit_event(
    start_line: int,
    event_name: str,
    timeout: float = 5.0,
    match_func: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Polls the PLC audit log JSONL for a specific event."""
    deadline = time.time() + timeout
    pos = start_line

    while time.time() < deadline:
        try:
            lines = AUDIT_PLC.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            lines = []

        if pos > len(lines):
            pos = 0

        for ln in lines[pos:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue

            if str(rec.get("event", "")) == event_name:
                if match_func is None or match_func(rec):
                    return rec
        
        # Update position to avoid re-scanning same lines next loop
        pos = len(lines)
        time.sleep(0.5)

    return None


def arm_and_inject_fault(c: Any, fault_addr: int, fault_code: int) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Performs the Safety Arm Handshake and injects the fault.
    Returns (success, plc_audit_record).
    """
    # 1. Arm Safety Channel
    print(f"  [ARM] Writing 0x{SAFETY_ARM_VAL:X} to HR[{SAFETY_ARM_REG}]...")
    ok_arm = wh(c, SAFETY_ARM_REG, SAFETY_ARM_VAL)
    if not ok_arm:
        print("  [ARM] Failed to write arm register.")
        return False, None
    
    time.sleep(0.2) # Small delay to ensure server processes arm

    # 2. Capture Audit Position BEFORE injection
    audit_pos = get_line_count(AUDIT_PLC)

    # 3. Inject Fault
    print(f"  [INJ] Writing fault code {fault_code} to HR[{fault_addr}]...")
    ok_inj = wh(c, fault_addr, fault_code)
    
    if not ok_inj:
        print("  [INJ] Write command rejected by Modbus layer.")
        return False, None

    # 4. Wait for FAULT_INJECT event in Audit Log
    # This confirms the PLC accepted it logically, even if it auto-cleared the register.
    print("  [WAIT] Checking PLC audit log for FAULT_INJECT...")
    record = wait_for_plc_audit_event(
        start_line=audit_pos,
        event_name="FAULT_INJECT",
        timeout=3.0,
        match_func=lambda r: int(r.get("address", -1)) == fault_addr and int(r.get("new_value", -1)) == fault_code
    )

    if record:
        print(f"  [OK] PLC Confirmed Injection: {json.dumps(record)[:150]}")
        return True, record
    else:
        # Check if it was blocked
        blocked = wait_for_plc_audit_event(
            start_line=audit_pos,
            event_name="SAFETY_WRITE_BLOCKED",
            timeout=1.0,
            match_func=lambda r: int(r.get("register", -1)) == fault_addr
        )
        if blocked:
            print(f"  [BLOCKED] PLC Rejected Injection: {json.dumps(blocked)[:150]}")
        else:
            print("  [UNKNOWN] No audit confirmation found.")
        
        return False, None


def reset_and_restart_armed(c: Any, device_count: int, settle: float = 4.0) -> None:
    """Performs RESET and Block Restart using Safety Arm."""
    # Arm for Reset Coil
    wh(c, SAFETY_ARM_REG, SAFETY_ARM_VAL)
    time.sleep(0.1)
    wc(c, 7, True) # RESET coil
    
    time.sleep(1.0)
    
    # Block Restart Motors (Coils 0-5)
    # Note: Motor starts usually don't require arm unless specifically configured, 
    # but doing it safely doesn't hurt.
    wcs(c, 0, [True] * device_count)
    time.sleep(settle)


def discover_eq_ids() -> List[str]:
    try:
        d = requests.get(f"{BACKEND}/api/equipment", timeout=8).json().get("equipment", {})
        ids = [k for k, v in d.items() if isinstance(v, dict)]
        if ids:
            return ids
    except Exception:
        pass
    return DEFAULT_EQ[:]


def set_backend_mode(mode: str) -> bool:
    try:
        r = requests.post(f"{BACKEND}/api/agent/remediation/mode", json={"mode": mode}, timeout=8)
        if r.ok and r.json().get("ok"):
            return True
    except Exception:
        pass
    return False


def wait_agent_chain(eq_id: str, start_pos: int, timeout: float) -> Tuple[Optional[Dict], Optional[Dict], List[Dict]]:
    decision = None
    execution = None
    all_records = []
    pos = start_pos
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            lines = AUDIT_AGENT.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            lines = []
        
        if pos > len(lines):
            pos = 0
            
        new_lines = lines[pos:]
        pos = len(lines)
        
        for ln in new_lines:
            ln = ln.strip()
            if not ln: continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
                
            ev = str(rec.get("event", ""))
            pay = rec.get("payload", {}) or {}
            
            all_records.append(rec)

            if decision is None and ev == "REMEDIATION_DECISION":
                if str(pay.get("eq_id", "")) == eq_id:
                    decision = rec
                    
            if execution is None and ev in {"AUTO_REMEDIATION_EXECUTED", "REMEDIATION_NOT_EXECUTED"}:
                dec_inner = pay.get("decision", {}) or {}
                if str(dec_inner.get("eq_id", "")) == eq_id or str(pay.get("eq_id", "")) == eq_id:
                    execution = rec
                    
            if decision and execution:
                return decision, execution, all_records
                
        time.sleep(2)
        
    return decision, execution, all_records


def find_obsidian_incident(eq_id: str, start_ts: float, timeout: float = 60.0) -> Optional[Path]:
    inc_dir = VAULT / "Incidents"
    if not inc_dir.is_dir():
        return None
        
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            files = list(inc_dir.glob("*.md"))
        except Exception:
            files = []
            
        candidates = []
        for p in files:
            try:
                if p.stat().st_mtime < start_ts - 10:
                    continue
                body = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
                
            if eq_id in body and "Autonomous Remediation" in body:
                candidates.append((p.stat().st_mtime, p))
                
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
            
        time.sleep(3)
        
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eq", default="UTI-01")
    ap.add_argument("--fault-codes", default="1,4,2,3")
    ap.add_argument("--set-load", type=int, default=80)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    ids = discover_eq_ids()
    if args.eq not in ids:
        print(f"ERROR: Equipment {args.eq} not found. Available: {ids}")
        return 1

    idx = ids.index(args.eq)
    fault_addr = FAULT_BASE + idx
    load_addr = LOAD_BASE + idx
    codes = [int(x) for x in args.fault_codes.split(",")]

    print(f"Target: {args.eq} (Idx={idx})")
    print(f"Fault Reg: HR[{fault_addr}], Load Reg: HR[{load_addr}]")
    
    # 1. Set Backend Mode
    if not set_backend_mode("limited_autonomous"):
        print("WARNING: Could not set backend mode to limited_autonomous. Proceeding anyway.")
    else:
        print("Backend Mode: limited_autonomous")

    # 2. Connect PLC
    c = ModbusTcpClient(PLC_HOST, port=PLC_PORT)
    if not c.connect():
        print("FATAL: Cannot connect to PLC.")
        return 1

    try:
        # 3. Initialize Plant
        print("\nInitializing Plant (Armed Reset)...")
        reset_and_restart_armed(c, len(ids))
        
        # 4. Set Baseline Load
        wh(c, load_addr, args.set_load)
        time.sleep(1.0)
        baseline = rh(c, load_addr)
        print(f"Baseline Load: {baseline}")
        
        if baseline is None or baseline <= 0:
             print("ERROR: Could not set/read baseline load.")
             return 1

        success_found = False
        best_score = 0
        best_details = {}

        # 5. Try Fault Codes
        for code in codes:
            print(f"\n--- Attempting Fault Code {code} ---")
            
            # Reset plant before each attempt to clear previous state
            reset_and_restart_armed(c, len(ids), settle=2.0)
            wh(c, load_addr, args.set_load) # Restore load
            time.sleep(0.5)
            
            agent_start_pos = get_line_count(AUDIT_AGENT)
            start_ts = time.time()
            
            # Inject Fault
            inj_ok, inj_rec = arm_and_inject_fault(c, fault_addr, code)
            
            if not inj_ok:
                print("Skipping: Fault injection failed or blocked.")
                continue
                
            # Wait for Agent Chain
            print("Waiting for Agent Cognitive Chain...")
            decision, execution, agent_records = wait_agent_chain(args.eq, agent_start_pos, args.timeout)
            
            executed = False
            load_changed = False
            obsidian_ok = False
            
            if decision:
                print(f"PASS: REMEDIATION_DECISION observed. Approved: {decision['payload'].get('approved_actions')}")
            else:
                print("FAIL: No REMEDIATION_DECISION.")
                
            if execution:
                payload = execution.get("payload", {})
                executed = bool(payload.get("executed"))
                print(f"INFO: Execution Event: {execution['event']}, Executed: {executed}")
            else:
                print("FAIL: No Execution Event.")
                
            # Check Load Change
            time.sleep(2)
            new_load = rh(c, load_addr)
            if new_load is not None and baseline is not None:
                load_changed = new_load < baseline
                print(f"Load Check: Baseline={baseline}, New={new_load}, Reduced={load_changed}")
                
            # Check Obsidian
            inc_path = find_obsidian_incident(args.eq, start_ts, timeout=30.0)
            if inc_path:
                obsidian_ok = True
                print(f"Obsidian: Found Incident {inc_path.name}")
            else:
                print("Obsidian: Not found within timeout.")
                
            score = int(bool(decision)) + int(bool(execution)) + int(executed) + int(load_changed) + int(obsidian_ok)
            
            if score > best_score:
                best_score = score
                best_details = {
                    "code": code,
                    "decision": decision,
                    "execution": execution,
                    "executed": executed,
                    "load_changed": load_changed,
                    "obsidian": obsidian_ok,
                    "new_load": new_load
                }
                
            if score >= 4: # Good enough for prototype proof
                print(">>> SUCCESS THRESHOLD MET <<<")
                break

        # 6. Final Report
        print("\n" + "="*50)
        print("FINAL RESULT")
        print("="*50)
        if best_score >= 4:
            print(f"VERDICT: PASS (Score {best_score}/5)")
            print(f"Best Fault Code: {best_details['code']}")
            print(f"Executed: {best_details['executed']}")
            print(f"Load Reduced: {best_details['load_changed']}")
            print(f"Obsidian Updated: {best_details['obsidian']}")
            return 0
        else:
            print(f"VERDICT: FAIL (Max Score {best_score}/5)")
            if best_details:
                 print(f"Details: {json.dumps(best_details, indent=2, default=str)}")
            return 1

    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())