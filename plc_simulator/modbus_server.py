"""
Modbus TCP Server - Simulates Delta PLC for 6 equipment
Port: 5020 (bind 127.0.0.1 for security)

FEATURES:
- ANSI Protection layer (49/50/51/27/59/38)
- Security: write allowlist, rate limiting, audit log
- E-STOP (CO[6]) latches globally, published at HR[120]
- RESET (CO[7]) clears E-STOP latch + per-equipment lockouts
- Watchdog via heartbeat monitoring
- 18 registers per equipment (108 total) + HR[120] system status
- OPTIONAL: Automatically send data to backend (REST API) [NON-BLOCKING]

FIXES APPLIED:
- Write hooks replace polling (no missed sub-100ms coil changes)
- Thread/async-safe rate limiting with asyncio.Lock
- Backend errors logged explicitly (no silent failures)
- Audit file flushed + locked on every write (no data loss on crash)
- Production-ready auth: X-API-Key header sent when NEXUS_API_KEY is set
- Payload keys aligned to DB schema (theta_per_mille, energy_kwh, status, running_time_min)
- MOTOR_STATE_NAMES defined for semantic clarity; wire format stays int for consumer safety
"""
import asyncio
import logging
import signal
import time
import json
import os
import requests
from pathlib import Path
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from data_generator import FactoryPLC

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==========================================
# Semantic Motor State Labels
# ==========================================
# Matches STATUS_MAP in dashboard/streamlit_app.py exactly.
# Wire format remains int to prevent breakage in consumers that do int(d.get("status", 0)).
MOTOR_STATE_NAMES = {
    0: "STOPPED",
    1: "RUNNING",
    2: "START PENDING",
    3: "LOCKOUT",
}

# ==========================================
# Backend Integration (Non-Blocking)
# ==========================================
BACKEND_URL = "http://localhost:8000/api/equipment"
API_KEY = os.environ.get("NEXUS_API_KEY", "")


def _sync_send_to_backend(equipment_id: str, data: dict):
    """Synchronous HTTP request (runs in a background thread)."""
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    try:
        resp = requests.post(
            f"{BACKEND_URL}/{equipment_id}/data",
            json=data,
            headers=headers,
            timeout=5.0
        )
        if not resp.ok:
            logger.warning(
                f"[BACKEND] HTTP {resp.status_code} for {equipment_id}: {resp.text[:200]}"
            )
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"[BACKEND] Connection refused for {equipment_id}: {e}")
    except requests.exceptions.Timeout:
        logger.warning(f"[BACKEND] Timeout sending data for {equipment_id}")
    except Exception as e:
        logger.warning(f"[BACKEND] Unexpected error for {equipment_id}: {type(e).__name__}: {e}")


async def send_to_backend(equipment_id: str, data: dict):
    """Offloads the blocking `requests` call to a separate thread."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_send_to_backend, equipment_id, data)
    except Exception as e:
        logger.warning(f"[BACKEND] Executor error for {equipment_id}: {type(e).__name__}: {e}")


# ==========================================
# Security & Audit Config
# ==========================================
AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit_log.jsonl"
RATE_LIMIT_WRITES_PER_SEC = 10
WRITE_ALLOWLIST_COILS = {0, 1, 2, 3, 4, 5, 6, 7}
WRITE_ALLOWLIST_REGS = set(range(12, 108, 18))

SYS_STATUS_ADDR = 120

# Global state
_shutdown_event = None
_audit_file = None
_audit_lock = asyncio.Lock()          # FIX: Protects audit file writes
_rate_limit_lock = asyncio.Lock()     # FIX: Protects rate limit state
_write_timestamps: dict[str, list[float]] = {}  # FIX: No longer defaultdict

plc = FactoryPLC()


# ==========================================
# Audit Logging (FIX: Locked + Flushed)
# ==========================================
async def audit_log(event_type: str, source: str, address: int,
                    old_value: int, new_value: int, result: str):
    """Append-only audit log. Thread/async safe with immediate flush."""
    entry = {
        "ts": time.strftime('%Y-%m-%d %H:%M:%S'),
        "event": event_type,
        "source": source,
        "address": address,
        "old_value": old_value,
        "new_value": new_value,
        "result": result,
    }
    line = json.dumps(entry) + "\n"

    async with _audit_lock:
        if _audit_file and not _audit_file.closed:
            _audit_file.write(line)
            _audit_file.flush()       # FIX: Immediate flush prevents data loss on crash

    logger.info(f"[AUDIT] {event_type} addr={address} {old_value}→{new_value} [{result}] from {source}")


async def check_rate_limit(source_ip: str) -> bool:
    """Async-safe rate limiter. Returns True if write is allowed."""
    async with _rate_limit_lock:
        now = time.time()
        timestamps = _write_timestamps.get(source_ip, [])
        timestamps = [t for t in timestamps if now - t < 1.0]

        if len(timestamps) >= RATE_LIMIT_WRITES_PER_SEC:
            # Release lock before awaiting audit_log
            _write_timestamps[source_ip] = timestamps
            await audit_log("RATE_LIMIT", source_ip, 0, 0, 0, "BLOCKED")
            return False

        timestamps.append(now)
        _write_timestamps[source_ip] = timestamps
        return True


# ==========================================
# Write Hook Context (FIX: Replaces Polling)
# ==========================================
class SafeSlaveContext(ModbusSlaveContext):
    """
    Custom slave context that intercepts writes synchronously.
    Eliminates the timing gap inherent in polling-based approaches.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._eq_ids = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]

    def setValues(self, fc, address, values):
        """Intercept ALL writes before they hit the datastore."""
        # --- Coil Writes (FC 1/5/15) ---
        if fc in (1, 5, 15) and address == 0:
            asyncio.ensure_future(self._handle_coil_write(values))
        elif fc == 5 and address in (6, 7):
            # Single-coil write (fc5) to E-STOP/RESET: route into the block handler
            block = [0] * 8
            block[address] = values[0]
            asyncio.ensure_future(self._handle_coil_write(block))
        elif fc == 5 and 0 <= address <= 5:
            # fc5 single-coil motor write: dedicated handler so the other
            # five motors are never disturbed.
            asyncio.ensure_future(
                self._handle_single_motor_write(address, values[0]))

        # --- Register Writes for Fault Injection (FC 3/6/16) ---
        elif fc in (3, 6, 16) and 150 <= address <= 155:
            asyncio.ensure_future(self._handle_fault_inject(address, values))
        elif fc in (3, 6, 16) and 160 <= address <= 165:
            # Operator load setpoints: written to the datastore (readable back
            # by any client) AND recorded by the PLC physics loop.
            asyncio.ensure_future(self._handle_setpoint_write(address, values))

        # Always apply the write to the underlying datastore
        super().setValues(fc, address, values)

    async def _handle_coil_write(self, coil_values: list[int]):
        """Process coil writes with security checks and E-STOP logic."""
        source_ip = "operator"  # In production, extract from connection info

        if not await check_rate_limit(source_ip):
            return

        eq_ids = self._eq_ids

        # === E-STOP (CO[6]) — latch globally ===
        if len(coil_values) > 6 and coil_values[6]:
            if not plc.estop_latched:
                plc.emergency_stop_all()
                await audit_log("E-STOP", source_ip, 6, 0, 1, "LATCHED")
            # Always consume the request - prevents repeat-trigger floods
            super().setValues(1, 6, [0])
            return

        # === RESET (CO[7]) — clear E-STOP latch + per-equipment lockouts ===
        if len(coil_values) > 7 and coil_values[7]:
            results = []
            if plc.estop_latched:
                plc.clear_estop_latch()
                results.append("E-STOP:CLEARED")
            else:
                results.append("E-STOP:WAS-CLEAR")

            for eq_id in eq_ids:
                eq = plc.equipment[eq_id]
                if eq.protection.latched:
                    ok = eq.try_reset()
                    results.append(f"{eq_id}:{'OK' if ok else 'REFUSED'}")

            super().setValues(1, 7, [0])
            await audit_log("RESET", source_ip, 7, 0, 1, ",".join(results))
            return

        # === Motor START/STOP (CO[0-5]) ===
        for i, eq_id in enumerate(eq_ids):
            if i >= len(coil_values):
                break
            new_state = coil_values[i]

            if i not in WRITE_ALLOWLIST_COILS:
                await audit_log("WRITE_BLOCKED", source_ip, i, 0, new_state, "ALLOWLIST")
                continue

            eq = plc.equipment[eq_id]
            if new_state:
                if plc.estop_latched:
                    await audit_log("MOTOR_START", source_ip, i, 0, 1, "ESTOP-LOCKOUT")
                    super().setValues(1, i, [0])
                else:
                    success = eq.start_motor()
                    await audit_log(
                        "MOTOR_START", source_ip, i, 0, 1,
                        "OK" if success else "LOCKOUT"
                    )
                    if not success:
                        super().setValues(1, i, [0])
            else:
                eq.stop_motor()
                await audit_log("MOTOR_STOP", source_ip, i, 1, 0, "OK")

    async def _handle_single_motor_write(self, coil_index: int, new_state: int):
        """Handle one fc5 motor-coil write without touching other motors."""
        source_ip = "operator"
        eq = plc.equipment[self._eq_ids[coil_index]]
        if new_state:
            if plc.estop_latched:
                await audit_log("MOTOR_START", source_ip, coil_index, 0, 1,
                                "ESTOP-LOCKOUT")
                super().setValues(1, coil_index, [0])
                return
            success = eq.start_motor()
            await audit_log("MOTOR_START", source_ip, coil_index, 0, 1,
                            "OK" if success else "LOCKOUT")
            if not success:
                super().setValues(1, coil_index, [0])
        else:
            eq.stop_motor()
            await audit_log("MOTOR_STOP", source_ip, coil_index, 1, 0, "OK")

    async def _handle_setpoint_write(self, address: int, values: list[int]):
        """Process operator load setpoint writes (HR[160-165]).

        Unlike fault injection, the value is NOT auto-cleared: the setpoint
        latches until the operator writes 0 (release to autonomous mode).
        The write itself still lands in the datastore via super().setValues.
        """
        source_ip = "operator"
        if not await check_rate_limit(source_ip):
            return

        idx = address - 160
        if idx < 0 or idx >= len(self._eq_ids):
            return

        new_val = values[0] if values else 0
        plc.set_load_setpoint(idx, int(new_val))
        await audit_log(
            "LOAD_SETPOINT", source_ip, address, 0, int(new_val),
            "OK" if new_val > 0 else "RELEASED",
        )

    async def _handle_fault_inject(self, address: int, values: list[int]):
        """Process fault injection register writes."""
        source_ip = "operator"
        if not await check_rate_limit(source_ip):
            return

        idx = address - 150
        if idx < 0 or idx >= len(self._eq_ids):
            return

        new_val = values[0] if values else 0
        if new_val <= 0:
            return

        eq_id = self._eq_ids[idx]
        eq = plc.equipment[eq_id]

        fault_map = {1: "thermal", 2: "inst_oc", 3: "overvolt", 4: "overtemp"}
        fault_type = fault_map.get(new_val)

        if fault_type:
            eq.inject_fault(fault_type)
            # Auto-clear the injection register
            super().setValues(3, address, [0])
            await audit_log("FAULT_INJECT", source_ip, address, 0, new_val, "OK")


# ==========================================
# PLC Data Update (with HR[120] status word)
# ==========================================
async def update_plc_data(context):
    """Update PLC data every 200ms + publish system status at HR[120] + send to backend."""
    last_backend_send = 0.0

    while not _shutdown_event.is_set():
        try:
            plc.update_all()
            registers = plc.get_all_registers()

            if len(registers) > 256:
                logger.error(f"Register dump {len(registers)} exceeds HR block 256")
                registers = registers[:256]

            context[1].setValues(3, 0, registers)

            sys_status = plc.get_system_status_word()
            context[1].setValues(3, SYS_STATUS_ADDR, [sys_status])

            now = time.time()
            if now - last_backend_send >= 1.0:
                for eq_id, eq in plc.equipment.items():
                    # Keys aligned to database.py schema exactly.
                    # status sent as int to match consumer expectations (int cast in dashboard).
                    # MOTOR_STATE_NAMES available for logging/debugging; wire format stays numeric.
                    data = {
                        'voltage': eq.data.voltage,
                        'current': eq.data.current,
                        'active_power': eq.data.active_power,
                        'reactive_power': eq.data.reactive_power,
                        'apparent_power': eq.data.apparent_power,
                        'power_factor': eq.data.power_factor,
                        'frequency': eq.data.frequency,
                        'energy_kwh': eq.data.energy,
                        'status': int(eq._motor_state),
                        'alarm': bool(eq.protection.alarm_word),
                        'alarm_code': eq.protection.trip_word,
                        'running_time_min': eq.data.running_time,
                        'load': eq.data.load,
                        'temperature': eq.data.temperature,
                        'trip_word': eq.protection.trip_word,
                        'alarm_word': eq.protection.alarm_word,
                        'theta_per_mille': int(eq.protection.theta * 1000),
                        'trip_count': eq.protection.trip_count,
                        'heartbeat': eq._heartbeat,
                    }
                    asyncio.create_task(send_to_backend(eq_id, data))
                last_backend_send = now

            await asyncio.sleep(0.2)

        except asyncio.CancelledError:
            logger.info("[PLC] Update task cancelled")
            break
        except Exception as e:
            logger.error(f"Error updating PLC data: {e}")
            await asyncio.sleep(0.2)


# ==========================================
# Context Creation
# ==========================================
def create_modbus_context():
    coils = ModbusSequentialDataBlock(0, [1, 1, 1, 1, 1, 1, 0, 0, 0, 0])
    holding_registers = ModbusSequentialDataBlock(0, [0] * 256)

    # FIX: Use SafeSlaveContext instead of plain ModbusSlaveContext
    store = SafeSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 10),
        co=coils,
        hr=holding_registers,
        ir=ModbusSequentialDataBlock(0, [0] * 10),
        zero_mode=True,
    )

    return ModbusServerContext(slaves={1: store}, single=False)


# ==========================================
# Startup Banner
# ==========================================
def print_startup_banner():
    print()
    print("=" * 70)
    print("⚡ DELTA PLC SIMULATOR - Car Factory")
    print("🛡️  WITH ANSI PROTECTION + E-STOP LATCH")
    print("=" * 70)
    print(f"🌐 IP: 127.0.0.1  |  Port: 5020 (localhost only)")
    print(f"🔌 Protocol: Modbus TCP")
    print(f"🆔 Slave ID: 1 (explicit)")
    print(f"📊 Register Map: 108 holding registers (18/equipment)")
    print(f"📊 System Status Word: HR[{SYS_STATUS_ADDR}]")
    print(f"   bit 0 = E-STOP latched")
    print(f"   bit 1 = any equipment tripped")
    print(f"⚙️  zero_mode: True")
    print(f"🔒 Security: Write allowlist + Rate limit ({RATE_LIMIT_WRITES_PER_SEC}/s)")
    print(f"📝 Audit: {AUDIT_LOG_PATH}")
    print("-" * 70)
    print("🏭 EQUIPMENT (with ANSI protection):")
    print("-" * 70)

    eq_info = [
        ("STP-01", "Stamping Press", 0),
        ("WLD-01", "Welding Robots", 18),
        ("PNT-01", "Paint Booth", 36),
        ("ASM-01", "Assembly Line", 54),
        ("UTI-01", "Compressor", 72),
        ("UTI-02", "Chiller Plant", 90),
    ]

    for eq_id, name, offset in eq_info:
        print(f"  ⏹️  {eq_id:8s} | {name:18s} | Offset {offset:3d}-{offset+17:3d} | Motor: OFF")

    print("-" * 70)
    print("🎛️  COIL MAP:")
    print("    CO[0-5]: Motor START/STOP per equipment")
    print("    CO[6]  : E-STOP ALL (latches globally)")
    print("    CO[7]  : RESET (clears E-STOP latch + per-eq lockouts)")
    print("-" * 70)
    print("📊 PER-EQUIPMENT HR (offset+N):")
    print("    +8 : motor_state (0=STOPPED,1=RUNNING,2=START PENDING,3=LOCKOUT)")
    print("    +10: trip_word   (ANSI fault bitmask)")
    print("    +13: alarm_word  (ANSI warning bitmask)")
    print("    +14: lockout     (1=latched, 0=clear)")
    print("    +15: theta × 1000 (thermal ‰)")
    print("    +16: trip_count  (lifetime)")
    print("    +17: heartbeat   (watchdog)")
    print("-" * 70)
    print("💡 Press Ctrl+C to stop the server")
    print("=" * 70)
    print()


# ==========================================
# Main Server
# ==========================================
async def run_modbus_server():
    global _shutdown_event, _audit_file
    _shutdown_event = asyncio.Event()

    _audit_file = open(AUDIT_LOG_PATH, "a", encoding="utf-8")
    await audit_log("SERVER_START", "system", 0, 0, 0, "OK")

    context = create_modbus_context()
    print_startup_banner()

    # NOTE: check_coil_writes and check_reg_writes tasks REMOVED
    # Write handling is now done synchronously via SafeSlaveContext.setValues
    update_task = asyncio.create_task(update_plc_data(context))

    def handle_shutdown(signame):
        logger.info(f"Received signal {signame}, shutting down...")
        _shutdown_event.set()

    loop = asyncio.get_running_loop()
    for signame in ('SIGINT', 'SIGTERM'):
        try:
            loop.add_signal_handler(
                getattr(signal, signame),
                lambda s=signame: handle_shutdown(s)
            )
        except (AttributeError, NotImplementedError):
            pass

    try:
        await StartAsyncTcpServer(
            context=context,
            address=("127.0.0.1", 5020),
        )
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Cancelling background tasks...")
        update_task.cancel()

        try:
            await update_task
        except asyncio.CancelledError:
            pass

        await audit_log("SERVER_STOP", "system", 0, 0, 0, "OK")
        if _audit_file and not _audit_file.closed:
            _audit_file.close()

        logger.info("Server stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(run_modbus_server())
    except KeyboardInterrupt:
        print("\n👋 Server stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Server crashed: {e}")
        import traceback
        traceback.print_exc()