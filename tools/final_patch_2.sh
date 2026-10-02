#!/usr/bin/env bash
# =============================================================================
#  final_patch_2.sh — P1 (audit-confirmed write check), P3 (window widening),
#                      P4 (alarm_events sink wiring)
#  Root-anchored, backup-first, compile-gated, idempotent.
# =============================================================================
set -uo pipefail
GREEN='\033[92m'; RED='\033[91m'; CYAN='\033[96m'; RESET='\033[0m'
pass() { echo -e "${GREEN}[PASS]${RESET} $1"; }
fail() { echo -e "${RED}[FAIL]${RESET} $1"; }
info() { echo -e "${CYAN}[INFO]${RESET} $1"; }

SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"; done
ROOT="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
until [[ -f "$ROOT/requirements.txt" ]] || [[ "$ROOT" == "/" ]]; do ROOT="$(dirname "$ROOT")"; done
[[ -f "$ROOT/requirements.txt" ]] || { echo "root not found"; exit 1; }
cd "$ROOT"; info "root: $ROOT"

PY="$ROOT/venv/Scripts/python.exe"; [[ -f "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"
[[ -f "$PY" ]] || { echo "no venv"; exit 1; }
BACKUP_DIR="$(dirname "$ROOT")/_patch_backups/$(date +%Y%m%d-%H%M%S)"

patch_file() {
  local f="$1"; [[ -f "$f" ]] || { fail "$f missing"; return 1; }
  mkdir -p "$BACKUP_DIR"; cp -- "$f" "$BACKUP_DIR/$(basename "$f").save"
  TARGET="$f" "$PY" -
  if "$PY" -m py_compile "$f" 2>/dev/null; then pass "$(basename "$f") OK"
  else fail "$(basename "$f") broke — restoring"; cp "$BACKUP_DIR/$(basename "$f").save" "$f"; return 1; fi
}

# ============ P1: audit-confirmed write acceptance ============
echo ""; info "P1: quick_test.py — PLC-audit-confirmed injection check"
QT="$ROOT/tests/quick_test.py"
if grep -q "PLC-AUDIT-CONFIRM" "$QT"; then
  pass "P1 already applied"
else
  QT="$QT" patch_file "$QT" <<'PYEOF'
import os, sys
path = os.environ["TARGET"]
src = open(path, encoding="utf-8").read()

OLD_INJECT = '''        # Inject fault.
        try:
            wr = armed_write_fault(client, fault_addr, code)
            accepted = not wr.isError()
        except Exception as e:
            accepted = False
            wr = None

        record(
            f"[{eq}] fault register write accepted",
            accepted,
            f"HR[{fault_addr}]={code}" if accepted else f"write error: {wr}",
        )'''

NEW_INJECT = '''        def _plc_inject_confirmed(addr: int, since: int, timeout_s: float = 15.0) -> bool:
            """PLC-AUDIT-CONFIRM: scan the PLC audit log (appended after `since`)
            for a FAULT_INJECT entry with result OK at the given address."""
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    if AUDIT_PLC.is_file():
                        with open(AUDIT_PLC, "r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(since)
                            for line in fh:
                                if ('"FAULT_INJECT"' in line
                                        and f'"address": {addr}' in line
                                        and '"OK"' in line):
                                    return True
                except Exception:
                    pass
                time.sleep(0.5)
            return False

        # Inject fault.
        try:
            wr = armed_write_fault(client, fault_addr, code)
            frame_ok = (wr is not None) and (not wr.isError())
        except Exception:
            frame_ok = False
            wr = None

        # PLC-AUDIT-CONFIRM: the simulator consumes the fault register after
        # processing and silently drops un-armed writes, so neither frame ACK
        # nor read-back proves injection. The PLC audit entry is authoritative.
        audit_ok = _plc_inject_confirmed(fault_addr, plc_offset)
        accepted = audit_ok

        record(
            f"[{eq}] fault register write accepted",
            accepted,
            (f"HR[{fault_addr}]={code} plc-audit-confirmed (frame_ack={frame_ok})"
             if accepted else
             f"unconfirmed: frame_ack={frame_ok}, wr={wr}, no FAULT_INJECT OK entry"),
        )'''

if OLD_INJECT not in src:
    print("PATCH ABORT: exact injection block not found (file drifted)")
    sys.exit(1)
src = src.replace(OLD_INJECT, NEW_INJECT, 1)

# P3: widen sweep windows (chain proven, currently latency-bound)
for old, new, label in (
    ("timeout_s=90,", "timeout_s=180,", "findings window"),
    ("timeout within 90s", "timeout within 180s", "findings msg"),
    ("timeout_s=120", "timeout_s=240", "LLM window"),
    ("timeout_s=45", "timeout_s=90", "Obsidian window"),
):
    n = src.count(old)
    if n:
        src = src.replace(old, new)
        print(f"    widened {label}: {n} occurrence(s)")

open(path, "w", encoding="utf-8").write(src)
print("PATCH OK: P1 + P3 applied")
PYEOF
fi

# ============ P4: alarm_events sink wiring ============
echo ""; info "P4: hook sink + api.py wiring"
HOOK="$ROOT/models/industrial_agent/remediation_hook.py"
if grep -q "set_alarm_sink" "$HOOK"; then
  pass "hook already has sink"
else
  HOOK="$HOOK" patch_file "$HOOK" <<'PYEOF'
import os, sys
path = os.environ["TARGET"]
src = open(path, encoding="utf-8").read()

ANCHOR = "from .remediation import RemediationEngine"
if ANCHOR not in src:
    print("PATCH ABORT: import anchor missing"); sys.exit(1)
SINK = ANCHOR + '''

# --- alarm-events sink (wired once by backend/api.py) ----------------------
_alarm_sink = None


def set_alarm_sink(db_handle) -> None:
    """Register the backend DB handle so HIGH/CRITICAL findings are persisted
    into the alarm_events table (statistical memory)."""
    global _alarm_sink
    _alarm_sink = db_handle'''
src = src.replace(ANCHOR, SINK, 1)

REC_LINE = '        ansi, sev, aa, rec = _ansi(d), _severity(d) or "HIGH", _auto_allowed(d), _llm_rec(a, d)'
if REC_LINE not in src:
    print("PATCH ABORT: record line missing"); sys.exit(1)
REC_PLUS = REC_LINE + '''
        if _alarm_sink is not None and sev.upper() in ("HIGH", "CRITICAL"):
            try:
                _alarm_sink.save_alarm_event(
                    eq, str(d.get("code", "FINDING")), ansi,
                    str(d.get("message", ""))[:250], sev,
                )
                _audit(au, "ALARM_EVENT_RECORDED", {"eq_id": eq, "severity": sev})
            except Exception as _db_err:
                _audit(au, "ALARM_EVENT_RECORD_FAILED", {"error": str(_db_err)})'''
src = src.replace(REC_LINE, REC_PLUS, 1)

open(path, "w", encoding="utf-8").write(src)
print("PATCH OK: hook sink added")
PYEOF
fi

API="$ROOT/backend/api.py"
if grep -q "ALARM-SINK WIRING" "$API"; then
  pass "api.py already wired"
else
  API="$API" patch_file "$API" <<'PYEOF'
import os, sys
path = os.environ["TARGET"]
lines = open(path, encoding="utf-8").read().split("\n")
start = next((i for i, ln in enumerate(lines)
              if ln.startswith("    agent = IndustrialCognitiveAgent(")), None)
if start is None:
    print("PATCH ABORT: agent constructor not found"); sys.exit(1)
# find end of the constructor statement (first line at indent < 4 that is code)
end = start + 1
while end < len(lines):
    ln = lines[end]
    if ln.strip() and not ln.startswith("        ") and not ln.lstrip().startswith("#"):
        break
    end += 1
WIRE = [
    "",
    "    # ALARM-SINK WIRING: give the remediation hook the backend DB handle so",
    "    # HIGH/CRITICAL findings persist into alarm_events (statistical memory).",
    "    try:",
    "        from industrial_agent.remediation_hook import set_alarm_sink",
    "        set_alarm_sink(db)",
    '        print("[API] alarm_events sink wired into remediation hook")',
    "    except Exception as _sink_err:",
    '        print(f"[API] alarm sink wiring failed: {_sink_err}")',
]
lines[end:end] = WIRE
open(path, "w", encoding="utf-8").write("\n".join(lines))
print("PATCH OK: api.py sink wiring inserted after agent construction")
PYEOF
fi

echo ""
cat <<'EOF'
==============================================================================
  NEXT:
    1. RESTART the backend (run_all.bat) — P4 wiring only takes effect then
    2. ./venv/Scripts/python.exe tests/quick_test.py --live --json
    3. STILL NEEDED for P2 (Option A): paste output of
         sed -n '1985,2015p' tests/quick_test.py
==============================================================================
EOF