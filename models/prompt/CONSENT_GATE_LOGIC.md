# CONSENT GATE — HUMAN-IN-THE-LOOP PROTOCOL

## TRIGGER CONDITIONS
Activate consent gate when ALL of these are true:
- AI diagnosis confidence ≥ 0.6
- Recommended action safety_level == 3
- AI has not already recommended the same action in last 60 seconds

## UI FLOW (Streamlit Implementation)

### STEP 1: Present diagnosis
Display modal with:
- 🔍 Problem description (plain language)
- 🎯 Recommended action (exact function name)
- ⚠️ Severity level (color-coded)
- 📊 Confidence percentage
- ⏱️ 15-second countdown timer

### STEP 2: Offer two buttons
[ ✅ YES, EXECUTE NOW ]  [ 🛑 NO, MANUAL REVIEW ]

### STEP 3: Handle response
- YES clicked → execute action → log → verify → show result
- NO clicked → cancel → log operator decision → defer
- Timeout (15s elapsed):
    - If severity == "critical" AND action in [reconnect, flush]:
        → Execute fail-safe
    - Else:
        → Cancel + escalate to persistent alert

## ANTI-SPAM PROTECTION
- Maximum 1 consent prompt per 60 seconds per equipment
- Maximum 3 consent prompts per 5 minutes globally
- If exceeded → AI enters "observe-only" mode for 10 minutes

## OPERATOR OVERRIDE
Operator can permanently disable AI auto-execution via:
Sidebar → Settings → "AI Auto-Recovery: [ON/OFF]"
Default state: ON
When OFF: AI operates in Level 1-2 only (monitoring + alerting)