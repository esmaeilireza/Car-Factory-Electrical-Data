# FAILURE MODE KNOWLEDGE BASE — PRIORITIZED

## PRIORITY 1: MODBUS CONNECTION TIMEOUT / SOCKET STARVATION
**Signature:**
- traffic_log gaps > 5 seconds
- modbus_errors contains "No Response received from remote slave"
- Streamlit shows "STALE DATA" badge

**Root Causes (in order of likelihood):**
1. Blocking HTTP call in async event loop (modbus_server.py)
2. CPU starvation from excessive read frequency
3. Firewall or antivirus intercepting port 5020
4. pymodbus socket buffer overflow

**Recommended Action (Level 3):**
reconnect() → flush_cache() → monitor for 30s

**IEC Reference:** IEC 62443-3-3 SR 1.1 (Communication Integrity)

---

## PRIORITY 2: HR[120] STATUS WORD CORRUPTION
**Signature:**
- HR[120] bit 0 stuck at 1 while all equipment running normally
- HR[120] bit 1 stuck at 1 while no trip_word active anywhere
- HR[120] reads as 0xFFFF or 0x0000 persistently

**Root Causes:**
1. data_generator.py get_system_status_word() race condition
2. Modbus register block overlap (HR[120] overwritten by HR[108+])
3. Memory corruption in PLC simulator

**Recommended Action (Level 2 — CRITICAL SAFETY):**
⚠️ ALWAYS alert operator. NEVER auto-recover.
This affects E-STOP integrity — human verification required.

**IEC Reference:** ISA-18.2 §4.2 (Dual-Channel Validation)

---

## PRIORITY 3: PROTECTION LOGIC SILENT FAILURE
**Signature:**
- motor_status = True AND load > 0
- trip_word = 0 AND alarm_word = 0
- theta_per_mille NOT incrementing over time

**Root Causes:**
1. Protection.step() not being called in update loop
2. I_n (base current) misconfigured to 0
3. tau (thermal time constant) set to infinity

**Recommended Action (Level 2):**
Alert operator + freeze auto-start commands for affected equipment

**IEC Reference:** ANSI C37.90 / IEC 60255-1 (Protection Relay Testing)

---

## PRIORITY 4: HEARTBEAT FREEZE (≥30 seconds static)
**Signature:**
- heartbeat register unchanged for ≥30 seconds
- Other registers may still update (zombie state)

**Root Causes:**
1. PLC simulator main loop crashed but TCP server still alive
2. Asyncio event loop deadlock
3. data_generator update_all() throwing unhandled exception

**Recommended Action (Level 3 with consent):**
restart_service("modbus_server.py") — requires operator YES

**IEC Reference:** IEC 61131-2 (Watchdog Compliance for SIL-2)

---

## PRIORITY 5: ENERGY ACCUMULATION ANOMALY
**Signature:**
- energy_kwh decreases between reads (negative delta)
- energy_kwh jumps by > 100 kWh in single read
- energy_kwh stuck at 0 while active_power > 0

**Root Causes:**
1. Register overflow (r[7] exceeds 65535 → wraps to 0)
2. Timestamp misalignment in integration
3. data_generator energy integration dt corrupted

**Recommended Action (Level 2):**
Alert maintenance team + freeze MES data export

**IEC Reference:** IEC 62053-22 (Energy Metering Accuracy)

---

## PRIORITY 6: EXCESSIVE READ FREQUENCY (STREAMLIT BUG)
**Signature:**
- traffic_log shows > 5 reads per second sustained
- Interval between reads < 1000ms

**Root Cause:**
Infinite st.rerun() loop OR fragment re-triggering without sleep

**Recommended Action (Level 2):**
⚠️ Alert operator — this will kill any local AI model CPU budget.

**Industry Reference:** Siemens WinCC best practice = 1 read / 5s per equipment