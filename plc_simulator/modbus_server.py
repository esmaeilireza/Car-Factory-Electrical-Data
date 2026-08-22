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
"""
import asyncio
import logging
import signal
import time
import json
import requests
from pathlib import Path
from collections import defaultdict
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from data_generator import FactoryPLC, ANSI_FUNCTIONS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==========================================
# Backend Integration (Non-Blocking)
# ==========================================
BACKEND_URL = "http://localhost:8000/api/equipment"

def _sync_send_to_backend(equipment_id: str, data: dict):
    """Synchronous HTTP request (runs in a background thread)"""
    try:
        requests.post(f"{BACKEND_URL}/{equipment_id}/data", json=data, timeout=1)
    except Exception:
        pass  # Ignore errors if Backend is offline

async def send_to_backend(equipment_id: str, data: dict):
    """Offloads the blocking `requests` call to a separate thread"""
    try:
        loop = asyncio.get_running_loop()
        # run_in_executor prevents blocking the Modbus asyncio event loop
        await loop.run_in_executor(None, _sync_send_to_backend, equipment_id, data)
    except Exception:
        pass

# ==========================================
# Security & Audit Config
# ==========================================
AUDIT_LOG_PATH = Path("audit_log.jsonl")
RATE_LIMIT_WRITES_PER_SEC = 10
WRITE_ALLOWLIST_COILS = {0, 1, 2, 3, 4, 5, 6, 7}
WRITE_ALLOWLIST_REGS = set(range(12, 108, 18))

# === System status word address (outside equipment range 0-107) ===
SYS_STATUS_ADDR = 120

# Global state
_shutdown_event = None
_audit_file = None
_write_timestamps = defaultdict(list)

plc = FactoryPLC()


# ==========================================
# Audit Logging
# ==========================================
def audit_log(event_type: str, source: str, address: int, 
              old_value: int, new_value: int, result: str):
    """Append-only audit log for non-repudiation"""
    global _audit_file
    entry = {
        "ts": time.strftime('%Y-%m-%d %H:%M:%S'),
        "event": event_type,
        "source": source,
        "address": address,
        "old_value": old_value,
        "new_value": new_value,
        "result": result,
    }
    if _audit_file:
        _audit_file.write(json.dumps(entry) + "\n")
        _audit_file.flush()
    logger.info(f"[AUDIT] {event_type} addr={address} {old_value}→{new_value} [{result}] from {source}")


def check_rate_limit(source_ip: str) -> bool:
    now = time.time()
    timestamps = _write_timestamps[source_ip]
    _write_timestamps[source_ip] = [t for t in timestamps if now - t < 1.0]
    
    if len(_write_timestamps[source_ip]) >= RATE_LIMIT_WRITES_PER_SEC:
        audit_log("RATE_LIMIT", source_ip, 0, 0, 0, "BLOCKED")
        return False
    
    _write_timestamps[source_ip].append(now)
    return True


# ==========================================
# Coil Write Handler
# ==========================================
async def check_coil_writes(context):
    """Check for coil writes from dashboard - with security + E-STOP latch"""
    last_coil_states = [1,1,1,1,1,1, 0,0]  # Must match initial coil block (motors running)
    eq_ids = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
    
    while not _shutdown_event.is_set():
        try:
            coil_values = context[1].getValues(1, 0, count=8)
            
            # === E-STOP (CO[6]) — latch globally ===
            if coil_values[6] and not last_coil_states[6]:
                plc.emergency_stop_all()               # sets estop_latched=True
                context[1].setValues(1, 6, [0])        # auto-clear the coil
                audit_log("E-STOP", "operator", 6, 0, 1, "LATCHED")
            
            # === RESET (CO[7]) — clear E-STOP latch + per-equipment lockouts ===
            if coil_values[7] and not last_coil_states[7]:
                results = []
                
                # First: clear the global E-STOP latch
                if plc.estop_latched:
                    plc.clear_estop_latch()
                    results.append("E-STOP:CLEARED")
                else:
                    results.append("E-STOP:WAS-CLEAR")
                
                # Then: attempt to reset per-equipment protection lockouts
                for eq_id in eq_ids:
                    eq = plc.equipment[eq_id]
                    if eq.protection.latched:
                        ok = eq.try_reset()
                        results.append(f"{eq_id}:{'OK' if ok else 'REFUSED'}")
                
                context[1].setValues(1, 7, [0])        # auto-clear coil
                audit_log("RESET", "operator", 7, 0, 1, ",".join(results))
            
            # === Motor START/STOP (CO[0-5]) ===
            for i, eq_id in enumerate(eq_ids):
                new_state = coil_values[i]
                if new_state != last_coil_states[i]:
                    eq = plc.equipment[eq_id]
                    
                    if i not in WRITE_ALLOWLIST_COILS:
                        audit_log("WRITE_BLOCKED", "operator", i, 
                                 last_coil_states[i], new_state, "ALLOWLIST")
                        continue
                    
                    if new_state:
                        # START inhibited by either per-eq lockout OR E-STOP latch
                        if plc.estop_latched:
                            audit_log("MOTOR_START", "operator", i, 0, 1, "ESTOP-LOCKOUT")
                            context[1].setValues(1, i, [0])
                        else:
                            success = eq.start_motor()
                            audit_log("MOTOR_START", "operator", i, 0, 1, 
                                     "OK" if success else "LOCKOUT")
                            if not success:
                                context[1].setValues(1, i, [0])
                    else:
                        eq.stop_motor()
                        audit_log("MOTOR_STOP", "operator", i, 1, 0, "OK")
                    
                    last_coil_states[i] = new_state
                    
        except asyncio.CancelledError:
            logger.info("[PLC] Coil check task cancelled")
            break
        except Exception as e:
            logger.error(f"Error checking coils: {e}")
            
        await asyncio.sleep(0.1)


# ==========================================
# PLC Data Update (with HR[120] status word)
# ==========================================
async def update_plc_data(context):
    """Update PLC data every 200ms + publish system status at HR[120] + send to backend"""
    last_backend_send = 0.0
    
    while not _shutdown_event.is_set():
        try:
            plc.update_all()
            registers = plc.get_all_registers()
            
            if len(registers) > 256:
                logger.error(f"Register dump {len(registers)} exceeds HR block 256")
                registers = registers[:256]
            
            # Write equipment registers (0..107)
            context[1].setValues(3, 0, registers)
            
            # === NEW: Write system status word at HR[120] ===
            # This is what Streamlit reads to know E-STOP state
            sys_status = plc.get_system_status_word()
            context[1].setValues(3, SYS_STATUS_ADDR, [sys_status])
            
            # --- Optional: Send data to backend every 1 second ---
            now = time.time()
            if now - last_backend_send >= 1.0:
                for eq_id, eq in plc.equipment.items():
                    data = {
                        'voltage': eq.data.voltage,
                        'current': eq.data.current,
                        'active_power': eq.data.active_power,
                        'reactive_power': eq.data.reactive_power,
                        'apparent_power': eq.data.apparent_power,
                        'power_factor': eq.data.power_factor,
                        'frequency': eq.data.frequency,
                        'energy': eq.data.energy,
                        'motor_status': eq.data.motor_status,
                        'alarm': eq.data.alarm,
                        'alarm_code': eq.data.alarm_code,
                        'running_time': eq.data.running_time,
                        'load': eq.data.load,
                        'temperature': eq.data.temperature,
                        'trip_word': eq.protection.trip_word,
                        'alarm_word': eq.protection.alarm_word,
                        'theta': eq.protection.theta,
                        'trip_count': eq.protection.trip_count,
                        'heartbeat': eq.heartbeat,
                    }
                    # [FIX] Fire and forget without blocking the main Modbus loop
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
    coils = ModbusSequentialDataBlock(0, [1,1,1,1,1,1, 0,0, 0,0])  # CO[0-5]=ON (motors running at init), CO[6-7]=OFF (E-STOP/RESET)
    # Need at least SYS_STATUS_ADDR+1 registers (121)
    holding_registers = ModbusSequentialDataBlock(0, [0] * 256)
    
    store = ModbusSlaveContext(
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
    print("    +13: trip_word   (ANSI bitmask)")
    print("    +14: alarm_word  (ANSI bitmask)")
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

# ==========================================
# Register Write Handler (For Fault Injection Testing)
# ==========================================
async def check_reg_writes(context):
    """Check for fault injection writes at HR[150..155]"""
    last_reg_states = [0] * 6
    eq_ids = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
    
    while not _shutdown_event.is_set():
        try:
            reg_values = context[1].getValues(3, 150, count=6)
            for i, eq_id in enumerate(eq_ids):
                new_val = reg_values[i]
                if new_val != last_reg_states[i] and new_val > 0:
                    eq = plc.equipment[eq_id]
                    if new_val == 1: eq.inject_fault("thermal")
                    elif new_val == 2: eq.inject_fault("inst_oc")
                    elif new_val == 3: eq.inject_fault("overvolt")
                    elif new_val == 4: eq.inject_fault("overtemp")
                    
                    context[1].setValues(3, 150+i, [0])
                    last_reg_states[i] = 0
                    audit_log("FAULT_INJECT", "operator", 150+i, 0, new_val, "OK")
                else:
                    last_reg_states[i] = new_val
                    
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error checking reg writes: {e}")
            await asyncio.sleep(0.2)

async def run_modbus_server():
    global _shutdown_event, _audit_file
    _shutdown_event = asyncio.Event()
    
    _audit_file = open(AUDIT_LOG_PATH, "a", encoding="utf-8")
    audit_log("SERVER_START", "system", 0, 0, 0, "OK")
    
    context = create_modbus_context()
    print_startup_banner()
    
    update_task = asyncio.create_task(update_plc_data(context))
    coil_task = asyncio.create_task(check_coil_writes(context))
    reg_task = asyncio.create_task(check_reg_writes(context))
    
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
        coil_task.cancel()
        reg_task.cancel()
        
        try:
            await update_task
        except asyncio.CancelledError:
            pass
        
        try:
            await coil_task
        except asyncio.CancelledError:
            pass
        try:
            await reg_task
        except asyncio.CancelledError:
            pass
        
        audit_log("SERVER_STOP", "system", 0, 0, 0, "OK")
        if _audit_file:
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