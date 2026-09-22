"""Phase 3: Convert Streamlit Modbus READS to backend REST. Writes stay Modbus."""
import shutil, sys, datetime, re

FILE = "dashboard/streamlit_app.py"
src = open(FILE, encoding="utf-8").read()

backup = FILE + ".pre-phase3-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(FILE, backup)

new_status_fn = '''def read_system_status(client) -> int:
    """System status word via backend REST (estop=bit0, trip=bit1)."""
    try:
        r = requests.get(f"{BACKEND_URL}/api/health", timeout=2)
        if r.ok:
            ss = r.json().get("system_status", {})
            word = 0
            if ss.get("estop_active"):
                word |= 1
            if ss.get("any_trip_active"):
                word |= 2
            st.session_state.total_requests += 1
            return word
    except Exception as e:
        print(f"[SYS-STATUS] REST read failed: {e}")
    return 0
'''

new_read_fn = '''def read_all_equipment(client):
    """Read all equipment via backend REST API (replaces direct Modbus batch read)."""
    records = []
    now = datetime.now()

    try:
        resp = requests.get(f"{BACKEND_URL}/api/equipment", timeout=3)
    except Exception as e:
        err_msg = f"[REST] Backend unreachable: {e}"
        print(err_msg)
        st.session_state.last_error = err_msg
        st.session_state.modbus_errors.append({
            "time": now.strftime('%H:%M:%S'), "eq": "REST", "offset": 0, "error": str(e)
        })
        if len(st.session_state.modbus_errors) > 20:
            st.session_state.modbus_errors = st.session_state.modbus_errors[-20:]
        return records

    if not resp.ok:
        print(f"[REST] HTTP {resp.status_code}")
        return records

    payload = resp.json().get("equipment", {})
    st.session_state.total_requests += 1

    st.session_state.traffic_log.append({
        "time": now.strftime('%H:%M:%S.%f')[:-3],
        "ip": "localhost:8000",
        "direction": "OUT",
        "cmd": "GET /api/equipment [REST]",
        "addr": "-",
        "qty": "6",
        "eq": "ALL"
    })
    if len(st.session_state.traffic_log) > 50:
        st.session_state.traffic_log = st.session_state.traffic_log[-50:]

    STATUS_MAP = {0: "STOPPED", 1: "RUNNING", 2: "START PENDING", 3: "LOCKED"}

    for eq_id, cfg in EQUIPMENT_CONFIG.items():
        row = payload.get(eq_id)
        if not row or row.get("stale") or not row.get("data"):
            continue

        d = row["data"]
        motor_state = int(d.get("status", 0) or 0)
        trip_word = int(d.get("trip_word", 0) or 0)
        alarm_word = int(d.get("alarm_word", 0) or 0)
        motor_on = motor_state == 1

        if motor_state == 3 or d.get("theta_per_mille", 0) >= 1000:
            status = "LOCKED"
        elif trip_word > 0 and not motor_on:
            status = "TRIPPED"
        elif motor_state == 2:
            status = "START PENDING"
        else:
            status = STATUS_MAP.get(motor_state, "STOPPED")

        records.append({
            "timestamp": d.get("timestamp", now),
            "equipment_id": eq_id,
            "equipment_name": cfg["name"],
            "area": cfg["area"],
            "pf_target": cfg["pf_target"],
            "status": status,
            "voltage": float(d.get("voltage", 0) or 0),
            "current": float(d.get("current", 0) or 0),
            "active_power": float(d.get("active_power", 0) or 0),
            "reactive_power": float(d.get("reactive_power", 0) or 0),
            "apparent_power": float(d.get("apparent_power", 0) or 0),
            "power_factor": float(d.get("power_factor", 0) or 0),
            "frequency": float(d.get("frequency", 50) or 0),
            "energy_kwh": float(d.get("energy_kwh", 0) or 0),
            "alarm": alarm_word > 0,
            "alarm_code": alarm_word,
            "running_time_min": int(d.get("running_time_min", 0) or 0),
            "load": int(d.get("load", 0) or 0),
            "trip_word": trip_word,
            "alarm_word": alarm_word,
            "theta_per_mille": int(d.get("theta_per_mille", 0) or 0),
            "trip_count": int(d.get("trip_count", 0) or 0),
            "heartbeat": int(d.get("heartbeat", 0) or 0),
        })

    return records
'''

# Replace read_system_status (ends at 'def read_all_equipment')
src2, n1 = re.subn(
    r'def read_system_status\(client\).*?(?=def read_all_equipment)',
    new_status_fn + '\n\n', src, count=1, flags=re.S)

# Replace read_all_equipment (ends at 'def write_motor_command')
src2, n2 = re.subn(
    r'def read_all_equipment\(client\):.*?(?=def write_motor_command)',
    new_read_fn + '\n\n', src2, count=1, flags=re.S)

print(f"[INFO] read_system_status replaced: {n1}")
print(f"[INFO] read_all_equipment replaced: {n2}")

if n1 != 1 or n2 != 1:
    print("[ABORT] patterns not matched - file NOT modified")
    sys.exit(1)

open(FILE, "w", encoding="utf-8", newline="\n").write(src2)
print(f"[DONE] Phase 3 applied. Backup: {backup}")
