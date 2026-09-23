#!/usr/bin/env bash
# ============================================================
# fix_kit.sh - E-STOP / RESET audit: diagnose + repair + verify
# Usage:
#   bash tools/fix_kit.sh diagnose   # READ-ONLY diagnostics (paste output to mentor)
#   bash tools/fix_kit.sh apply      # 3 PLC fixes (auto-backup, anchored, syntax-checked)
#   bash tools/fix_kit.sh verify     # RESET->E-STOP->RESET via single coils, audit the audit log
# ============================================================
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
[ "$(basename "$ROOT")" = "tools" ] && ROOT="$(dirname "$ROOT")"
cd "$ROOT" || exit 1
echo "project root: $ROOT"

command -v python >/dev/null 2>&1 || { echo "ERROR: python not found - activate venv first"; exit 1; }

AUDIT_NEW="plc_simulator/audit_log.jsonl"
AUDIT_ROOT="audit_log.jsonl"

diagnose() {
  echo "############ E-STOP DIAGNOSTIC KIT - $(date '+%F %T') ############"

  echo; echo "=== [1] all audit_log.jsonl files ==="
  find . -name "audit_log.jsonl" -not -path "./venv/*" -exec ls -la {} \; 2>/dev/null

  echo; echo "=== [2] event counts per file ==="
  find . -name "audit_log.jsonl" -not -path "./venv/*" 2>/dev/null | while read -r f; do
    echo "--- $f"
    echo "  E-STOP:       $(grep -c '"event": "E-STOP"' "$f")"
    echo "  RESET:        $(grep -c '"event": "RESET"' "$f")"
    echo "  FAULT_INJECT: $(grep -c '"event": "FAULT_INJECT"' "$f")"
  done

  echo; echo "=== [3] tail of ROOT audit file (live for the unpatched PLC) ==="
  tail -5 "$AUDIT_ROOT" 2>/dev/null || echo "(missing)"

  echo; echo "=== [4] 'Emergency stop' print sites ==="
  grep -rn "Emergency stop" plc_simulator/ --include="*.py" || echo "(none)"

  echo; echo "=== [5] emergency_stop_all call sites ==="
  grep -rn "emergency_stop_all" plc_simulator/ --include="*.py" || echo "(none)"

  echo; echo "=== [6] estop_latched uses ==="
  grep -rn "estop_latched" plc_simulator/ --include="*.py" || echo "(none)"

  echo; echo "=== [7] HMI coil writes ==="
  grep -n "write_coil\|write_coils\|coil" hmi/hmi_gui.py 2>/dev/null | head -40 || echo "(no hmi_gui.py)"

  echo; echo "=== [8] Dashboard writes (Streamlit reruns re-execute top-level code!) ==="
  grep -n "write_coil\|write_coils\|write_register" dashboard/streamlit_app.py 2>/dev/null | head -40 || echo "(none)"

  echo; echo "=== [9] LLM placeholder leak ('short message' / 'short diagnosis') ==="
  grep -rn --include="*.py" -e "short message" -e "short diagnosis" . 2>/dev/null | grep -v "./venv/" || echo "(not found)"

  echo; echo "############ END DIAGNOSTICS - paste everything above ############"
}

apply() {
  echo "### APPLY: 3 PLC fixes (backup first)"
  BAK="plc_simulator/modbus_server.py.bak-$(date +%Y%m%d-%H%M%S)"
  cp plc_simulator/modbus_server.py "$BAK" || { echo "ABORT: cannot back up"; exit 1; }
  echo "backup: $BAK"

  grep -qxF "*.bak-*" .gitignore 2>/dev/null || echo "*.bak-*" >> .gitignore

  python - <<'PYEOF'
import sys, pathlib
p = pathlib.Path("plc_simulator/modbus_server.py")
try:
    src = p.read_text(encoding="utf-8")
except UnicodeDecodeError as e:
    sys.exit(f"ABORT: cannot decode as utf-8: {e}")

def patch(s, old, new, label):
    n = s.count(old)
    if n != 1:
        sys.exit(f"ABORT: anchor '{label}' matched {n} times (expected 1). Do not force it - send diagnose output to the mentor.")
    print(f"  patched: {label}")
    return s.replace(old, new)

src = patch(src,
    'AUDIT_LOG_PATH = Path("audit_log.jsonl")',
    'AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit_log.jsonl"',
    "FIX1 pin audit log path")

src = patch(src,
'''        # --- Coil Writes (FC 1/5/15) ---
        if fc in (1, 5, 15) and address == 0:
            asyncio.ensure_future(self._handle_coil_write(values))''',
'''        # --- Coil Writes (FC 1/5/15) ---
        if fc in (1, 5, 15) and address == 0:
            asyncio.ensure_future(self._handle_coil_write(values))
        elif fc == 5 and address in (6, 7):
            # Single-coil write (fc5) to E-STOP/RESET: route into the block handler
            block = [0] * 8
            block[address] = values[0]
            asyncio.ensure_future(self._handle_coil_write(block))''',
    "FIX2 accept single-coil E-STOP/RESET writes")

src = patch(src,
'''        if len(coil_values) > 6 and coil_values[6]:
            plc.emergency_stop_all()
            # Auto-clear the E-STOP coil
            super().setValues(1, 6, [0])
            await audit_log("E-STOP", source_ip, 6, 0, 1, "LATCHED")
            return''',
'''        if len(coil_values) > 6 and coil_values[6]:
            if not plc.estop_latched:
                plc.emergency_stop_all()
                await audit_log("E-STOP", source_ip, 6, 0, 1, "LATCHED")
            # Always consume the request - prevents repeat-trigger floods
            super().setValues(1, 6, [0])
            return''',
    "FIX3 idempotent E-STOP latch (anti-flood)")

p.write_text(src, encoding="utf-8")
print("PATCH OK: 3 fixes written (all-or-nothing; nothing written on abort)")
PYEOF
  RC=$?
  [ $RC -ne 0 ] && { echo "apply FAILED (exit $RC) - original untouched"; exit 1; }

  python -m py_compile plc_simulator/modbus_server.py \
    && echo "SYNTAX OK" \
    || { echo "SYNTAX FAILED - restore: cp $BAK plc_simulator/modbus_server.py"; exit 1; }

  echo; echo "changed lines:"
  grep -n "AUDIT_LOG_PATH\|elif fc == 5\|estop_latched:" plc_simulator/modbus_server.py | head
  echo; echo "NEXT: restart the stack (close all NEXUS windows -> run_all.bat), then: bash tools/fix_kit.sh verify"
}

verify() {
  echo "### VERIFY: RESET -> E-STOP -> RESET via single-coil (fc5) writes"
  python - <<'PYEOF'
from pymodbus.client import ModbusTcpClient
import time
c = ModbusTcpClient('127.0.0.1', port=5020)
if not c.connect():
    raise SystemExit("FAIL: PLC unreachable on 127.0.0.1:5020 - is the patched stack running?")
for addr, name in [(7, "RESET"), (6, "E-STOP"), (7, "RESET")]:
    r = c.write_coil(addr, True, slave=1)
    print(f"sent {name}: {r}")
    time.sleep(2)
c.close()
print("modbus sequence done")
PYEOF
  sleep 1
  TODAY=$(date +%F)
  E=$(grep -c "\"ts\": \"$TODAY.*\"event\": \"E-STOP\"" "$AUDIT_NEW" 2>/dev/null); E=${E:-0}
  R=$(grep -c "\"ts\": \"$TODAY.*\"event\": \"RESET\"" "$AUDIT_NEW" 2>/dev/null); R=${R:-0}
  echo; echo "today's entries in $AUDIT_NEW:  E-STOP=$E  RESET=$R"
  echo "expected on first run: E-STOP=1, RESET=2 (first RESET may log WAS-CLEAR)"
  tail -3 "$AUDIT_NEW" 2>/dev/null || echo "(no audit file yet!)"
  echo; echo "ALSO check the NEXUS-1 console: exactly ONE set of 6 [E-STOP] lines - no flood."
}

case "${1:-}" in
  diagnose) diagnose ;;
  apply)    apply ;;
  verify)   verify ;;
  *) echo "usage: bash tools/fix_kit.sh {diagnose|apply|verify}" ;;
esac