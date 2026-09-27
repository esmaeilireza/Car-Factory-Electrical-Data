#!/usr/bin/env python3
"""
Diagnostic and Auto-Patch Script for Probe Logic Failure (Windows Compatible).
Fixes the 'no HIGH finding within 180s' error caused by ISO timestamp precision collisions.
"""

import pathlib
import shutil
import subprocess
import sys
from datetime import datetime

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
TARGET_FILE = pathlib.Path("tests/quick_test.py")
AUDIT_LOG = pathlib.Path("data/agent_audit.jsonl")
PLC_LOG = pathlib.Path("plc_simulator/audit_log.jsonl")

def check_diagnostics():
    """Read files directly to diagnose if findings exist but were filtered out."""
    print("\n--- DIAGNOSTIC PHASE ---")
    
    # Check Agent Audit Log
    if AUDIT_LOG.exists():
        try:
            content = AUDIT_LOG.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            
            total_ansi = sum(1 for l in lines if "ANSI_ALARM" in l)
            print(f"[Audit] Total ANSI_ALARM entries found: {total_ansi}")
            
            recent_lines = lines[-40:] if len(lines) > 40 else lines
            high_findings = sum(1 for l in recent_lines if "TRIP" in l or "LOCKOUT" in l)
            print(f"[Audit] Recent HIGH findings (last 40 lines): {high_findings}")
            
        except Exception as e:
            print(f"[Error] Could not read audit logs: {e}")
    else:
        print("[Warning] data/agent_audit.jsonl does not exist.")

    # Check PLC Simulator Log
    if PLC_LOG.exists():
        try:
            content = PLC_LOG.read_text(encoding="utf-8", errors="ignore")
            lines = [l for l in content.splitlines() if "FAULT_INJECT" in l]
            plc_lines = lines[-2:] if len(lines) >= 2 else lines
            print(f"[PLC] Last fault injection events:")
            for l in plc_lines:
                print(f"   -> {l[:100]}...") 
        except Exception as e:
            print(f"[Warning] Could not read PLC logs: {e}")
    else:
        print("[Warning] plc_simulator/audit_log.jsonl does not exist.")

def apply_patch():
    """Apply the line-count watermark fix to quick_test.py."""
    print("\n--- PATCH PHASE ---")
    
    if not TARGET_FILE.exists():
        print(f"ABORT: File {TARGET_FILE} does not exist.")
        return False

    src = TARGET_FILE.read_text(encoding="utf-8")
    
    def patch_content(old_snippet, new_snippet, label):
        nonlocal src
        if new_snippet in src:
            print(f"  Skipped: {label} (already applied)")
            return True
        
        if old_snippet not in src:
            print(f"  WARNING: Anchor for '{label}' not found. Skipping this specific hunk.")
            return False
            
        backup_path = f"{TARGET_FILE}.bak-{STAMP}"
        if not pathlib.Path(backup_path).exists():
            shutil.copy(TARGET_FILE, backup_path)
            print(f"  Created backup: {backup_path}")

        src = src.replace(old_snippet, new_snippet)
        print(f"  Patched: {label}")
        return True

    success = True
    
    # 1. Replace clock-based start calculation with line-count watermark
    old_1 = '''    start = datetime.now() - timedelta(seconds=2)     # clock-safety margin
    start_ts = start.timestamp()'''
    
    new_1 = '''    # Count-based watermark instead of clock comparison: ISO timestamps in
    # the audit chain have second precision and can collide with the probe
    # start, causing findings to be filtered out. Line count is monotonic.
    AUDIT_AGENT_PATH = pathlib.Path("data/agent_audit.jsonl")
    audit_start_line = AUDIT_AGENT_PATH.read_text(encoding='utf-8').count('\\n') if AUDIT_AGENT_PATH.is_file() else 0
    start_ts = time.time() - 2'''
    
    if not patch_content(old_1, new_1, "Replace clock filter with line-count watermark"):
        success = False

    # 2. Update reading logic to use the new watermark variable
    old_2 = '''        try:
            lines = AUDIT_AGENT.read_text(encoding="utf-8").splitlines()[audit_pos:]'''
            
    new_2 = '''        try:
            lines = AUDIT_AGENT.read_text(encoding="utf-8").splitlines()[audit_start_line:]'''
            
    if not patch_content(old_2, new_2, "Use line watermark when reading new audit lines"):
        success = False

    # 3. Remove stale audit_pos definition if it exists elsewhere
    old_3 = '''    audit_pos = AUDIT_AGENT.read_text(encoding="utf-8").count("\\n") if AUDIT_AGENT.is_file() else 0'''
    new_3 = '''    # (audit_pos removed - replaced by audit_start_line above)'''
    
    patch_content(old_3, new_3, "Remove stale audit_pos variable")

    if success:
        TARGET_FILE.write_text(src, encoding="utf-8")
        print("PATCH APPLIED SUCCESSFULLY")
        
        compile_res = subprocess.run([sys.executable, "-m", "py_compile", str(TARGET_FILE)], capture_output=True)
        if compile_res.returncode == 0:
            print("SYNTAX CHECK: OK")
            return True
        else:
            print("SYNTAX ERROR after patch! Reverting...")
            shutil.move(f"{TARGET_FILE}.bak-{STAMP}", TARGET_FILE)
            return False
    else:
        print("PATCH INCOMPLETE OR FAILED ANCHOR MATCHES.")
        return False

if __name__ == "__main__":
    check_diagnostics()
    
    if apply_patch():
        print("\n>>> Next Steps:")
        print("1. Restart the Backend completely (Kill process, don't just reload).")
        print("2. Run the test again: python tests/quick_test.py --wait 90 --live --skip-pytest --json")
        print("3. If PASS -> Commit changes.")
        print("4. If FAIL -> Paste the diagnostic output from step 1 into GitHub Issue.")
    else:
        print("\n>>> Manual Intervention Required: Check anchors in tests/quick_test.py")
