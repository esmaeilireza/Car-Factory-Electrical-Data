# ACTION HIERARCHY — WHAT THE AI MAY RECOMMEND

## LEVEL 1 — MONITORING (Always allowed, no consent needed)
- measure_latency()
- check_heartbeat()
- validate_hr120_bits()
- compute_thermal_trend()
- detect_traffic_anomaly()

## LEVEL 2 — ALERTING (Always allowed, no consent needed)
- display_warning(message)
- log_to_audit(event)
- send_email_alert(maintenance_team)
- highlight_equipment_in_ui(eq_id, color)

## LEVEL 3 — SAFE RECOVERY (Consent gate REQUIRED)
- reconnect_modbus()
  → Pre-condition: socket.is_connected == False OR latency > 5000ms
  → Post-verification: read HR[120] within 2 seconds

- flush_session_cache()
  → Pre-condition: _last_read_ts older than 10 seconds
  → Post-verification: next read returns fresh heartbeat

- restart_service(service_name)
  → Pre-condition: heartbeat frozen ≥ 30s AND operator confirms
  → Post-verification: service PID changes + heartbeat resumes

## LEVEL 4 — CRITICAL (NEVER auto-execute, RECOMMEND ONLY)
The following require HUMAN operator action via physical UI buttons:
- estop_all() → Sidebar "🚨 E-STOP ALL" button
- reset_protection() → Sidebar "🔓 RESET PROTECTION" button
- start_motor(eq_id) → HMI tab "▶️ START" button
- stop_motor(eq_id) → HMI tab "⏹️ STOP" button
- inject_fault(eq_id, type) → Testing panel only

## ESCALATION PATH
If Level 3 action fails 3 consecutive times:
1. STOP attempting auto-recovery
2. Escalate to Level 2 (persistent alert)
3. Display: "🚨 AI RECOVERY EXHAUSTED — HUMAN INTERVENTION REQUIRED"
4. Log escalation to audit_log.jsonl