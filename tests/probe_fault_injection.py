#!/usr/bin/env python3
"""
probe_fault_injection.py v7 - Robust Diagnostic
Confirms arm/fault via Audit Log, NOT register readback.
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    print("FATAL: pymodbus not installed. Run: pip install pymodbus")
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PLC = ROOT / "plc_simulator" / "audit_log.jsonl"
SRC = ROOT / "plc_simulator" / "modbus_server.py"

HOST, PORT = "127.0.0.1", 5020

def get_const(name: str, default: int) -> int:
    try:
        text = SRC.read_text(encoding="utf-8", errors="ignore")
        m = re.search(rf"^{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", text, re.M)
        return int(m.group(1), 0) if m else default
    except Exception:
        return default

ARM_REG = get_const("SAFETY_ARM_REGISTER", 199)
ARM_VAL = get_const("SAFETY_ARM_VALUE", 0xA5A5)
FAULT_BASE = get_const("FAULT_INJECT_BASE", 150)
LOAD_BASE = get_const("LOAD_SETPOINT_BASE", 160)

# Target UTI-01 by default (Index 4)
TARGET_IDX = int(os.environ.get("NEXUS_PROBE_TARGET_IDX", "4"))
FAULT_REG = FAULT_BASE + TARGET_IDX
CTRL_REG = LOAD_BASE + TARGET_IDX

FAULT_CODE = int(os.environ.get("NEXUS_PROBE_FAULT_CODE", "1")) # 1=thermal
CTRL_VAL = int(os.environ.get("NEXUS_PROBE_CTRL_VAL", "80"))

TIMEOUT = float(os.environ.get("NEXUS_PROBE_TIMEOUT", "5.0"))

def audit_count() -> int:
    if not AUDIT_PLC.is_file(): return 0
    try: return AUDIT_PLC.read_text(errors="ignore").count("\n")
    except: return 0

def wait_audit(start_pos: int, events: Set[str], timeout: float, addr: Optional[int]=None, val: Optional[int]=None) -> Optional[Dict]:
    deadline = time.time() + timeout
    pos = start_pos
    while time.time() < deadline:
        try:
            lines = AUDIT_PLC.read_text(errors="ignore").splitlines()
        except: lines = []
        
        if pos > len(lines): pos = 0
        
        for ln in lines[pos:]:
            ln = ln.strip()
            if not ln: continue
            try: rec = json.loads(ln)
            except: continue
            
            ev = str(rec.get("event", ""))
            if ev not in events: continue
            
            r_addr = rec.get("address", rec.get("register"))
            if addr is not None and r_addr != addr: continue
            
            r_val = rec.get("new_value")
            if val is not None and r_val != val: continue
            
            return rec
        pos = len(lines)
        time.sleep(0.2)
    return None

def main():
    print(f"Target: Index {TARGET_IDX} | FaultReg HR[{FAULT_REG}] | CtrlReg HR[{CTRL_REG}]")
    
    client = ModbusTcpClient(HOST, port=PORT)
    if not client.connect():
        print(f"[FAIL] Cannot connect to PLC {HOST}:{PORT}")
        return 1
    
    try:
        # 1. Arm Check
        pos = audit_count()
        ok = client.write_register(ARM_REG, ARM_VAL, slave=1)
        armed_rec = wait_audit(pos, {"SAFETY_ARMED", "SAFETY_ARM_REJECTED"}, TIMEOUT)
        
        if not armed_rec or armed_rec.get("event") != "SAFETY_ARMED":
            print(f"[FAIL] Arm rejected/not confirmed. Rec: {armed_rec}")
            return 1
        print("[PASS] Safety Armed Confirmed.")
        
        # 2. Control Write Check
        pos = audit_count()
        client.write_register(CTRL_REG, CTRL_VAL, slave=1)
        ctrl_rec = wait_audit(pos, {"LOAD_SETPOINT_WRITE", "SAFETY_WRITE_BLOCKED"}, TIMEOUT, addr=CTRL_REG)
        
        if not ctrl_rec or ctrl_rec.get("event") != "LOAD_SETPOINT_WRITE":
            print(f"[FAIL] Control write blocked/no audit. Rec: {ctrl_rec}")
            return 1
        print("[PASS] Control Write Confirmed.")
        
        # 3. Fault Injection Check
        pos = audit_count()
        client.write_register(FAULT_REG, FAULT_CODE, slave=1)
        fault_rec = wait_audit(pos, {"FAULT_INJECT", "SAFETY_WRITE_BLOCKED"}, TIMEOUT + 2, addr=FAULT_REG, val=FAULT_CODE)
        
        if not fault_rec:
            print("[FAIL] No fault audit event received.")
            return 1
            
        if fault_rec.get("event") == "FAULT_INJECT":
            print(f"[PASS] Fault Injection Confirmed! Result: {fault_rec.get('result')}")
            print("NOTE: Register auto-clears to 0. Readback is irrelevant.")
            return 0
        else:
            print(f"[FAIL] Fault Blocked. Rec: {fault_rec}")
            return 1
            
    finally:
        client.close()

if __name__ == "__main__":
    sys.exit(main())
