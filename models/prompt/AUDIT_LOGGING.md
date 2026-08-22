# AUDIT LOG FORMAT — NON-REPUDIATION COMPLIANCE

## FILE: audit_log.jsonl (append-only, never edited)

## SCHEMA (one JSON object per line):
{
  "ts": "2026-08-22T05:44:00.783Z",
  "source": "ai_engine | operator | system",
  "event": "AI_DIAGNOSIS | AI_ACTION | OPERATOR_APPROVAL | OPERATOR_REJECTION | TIMEOUT_FAILSAFE",
  "safety_level": 1 | 2 | 3 | 4,
  "equipment": ["STP-01"],
  "diagnosis_hash": "sha256:...",
  "recommended_action": "reconnect_modbus",
  "executed": true | false,
  "pre_state": {"connected": false, "latency_ms": 9999},
  "post_state": {"connected": true, "latency_ms": 42},
  "confidence": 0.87,
  "operator_id": "operator@local" | null,
  "iec_reference": "IEC 62443-3 Annex C"
}

## RETENTION POLICY
- Keep minimum 90 days online
- Archive to compressed storage for 7 years
- Never delete entries (append-only)

## PRIVACY
- No personal data in logs
- Operator IDs are role-based, not names
- IP addresses limited to 127.0.0.1 (local deployment)