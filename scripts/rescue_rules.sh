#!/usr/bin/env bash
# =============================================================================
#  fix_rules_now.sh — definitive repair of models/industrial_agent/rules.py
#
#  Path A (expected): git HEAD is good -> restore, compile, done.
#  Path B (fallback): HEAD also broken -> print lines 190-215 so I can write
#                     a whole-function replacement (no more inserting).
#
#  After Path A succeeds, this script immediately verifies the two
#  downstream consumers (pytest, backend import) so we know the cascade
#  is cleared — not just the file.
# =============================================================================
set -uo pipefail
GREEN='\033[92m'; RED='\033[91m'; CYAN='\033[96m'; YELLOW='\033[93m'; RESET='\033[0m'
pass() { echo -e "${GREEN}[PASS]${RESET} $1"; }
fail() { echo -e "${RED}[FAIL]${RESET} $1"; }
info() { echo -e "${CYAN}[INFO]${RESET} $1"; }
warn() { echo -e "${YELLOW}[WARN]${RESET} $1"; }

PY="./venv/Scripts/python.exe"
RULES="models/industrial_agent/rules.py"
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 0. evidence: current broken state -------------------------------------
echo ""
info "Current broken region (lines 195-212):"
echo "----------------------------------------------------------------------"
nl -ba "$RULES" | sed -n '195,212p'
echo "----------------------------------------------------------------------"

# ---- 1. diff vs HEAD --------------------------------------------------------
echo ""
info "Git diff vs HEAD for rules.py:"
git diff --stat -- "$RULES" || true
git diff -- "$RULES" | head -50 || true

# ---- 2. restore from HEAD unconditionally -----------------------------------
echo ""
info "Restoring rules.py from git HEAD (last known-good source)..."
git restore -- "$RULES" && pass "git restore executed"

# ---- 3. compile gate ---------------------------------------------------------
if "$PY" -m py_compile "$RULES" 2>/dev/null; then
  pass "rules.py (HEAD) COMPILES -> Path A complete"
else
  fail "rules.py fails even at HEAD — Path B: file was committed broken."
  warn "PASTE ME the 195-212 region printed above, and run:"
  echo "    git log --oneline -5 -- $RULES"
  echo "    git show HEAD:$RULES | ./venv/Scripts/python.exe -m py_compile /dev/stdin"
  exit 1
fi

# ---- 4. verify downstream consumers -----------------------------------------
echo ""
info "Verifying pytest collection (the earlier 1-error blocker):"
"$PY" -m pytest tests/ -q 2>&1 | tail -5

echo ""
info "Verifying backend import (the uvicorn blocker):"
"$PY" -c "import backend.api; print('backend.api imports OK')" 2>&1 | tail -3

# ---- 5. next steps ------------------------------------------------------------
echo ""
cat <<'EOF'
==============================================================================
  IF both verifications above are green:
    1. Start the backend (its own terminal, watch it load Qwen ~60-90s):
         ./venv/Scripts/python.exe -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
    2. Then run the read-only verifier:
         ./venv/Scripts/python.exe tests/quick_test.py
    3. Expected: back to baseline 52 PASS / 0 FAIL / 9 pytest tests.

  The rules.py breakage was caused by the quarantined script
  (scripts/fix_plc_client_and_guard_reset.sh.QUARANTINED) whose backups in
  docs/backups/ hold BROKEN copies — never restore from those. Only git.
==============================================================================
EOF