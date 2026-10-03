#!/usr/bin/env bash
# =============================================================================
#  apply_layer_a.sh — device attribution at the LLM finding (Layer A)
#
#  Stamps the originating device + code into the SYSTEM-level LLM finding:
#    - message prefixed with "<EQ>:"  -> obsidian_bridge text-mining emits
#      [[<EQ>]] in the incident body -> checker matches, remediation lookup
#      (content fallback) works, cross-device drift gets an anchor
#    - evidence gets originating_eq_id / originating_code for auditability
#
#  Best-effort by design: attribution failures must never block the agent
#  loop. Backend restart REQUIRED after patching.
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
[[ -f "$ROOT/requirements.txt" ]] || { fail "root not found"; exit 1; }
cd "$ROOT"; info "root: $ROOT"

PY="$ROOT/venv/Scripts/python.exe"; [[ -f "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"
AG="$ROOT/models/industrial_agent/agent.py"
BK="$(dirname "$ROOT")/_patch_backups/$(date +%Y%m%d-%H%M%S)"

if grep -q "LAYER-A-ATTRIBUTION" "$AG"; then
  pass "already applied (idempotent skip)"; exit 0
fi

mkdir -p "$BK"; cp -- "$AG" "$BK/agent.py.save"
AG="$AG" "$PY" - <<'PYEOF'
import os, sys
src = open(os.environ["TARGET"], encoding="utf-8").read()

ANCHOR = '''                llm_finding = self._llm_to_finding(llm_result)
                if llm_finding:
                    self.findings.append(llm_finding)'''
if src.count(ANCHOR) != 1:
    print(f"PATCH ABORT: anchor found {src.count(ANCHOR)}x (expected 1).")
    print("Paste the current block around _llm_to_finding for a manual patch.")
    sys.exit(1)

REPLACEMENT = '''                llm_finding = self._llm_to_finding(llm_result)
                if llm_finding:
                    # LAYER-A-ATTRIBUTION: the LLM finding is SYSTEM-tagged
                    # (Qwen's context is cross-device), but the agent knows
                    # which HIGH/CRITICAL finding triggered this consult.
                    # Stamp the originating device + code so obsidian_bridge's
                    # text mining emits [[<eq>]] and incidents are correctly
                    # attributed (also fixes find_latest_incident_for).
                    try:
                        _origin = None
                        for _f in all_findings:
                            _sev = getattr(_f, "severity", "")
                            _sev = str(getattr(_sev, "value", _sev)).upper()
                            _eid = str(getattr(_f, "eq_id", ""))
                            if _sev in ("HIGH", "CRITICAL") and _eid not in ("", "SYSTEM"):
                                _origin = _f
                                break
                        if _origin is not None:
                            _eid = str(_origin.eq_id)
                            _msg = str(llm_finding.message or "")
                            if _eid not in _msg:
                                llm_finding.message = f"{_eid}: {_msg}"
                            _ev = dict(llm_finding.evidence or {})
                            _ev.setdefault("originating_eq_id", _eid)
                            _ev.setdefault("originating_code",
                                           str(getattr(_origin, "code", "")))
                            llm_finding.evidence = _ev
                    except Exception:
                        pass  # attribution is best-effort; never block the loop
                    self.findings.append(llm_finding)'''

src = src.replace(ANCHOR, REPLACEMENT, 1)
open(os.environ["TARGET"], "w", encoding="utf-8").write(src)
print("PATCH OK: LAYER-A-ATTRIBUTION inserted")
PYEOF

OUT="$("$PY" -m py_compile "$AG" 2>&1)"
if [[ -z "$OUT" ]]; then
  pass "agent.py patched and compiles"
else
  fail "compile error — RESTORING. Error:"
  echo "$OUT"; cp "$BK/agent.py.save" "$AG"
fi

echo ""
cat <<'EOF'
==============================================================================
  NEXT (order matters):
    1. Commit the pending README/.gitignore changes FIRST (see above)
    2. RESTART the stack (run_all.bat)  <- agent.py is process state
    3. ./venv/Scripts/python.exe tests/quick_test.py --live --json
    4. Expected improvements:
       - Incident filenames become {ts}_<EQ>.md instead of _SYSTEM.md
       - Qwen diagnoses prefixed with the originating device
       - Remediation section lookup should now find the note
         (AUTONOMOUS REMEDIATION FAIL likely clears — Layer A was its root)
    5. If all green:
         git add -A && git commit -m "feat: Layer A device attribution at
         LLM findings — incidents correctly attributed, cross-device drift
         anchored"
==============================================================================
EOF