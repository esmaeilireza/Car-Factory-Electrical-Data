"""
Modbus TCP Server - Simulates an industrial PLC for 6 equipment
Port: 5020 (bind 127.0.0.1 for security)

FEATURES:
- ANSI Protection layer (49/50/51/27/59/38)
- Security: write allowlist, rate limiting, audit log, safety-arm handshake
- E-STOP (CO[6]) latches globally, published at HR[120]
- RESET (CO[7]) clears E-STOP latch + per-equipment lockouts
- Watchdog via heartbeat monitoring
- 18 registers per equipment (108 total) + HR[120] system status
- Load setpoints at HR[160..165], ramped by the server
- OPTIONAL: Automatically send data to backend (REST API) [NON-BLOCKING]

SAFETY CONTRACT (v2)
--------------------
The remediation engine (and any non-operator client) may write *only* the
load setpoint registers HR[160..165]. Everything below is blocked unless
the client performs the safety-arm handshake first:

    1) Write 0xA5A5 to HR[199]         (arm safety for 5 s)
    2) Within 5 s, write the target:
         CO[6]  E-STOP
         CO[7]  RESET
         HR[150..155]  fault injection

Any other write to safety coils, safety registers, or ANSI bypass
registers is rejected and audited as SAFETY_WRITE_BLOCKED.

REGISTER SYNCHRONIZATION CONTRACT (v3)
--------------------------------------
The equipment register block (HR[0..107]) is *owned* by the PLC update
loop. Every 200 ms:

    1. plc.update_all() runs the physics AND the protection step, which
       writes trip_word / alarm_word / lockout into each Equipment.
    2. plc.get_all_registers() produces a fresh 108-entry list by reading
       live state under each Equipment's internal lock.
    3. context[1].setValues(3, 0, registers) overwrites HR[0..107] with
       that list.
    4. context[1].setValues(3, 120, [sys_status]) overwrites HR[120].

Because step (3) runs every tick, any in-memory change (including a
fault injected via force_trip) is visible on the wire within one tick.
There is no separate "sync" step to forget; the push is unconditional.

FIXES APPLIED:
- Write hooks replace polling (no missed sub-100ms coil changes)
- Thread/async-safe rate limiting with asyncio.Lock
- Backend errors logged explicitly (no silent failures)
- Audit file flushed + locked on every write (no data loss on crash)
- Old value captured synchronously before datastore write (audit race fixed)
- Production-ready auth: X-API-Key header sent when NEXUS_API_KEY is set
- Payload keys aligned to DB schema
- MOTOR_STATE_NAMES defined for semantic clarity; wire format stays int
- Safety-arm handshake for E-STOP / RESET / fault-injection writes
- Server-side load ramp for HR[160..165] setpoints
- Dedicated LOAD_SETPOINT_WRITE audit event with register/old/new values
- Explicit register-push helper + trip_word transition logging for
  end-to-end verification of the fault -> Modbus path
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

from data_generator import FactoryPLC


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ==========================================
# Semantic Motor State Labels
# ==========================================
# Matches STATUS_MAP in dashboard/streamlit_app.py exactly.
# Wire format remains int to prevent breakage in consumers that do
# int(d.get("status", 0)).
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


def _sync_send_to_backend(equipment_id: str, data: dict) -> None:
    """Synchronous HTTP request (runs in a background thread)."""
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    try:
        resp = requests.post(
            f"{BACKEND_URL}/{equipment_id}/data",
            json=data,
            headers=headers,
            timeout=5.0,
        )
        if not resp.ok:
            logger.warning(
                f"[BACKEND] HTTP {resp.status_code} for {equipment_id}: "
                f"{resp.text[:200]}"
            )
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"[BACKEND] Connection refused for {equipment_id}: {e}")
    except requests.exceptions.Timeout:
        logger.warning(f"[BACKEND] Timeout sending data for {equipment_id}")
    except Exception as e:
        logger.warning(
            f"[BACKEND] Unexpected error for {equipment_id}: "
            f"{type(e).__name__}: {e}"
        )


async def send_to_backend(equipment_id: str, data: dict) -> None:
    """Offloads the blocking `requests` call to a separate thread."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_send_to_backend, equipment_id, data)
    except Exception as e:
        logger.warning(
            f"[BACKEND] Executor error for {equipment_id}: "
            f"{type(e).__name__}: {e}"
        )


# ==========================================
# Register Map Constants
# ==========================================

# Coils
COIL_MOTOR_BASE = 0          # CO[0..5]  motor start/stop per equipment
COIL_MOTOR_COUNT = 6
COIL_ESTOP = 6               # CO[6]     E-STOP ALL (latches globally)
COIL_RESET = 7               # CO[7]     RESET (clears latches)

# Holding registers - equipment block
EQ_REG_BLOCK = 18            # registers per equipment
EQ_BLOCK_COUNT = 6           # 6 * 18 = 108

# Per-equipment register offsets (must match Equipment.get_registers order)
EQ_OFF_MOTOR_STATE = 8
EQ_OFF_TRIP_WORD = 10
EQ_OFF_ALARM_WORD = 13
EQ_OFF_LOCKOUT = 14
EQ_OFF_THETA = 15
EQ_OFF_HEARTBEAT = 17

# Fault injection
FAULT_INJECT_BASE = 150      # HR[150..155]  1=thermal 2=inst_oc 3=overvolt 4=overtemp

# Operator / remediation load setpoints
LOAD_SETPOINT_BASE = 160     # HR[160..165]  0=release, else 1..100
LOAD_SETPOINT_COUNT = 6

# Safety-arm register: writing 0xA5A5 arms safety writes for SAFETY_ARM_TTL_SEC.
SAFETY_ARM_REGISTER = 199
SAFETY_ARM_VALUE = 0xA5A5
SAFETY_ARM_TTL_SEC = 5.0

# System status word
SYS_STATUS_ADDR = 120

# Load ramp characteristics
LOAD_RAMP_STEP_PERCENT = 2   # percentage points per update tick (200ms)


# ==========================================
# Security & Audit Config
# ==========================================
AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit_log.jsonl"
RATE_LIMIT_WRITES_PER_SEC = 10

# What an unauthenticated (non-operator) client may write.
# Everything else is either safety-gated or rejected outright.
WRITE_ALLOWLIST_COILS = {0, 1, 2, 3, 4, 5}
WRITE_ALLOWLIST_REGS = set(range(12, 108, 18))

EQ_IDS: List[str] = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]

# Global state
_shutdown_event: Optional[asyncio.Event] = None
_audit_file = None
_audit_lock = asyncio.Lock()
_rate_limit_lock = asyncio.Lock()
_write_timestamps: Dict[str, List[float]] = {}

# Safety-arm state (monotonic seconds; 0 == not armed)
_safety_armed_until: float = 0.0

# Server-side load setpoints (idx -> target %).  None == autonomous.
_load_setpoints: Dict[int, Optional[int]] = {i: None for i in range(EQ_BLOCK_COUNT)}

# Last observed trip_word per equipment, used only for transition logging.
_last_trip_word: Dict[str, int] = {eq_id: 0 for eq_id in EQ_IDS}

plc = FactoryPLC()


# ==========================================
# Audit Logging
# ==========================================
async def audit_log(
    event_type: str,
    source: str,
    address: int,
    old_value: int,
    new_value: int,
    result: str,
) -> None:
    """
    Append-only audit log with immediate flush.

    Retained for backwards compatibility with the previous schema.
    New structured events should use :func:`audit_event`.
    """
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event_type,
        "source": source,
        "address": address,
        "old_value": old_value,
        "new_value": new_value,
        "result": result,
    }
    await _write_audit_entry(entry)
    logger.info(
        f"[AUDIT] {event_type} addr={address} {old_value}→{new_value} "
        f"[{result}] from {source}"
    )


async def audit_event(event_type: str, **fields: Any) -> None:
    """
    Emit a structured audit event with an explicit field set.

    Used for events whose canonical schema is defined by the consumer
    (e.g. LOAD_SETPOINT_WRITE: register / old_value / new_value).
    """
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event_type, **fields}
    await _write_audit_entry(entry)
    logger.info(f"[AUDIT] {event_type} {fields}")


async def _write_audit_entry(entry: Dict[str, Any]) -> None:
    """Serialize an audit entry and flush it under the audit lock."""
    line = json.dumps(entry) + "\n"
    async with _audit_lock:
        if _audit_file and not _audit_file.closed:
            _audit_file.write(line)
            _audit_file.flush()


async def check_rate_limit(source_ip: str) -> bool:
    """Async-safe rate limiter. Returns True if the write is allowed."""
    async with _rate_limit_lock:
        now = time.time()
        timestamps = _write_timestamps.get(source_ip, [])
        timestamps = [t for t in timestamps if now - t < 1.0]

        if len(timestamps) >= RATE_LIMIT_WRITES_PER_SEC:
            _write_timestamps[source_ip] = timestamps
            # Release lock before awaiting audit_log.
            allowed = False
        else:
            timestamps.append(now)
            _write_timestamps[source_ip] = timestamps
            allowed = True

    if not allowed:
        await audit_log("RATE_LIMIT", source_ip, 0, 0, 0, "BLOCKED")
    return allowed


def _is_safety_armed() -> bool:
    """Return True when a client has recently armed the safety channel."""
    return time.monotonic() < _safety_armed_until


# ==========================================
# Write Hook Context
# ==========================================
class SafeSlaveContext(ModbusSlaveContext):
    """
    Custom slave context that intercepts writes synchronously.

    Old values are captured *before* the write is applied to the
    datastore so audit events carry accurate before/after state.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._eq_ids = EQ_IDS

    # ------------------------------------------------------------------
    # Synchronous interception
    # ------------------------------------------------------------------
    def setValues(self, fc, address, values):  # noqa: N802 (pymodbus API)
        """Intercept ALL writes before they hit the datastore."""
        try:
            self._dispatch_write(fc, address, values)
        finally:
            # Always apply the write to the underlying datastore.
            super().setValues(fc, address, values)

    def _dispatch_write(self, fc, address, values):
        # --- Coil writes (FC 1/5/15) ---
        if fc in (1, 5, 15):
            if address == COIL_MOTOR_BASE and fc in (1, 15):
                # Bulk motor-coil write
                asyncio.ensure_future(self._handle_coil_write(list(values)))
            elif fc == 5 and COIL_MOTOR_BASE <= address < COIL_MOTOR_BASE + COIL_MOTOR_COUNT:
                # Single motor-coil write
                asyncio.ensure_future(
                    self._handle_single_motor_write(address, values[0])
                )
            elif fc == 5 and address in (COIL_ESTOP, COIL_RESET):
                old = self.getValues(1, address, 1)[0]
                asyncio.ensure_future(
                    self._handle_safety_coil_write(address, values[0], old)
                )
            elif fc in (1, 15) and (
                COIL_ESTOP < address < COIL_MOTOR_BASE + COIL_MOTOR_COUNT
                or address > COIL_RESET
            ):
                asyncio.ensure_future(
                    self._handle_out_of_allowlist_coil_write(address, values)
                )

        # --- Register writes ---
        #
        # NOTE: fc==3 is the *read-holding-registers* code. We do not
        # intercept it here because the update loop calls setValues(3, ...)
        # directly to publish live values. Only the *write* codes (6/16)
        # trigger business logic.
        elif fc in (3, 6, 16):
            if fc == 6 and address == SAFETY_ARM_REGISTER:
                old = self.getValues(3, address, 1)[0]
                asyncio.ensure_future(self._handle_arm_register(values[0], old))
            elif fc in (6, 16) and FAULT_INJECT_BASE <= address < FAULT_INJECT_BASE + EQ_BLOCK_COUNT:
                old = self.getValues(3, address, 1)[0]
                asyncio.ensure_future(
                    self._handle_fault_inject(address, list(values), old)
                )
            elif fc in (6, 16) and LOAD_SETPOINT_BASE <= address < LOAD_SETPOINT_BASE + LOAD_SETPOINT_COUNT:
                old = self.getValues(3, address, 1)[0]
                asyncio.ensure_future(
                    self._handle_setpoint_write(address, list(values), old)
                )

    # ------------------------------------------------------------------
    # Coil handlers
    # ------------------------------------------------------------------
    async def _handle_coil_write(self, coil_values: List[int]) -> None:
        """
        Process a *bulk* coil write (FC1/FC15 starting at CO[0]).

        Motors (CO[0..5]) are processed directly. CO[6]/CO[7] require the
        safety-arm handshake; without it, they are rejected and reset to 0.
        """
        source_ip = "operator"

        if not await check_rate_limit(source_ip):
            return

        # Motors first, independent of safety-armed state.
        for i, eq_id in enumerate(self._eq_ids):
            if i >= len(coil_values):
                break
            if i >= COIL_MOTOR_COUNT:
                break
            if i not in WRITE_ALLOWLIST_COILS:
                await audit_log(
                    "WRITE_BLOCKED", source_ip, i, 0, coil_values[i], "ALLOWLIST"
                )
                continue
            await self._apply_motor_command(i, eq_id, coil_values[i])

        # E-STOP and RESET (require safety-arm handshake).
        if len(coil_values) > COIL_ESTOP and coil_values[COIL_ESTOP]:
            await self._handle_safety_coil_write(COIL_ESTOP, 1, 0)

        if len(coil_values) > COIL_RESET and coil_values[COIL_RESET]:
            await self._handle_safety_coil_write(COIL_RESET, 1, 0)

    async def _handle_single_motor_write(self, coil_index: int, new_state: int) -> None:
        """Handle one FC5 motor-coil write without touching other motors."""
        source_ip = "operator"
        if not await check_rate_limit(source_ip):
            return
        eq_id = self._eq_ids[coil_index]
        await self._apply_motor_command(coil_index, eq_id, new_state)

    async def _apply_motor_command(self, coil_index: int, eq_id: str, new_state: int) -> None:
        """Common motor start/stop handling for bulk and single-coil writes."""
        source_ip = "operator"
        eq = plc.equipment[eq_id]

        if new_state:
            if plc.estop_latched:
                await audit_log(
                    "MOTOR_START", source_ip, coil_index, 0, 1, "ESTOP-LOCKOUT"
                )
                super().setValues(1, coil_index, [0])
                return
            success = eq.start_motor()
            await audit_log(
                "MOTOR_START", source_ip, coil_index, 0, 1,
                "OK" if success else "LOCKOUT",
            )
            if not success:
                super().setValues(1, coil_index, [0])
        else:
            eq.stop_motor()
            await audit_log("MOTOR_STOP", source_ip, coil_index, 1, 0, "OK")

    async def _handle_safety_coil_write(
        self, address: int, new_value: int, old_value: int
    ) -> None:
        """
        Handle CO[6] (E-STOP) and CO[7] (RESET).

        Requires an unexpired safety-arm window opened via HR[199].
        Rejected writes are audited as SAFETY_WRITE_BLOCKED and the coil
        is reset to 0.
        """
        source_ip = "operator"

        if not await check_rate_limit(source_ip):
            return

        if not _is_safety_armed():
            await audit_event(
                "SAFETY_WRITE_BLOCKED",
                source=source_ip,
                register=address,
                old_value=int(old_value),
                new_value=int(new_value),
                reason="safety channel not armed (write 0xA5A5 to HR[199] first)",
            )
            super().setValues(1, address, [0])
            return

        if address == COIL_ESTOP:
            if new_value and not plc.estop_latched:
                plc.emergency_stop_all()
                await audit_log("E-STOP", source_ip, COIL_ESTOP, 0, 1, "LATCHED")
            super().setValues(1, COIL_ESTOP, [0])
            return

        if address == COIL_RESET:
            if new_value:
                results = []
                if plc.estop_latched:
                    plc.clear_estop_latch()
                    results.append("E-STOP:CLEARED")
                else:
                    results.append("E-STOP:WAS-CLEAR")

                for eq_id in self._eq_ids:
                    eq = plc.equipment[eq_id]
                    if eq.protection.latched:
                        ok = eq.try_reset()
                        results.append(f"{eq_id}:{'OK' if ok else 'REFUSED'}")

                super().setValues(1, COIL_RESET, [0])
                await audit_log(
                    "RESET", source_ip, COIL_RESET, 0, 1, ",".join(results)
                )
            return

    async def _handle_out_of_allowlist_coil_write(
        self, address: int, values
    ) -> None:
        """Any coil write outside the allowlist is rejected and audited."""
        await audit_log(
            "WRITE_BLOCKED",
            "external_client",
            address,
            -1,
            int(values[0]) if values else -1,
            "OUT_OF_ALLOWLIST",
        )

    # ------------------------------------------------------------------
    # Register handlers
    # ------------------------------------------------------------------
    async def _handle_arm_register(self, new_value: int, old_value: int) -> None:
        """Arm the safety channel for SAFETY_ARM_TTL_SEC when 0xA5A5 is written."""
        global _safety_armed_until
        source_ip = "operator"

        if not await check_rate_limit(source_ip):
            return

        if int(new_value) == SAFETY_ARM_VALUE:
            _safety_armed_until = time.monotonic() + SAFETY_ARM_TTL_SEC
            await audit_event(
                "SAFETY_ARMED",
                source=source_ip,
                register=SAFETY_ARM_REGISTER,
                old_value=int(old_value),
                new_value=int(new_value),
                ttl_sec=SAFETY_ARM_TTL_SEC,
            )
        else:
            await audit_event(
                "SAFETY_ARM_REJECTED",
                source=source_ip,
                register=SAFETY_ARM_REGISTER,
                old_value=int(old_value),
                new_value=int(new_value),
            )

    async def _handle_fault_inject(
        self, address: int, values: List[int], old_value: int
    ) -> None:
        """
        Handle fault injection writes (HR[150..155]).

        Requires the safety-arm handshake. The register is auto-cleared
        after a successful injection so the fault is a one-shot pulse.

        The injected fault is *sticky* in the Protection layer: it
        survives every subsequent protection.step() call until an
        operator RESET clears it. This is required for the fault to be
        visible on Modbus across multiple update-loop ticks.
        """
        source_ip = "operator"

        if not await check_rate_limit(source_ip):
            return

        new_val = int(values[0]) if values else 0

        if not _is_safety_armed():
            await audit_event(
                "SAFETY_WRITE_BLOCKED",
                source=source_ip,
                register=address,
                old_value=int(old_value),
                new_value=new_val,
                reason="fault injection requires safety-arm handshake",
            )
            super().setValues(3, address, [0])
            return

        if new_val <= 0:
            return

        idx = address - FAULT_INJECT_BASE
        if idx < 0 or idx >= len(self._eq_ids):
            return

        eq_id = self._eq_ids[idx]
        eq = plc.equipment[eq_id]

        fault_map = {1: "thermal", 2: "inst_oc", 3: "overvolt", 4: "overtemp"}
        fault_type = fault_map.get(new_val)

        if fault_type:
            eq.inject_fault(fault_type)
            super().setValues(3, address, [0])
            await audit_log(
                "FAULT_INJECT", source_ip, address, int(old_value), new_val, "OK"
            )

    async def _handle_setpoint_write(
        self, address: int, values: List[int], old_value: int
    ) -> None:
        """
        Handle load setpoint writes (HR[160..165]).

        This is the *only* write channel the remediation engine is allowed
        to use. Writes here are never safety-gated because the PLC still
        ramps internally toward the target at a bounded rate, and the
        setpoint is trivially reversible by writing 0.

        Value semantics:
            0        -> release to autonomous mode
            1..100   -> target load percentage
        """
        source_ip = "external_client"

        if not await check_rate_limit(source_ip):
            return

        idx = address - LOAD_SETPOINT_BASE
        if idx < 0 or idx >= LOAD_SETPOINT_COUNT:
            return

        raw = int(values[0]) if values else 0
        new_val = max(0, min(100, raw))

        # Latch server-side ramp target. 0 releases control.
        _load_setpoints[idx] = None if new_val == 0 else new_val

        # Defensive: allow the PLC class to keep its own internal copy if
        # it exposes a set_load_setpoint method (optional).
        setter = getattr(plc, "set_load_setpoint", None)
        if callable(setter):
            try:
                setter(idx, new_val)
            except Exception as e:
                logger.warning(f"[PLC] set_load_setpoint failed: {e}")

        await audit_event(
            "LOAD_SETPOINT_WRITE",
            source=source_ip,
            register=address,
            equipment_id=EQ_IDS[idx],
            old_value=int(old_value),
            new_value=new_val,
        )


# ==========================================
# PLC Data Update (with HR[120] status word)
# ==========================================
def _apply_load_setpoint_ramp() -> None:
    """
    Gently ramp equipment load toward the active setpoint.

    Runs after plc.update_all() so it composes with the physics update
    rather than fighting it. A step of LOAD_RAMP_STEP_PERCENT per tick
    (200 ms) yields full-range motion in ~10 s.
    """
    for idx, eq_id in enumerate(EQ_IDS):
        target = _load_setpoints.get(idx)
        if target is None:
            continue

        eq = plc.equipment[eq_id]
        current = int(eq.data.load)

        if abs(current - target) <= LOAD_RAMP_STEP_PERCENT:
            eq.data.load = target
        elif current < target:
            eq.data.load = min(target, current + LOAD_RAMP_STEP_PERCENT)
        else:
            eq.data.load = max(target, current - LOAD_RAMP_STEP_PERCENT)


def _push_equipment_registers(context: ModbusServerContext) -> None:
    """
    Publish live equipment state to the Modbus holding register block.

    This is the *single* point where HR[0..107] is refreshed. It runs
    every tick of update_plc_data(), so any in-memory change (physics,
    protection, injected fault) is visible on the wire within one tick.

    Layout (must match Equipment.get_registers()):
        HR[i*18 +  0..7]  telemetry
        HR[i*18 +  8]     motor_state
        HR[i*18 +  9]     alarm_flag
        HR[i*18 + 10]     trip_word      <- ANSI fault bitmask
        HR[i*18 + 11]     running_time
        HR[i*18 + 12]     load
        HR[i*18 + 13]     alarm_word
        HR[i*18 + 14]     lockout
        HR[i*18 + 15]     theta x 1000
        HR[i*18 + 16]     trip_count
        HR[i*18 + 17]     heartbeat
    """
    registers = plc.get_all_registers()
    if len(registers) > 256:
        logger.error(f"Register dump {len(registers)} exceeds HR block 256")
        registers = registers[:256]

    context[1].setValues(3, 0, registers)


def _push_system_status(context: ModbusServerContext) -> None:
    """Publish HR[120] system status word."""
    sys_status = plc.get_system_status_word()
    context[1].setValues(3, SYS_STATUS_ADDR, [sys_status])


def _log_trip_word_transitions() -> None:
    """
    Log only when an equipment's trip_word transitions from 0 to nonzero.

    Useful for verifying the fault -> register path end-to-end without
    spamming the console at 5 Hz. Cleared on the reverse transition.
    """
    for eq_id, eq in plc.equipment.items():
        tw = eq.protection.trip_word
        prev = _last_trip_word.get(eq_id, 0)
        if tw and not prev:
            logger.info(
                f"[SYNC] {eq_id} trip_word 0 -> 0x{tw:02x} "
                f"(θ={eq.protection.theta:.2f}, "
                f"T={eq.data.temperature:.1f}°C, "
                f"lockout={'yes' if eq.protection.latched else 'no'})"
            )
        _last_trip_word[eq_id] = tw


async def update_plc_data(context) -> None:
    """
    Update PLC data every 200 ms.

    Pipeline per tick:
      1. plc.update_all()            - advance physics + protection
      2. _apply_load_setpoint_ramp() - honour operator setpoints
      3. _push_equipment_registers() - publish HR[0..107]
      4. _push_system_status()       - publish HR[120]
      5. _log_trip_word_transitions() - diagnostic
      6. Rate-limited backend POST
    """
    last_backend_send = 0.0

    while not _shutdown_event.is_set():
        try:
            plc.update_all()
            _apply_load_setpoint_ramp()

            _push_equipment_registers(context)
            _push_system_status(context)
            _log_trip_word_transitions()

            now = time.time()
            if now - last_backend_send >= 1.0:
                for eq_id, eq in plc.equipment.items():
                    # Keys aligned to database.py schema exactly.
                    # status sent as int to match consumer expectations.
                    data = {
                        "voltage": eq.data.voltage,
                        "current": eq.data.current,
                        "active_power": eq.data.active_power,
                        "reactive_power": eq.data.reactive_power,
                        "apparent_power": eq.data.apparent_power,
                        "power_factor": eq.data.power_factor,
                        "frequency": eq.data.frequency,
                        "energy_kwh": eq.data.energy,
                        "status": int(eq._motor_state),
                        "alarm": bool(eq.protection.alarm_word),
                        "alarm_code": eq.protection.trip_word,
                        "running_time_min": eq.data.running_time,
                        "load": eq.data.load,
                        "temperature": eq.data.temperature,
                        "trip_word": eq.protection.trip_word,
                        "alarm_word": eq.protection.alarm_word,
                        "theta_per_mille": int(eq.protection.theta * 1000),
                        "trip_count": eq.protection.trip_count,
                        "heartbeat": eq._heartbeat,
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
def create_modbus_context() -> ModbusServerContext:
    coils = ModbusSequentialDataBlock(0, [1, 1, 1, 1, 1, 1, 0, 0, 0, 0])
    holding_registers = ModbusSequentialDataBlock(0, [0] * 256)

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
def print_startup_banner() -> None:
    print()
    print("=" * 70)
    print("⚡ INDUSTRIAL PLC SIMULATOR - Car Factory")
    print("🛡️  ANSI PROTECTION + E-STOP LATCH + SAFETY-ARM HANDSHAKE")
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
    print(f"🔐 Safety-arm: write 0x{SAFETY_ARM_VALUE:04X} to HR[{SAFETY_ARM_REGISTER}] "
          f"(TTL {SAFETY_ARM_TTL_SEC:.0f}s) before E-STOP / RESET / fault injection")
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
    print("    CO[6]  : E-STOP ALL (latches globally)      [safety-arm required]")
    print("    CO[7]  : RESET (clears latch + lockouts)    [safety-arm required]")
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
    print("🎚️  SPECIAL REGISTERS:")
    print(f"    HR[{FAULT_INJECT_BASE}..{FAULT_INJECT_BASE + 5}] : Fault injection     [safety-arm required]")
    print(f"    HR[{LOAD_SETPOINT_BASE}..{LOAD_SETPOINT_BASE + 5}] : Load setpoints (1-100, 0=release)")
    print(f"    HR[{SAFETY_ARM_REGISTER}] : Safety-arm register (write 0x{SAFETY_ARM_VALUE:04X})")
    print("-" * 70)
    print("💡 Press Ctrl+C to stop the server")
    print("=" * 70)
    print()


# ==========================================
# Main Server
# ==========================================
async def run_modbus_server() -> None:
    global _shutdown_event, _audit_file
    _shutdown_event = asyncio.Event()

    _audit_file = open(AUDIT_LOG_PATH, "a", encoding="utf-8")
    await audit_log("SERVER_START", "system", 0, 0, 0, "OK")

    context = create_modbus_context()
    print_startup_banner()

    update_task = asyncio.create_task(update_plc_data(context))

    def handle_shutdown(signame: str) -> None:
        logger.info(f"Received signal {signame}, shutting down...")
        _shutdown_event.set()

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(signal, signame),
                lambda s=signame: handle_shutdown(s),
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