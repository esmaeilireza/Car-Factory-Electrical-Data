"""
Data Generator for Industrial PLC Simulator (Refactored)
Simulates 6 equipment from a car factory with thread-safe state management.

FIXES APPLIED:
- Explicit MotorState enum eliminates UI lag during start delay
- Separated alarm/trip/lockout concerns (no more bit 15 overload)
- Thread-safe state mutations via internal locking
- Clean separation between warnings (alarms) and faults (trips)
- Idempotent emergency_stop() prevents console flood when E-STOP is latched
- inject_fault() now drives the physical model (θ, I, V, T) in addition to
  setting the trip bit, and Protection.force_trip() is sticky so the bit
  survives every step() tick until try_reset() is called.
"""

import math
import random
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional


# ==========================================
# CONSTANTS & CONFIGURATION
# ==========================================

PROT_CONFIG = {
    "I_n_base": 1.0,
    "V_n": 400.0,
    "tau": 300.0,
    "TMS": 0.10,
    "temp_trip": 95.0,
    "temp_alarm": 80.0,
    "ambient": 35.0,
}

MOTOR_START_DELAY_SEC = 2.5

# System Status Word Bits (HR[120])
SYS_STATUS_ESTOP = 1 << 0
SYS_STATUS_ANY_TRIP = 1 << 1


class MotorState(IntEnum):
    """Explicit motor states for unambiguous UI feedback."""
    STOPPED = 0
    RUNNING = 1
    START_PENDING = 2
    FAULT_LOCKOUT = 3


@dataclass(frozen=True)
class EquipmentConfig:
    """Immutable static configuration for a piece of equipment."""
    equipment_id: str
    equipment_name: str
    area: str
    rated_kw: float
    base_current: float
    rated_pf: float
    pf_target: float
    profile: str


@dataclass
class ElectricalData:
    """Live electrical measurements for one equipment."""
    equipment_id: str = ""
    voltage: float = 380.0
    current: float = 0.0
    active_power: float = 0.0
    reactive_power: float = 0.0
    apparent_power: float = 0.0
    power_factor: float = 0.0
    frequency: float = 50.0
    energy: float = 0.0
    running_time: float = 0.0
    load: float = 0.0
    temperature: float = 35.0


# ==========================================
# PROTECTION LAYER
# ==========================================

class Protection:
    """
    ANSI protection relay simulation.

    Trip and alarm words are SEPARATE bitmasks:
      trip_word  -> Faults that cause lockout (bits 0-5)
      alarm_word -> Warnings only, no lockout (bits 6-7)

    Injected faults are "sticky": force_trip() records the bit in
    _forced_trip_bits, which step() ORs back into trip_word every cycle.
    A human operator RESET (try_reset) is the only thing that clears them.
    """

    # Trip bits (cause lockout)
    TRIP_THERMAL = 1 << 0       # 49 Thermal Overload
    TRIP_INST_OC = 1 << 1       # 50 Instantaneous OC
    TRIP_TIME_OC = 1 << 2       # 51 Time Overcurrent
    TRIP_UNDERVOLT = 1 << 3     # 27 Undervoltage
    TRIP_OVERVOLT = 1 << 4      # 59 Overvoltage
    TRIP_OVERTEMP = 1 << 5      # 38 Over-Temperature

    # Alarm bits (warning only)
    ALARM_PHASE_IMBALANCE = 1 << 6  # 46 Phase Imbalance
    ALARM_LOSS_OF_LOAD = 1 << 7     # 37 Loss of Load

    def __init__(self, I_n: float):
        self.I_n = I_n
        self.V_n = PROT_CONFIG["V_n"]
        self.tau = PROT_CONFIG["tau"]
        self.TMS = PROT_CONFIG["TMS"]

        self.theta = 0.0
        self.trip_word = 0
        self.alarm_word = 0
        self.latched = False
        self.t_oc = 0.0
        self.t_uv = 0.0
        self.trip_count = 0
        self.last_trip_time = 0.0

        # Sticky fault bits injected via force_trip(); cleared on try_reset().
        self._forced_trip_bits = 0

    def step(self, I: float, V: float, temp: float, running: bool, dt: float):
        """Execute one protection cycle. Updates trip/alarm words independently."""
        trips = 0
        alarms = 0

        # -- 49 Thermal Overload --
        if running:
            i_ratio_sq = (I / self.I_n) ** 2 if self.I_n > 0 else 0
            self.theta += (dt / self.tau) * (i_ratio_sq - self.theta)
        else:
            self.theta += (dt / (self.tau * 2)) * (0 - self.theta)
        self.theta = max(0.0, self.theta)

        if self.theta > 1.0:
            trips |= self.TRIP_THERMAL
        elif self.theta > 0.85:
            alarms |= self.TRIP_THERMAL  # Reuse bit position for alarm indication

        # -- 50 Instantaneous OC --
        if I > 2.5 * self.I_n:
            trips |= self.TRIP_INST_OC

        # -- 51 Time OC (IEC Standard Inverse) --
        if running and I > 1.15 * self.I_n:
            i_ratio = I / self.I_n
            denom = (i_ratio ** 0.02) - 1
            if denom > 0:
                t_req = (0.14 * self.TMS) / denom
                self.t_oc += dt
                if self.t_oc >= t_req:
                    trips |= self.TRIP_TIME_OC
            else:
                self.t_oc = 0.0
        else:
            self.t_oc = max(0.0, self.t_oc - dt * 0.5)

        # -- 27 Undervoltage (2s delay) --
        if running and V < 0.85 * self.V_n:
            self.t_uv += dt
            if self.t_uv >= 2.0:
                trips |= self.TRIP_UNDERVOLT
        else:
            self.t_uv = 0.0

        # -- 59 Overvoltage --
        if V > 1.10 * self.V_n:
            trips |= self.TRIP_OVERVOLT

        # -- 38 Over-Temperature --
        if temp > PROT_CONFIG["temp_trip"]:
            trips |= self.TRIP_OVERTEMP
        elif temp > PROT_CONFIG["temp_alarm"]:
            alarms |= self.TRIP_OVERTEMP

        # -- 46 Phase Imbalance (alarm only) --
        if running and random.random() < 0.0001:
            alarms |= self.ALARM_PHASE_IMBALANCE

        # -- 37 Loss of Load (alarm only) --
        if running and 0 < I < 0.3 * self.I_n:
            alarms |= self.ALARM_LOSS_OF_LOAD

        # Sticky injected faults survive every tick until try_reset().
        trips |= self._forced_trip_bits

        self.trip_word = trips
        self.alarm_word = alarms

        # Latch on first trip event
        if trips and not self.latched:
            self.latched = True
            self.trip_count += 1
            self.last_trip_time = time.time()

    def try_reset(self, I: float, V: float, temp: float) -> bool:
        """Attempt reset. Refuses if electrical conditions are still unsafe."""
        if I > 1.15 * self.I_n:
            return False
        if V < 0.85 * self.V_n or V > 1.10 * self.V_n:
            return False

        self.latched = False
        self.trip_word = 0
        self.alarm_word = 0
        self.t_oc = 0.0
        self.t_uv = 0.0
        self.theta = 0.0
        # Release injected faults on explicit operator reset.
        self._forced_trip_bits = 0
        return True

    def force_trip(self, ansi_bit: int):
        """
        Force a specific ANSI trip for testing.

        The bit is recorded as sticky so step() cannot overwrite it; it
        survives until an operator RESET clears the relay via try_reset().
        """
        self._forced_trip_bits |= (1 << ansi_bit)
        self.trip_word |= (1 << ansi_bit)
        if not self.latched:
            self.latched = True
            self.trip_count += 1
            self.last_trip_time = time.time()


# ==========================================
# EQUIPMENT CLASS (THREAD-SAFE)
# ==========================================

class Equipment:
    """
    Thread-safe factory equipment simulator.

    All public methods acquire an internal lock to prevent race conditions
    between the simulation thread and external callers (Modbus server, UI).
    """

    # Maps the public fault_type strings to ANSI trip bits in Protection.
    # (Kept in sync with Protection.TRIP_* — do not renumber.)
    _FAULT_BIT_MAP = {
        "thermal":   0,   # ANSI-49
        "inst_oc":   1,   # ANSI-50
        "time_oc":   2,   # ANSI-51
        "undervolt": 3,   # ANSI-27
        "overvolt":  4,   # ANSI-59
        "overtemp":  5,   # ANSI-38
    }

    def __init__(self, config: EquipmentConfig):
        self.config = config
        self.data = ElectricalData(equipment_id=config.equipment_id)
        self.protection = Protection(I_n=config.base_current)

        # State machine
        self._motor_state = MotorState.STOPPED
        self._start_pending_until = 0.0
        self._start_time: Optional[float] = None
        self._load_factor = 0.75
        self._base_voltage = 380.0
        self._last_update_time = time.time()
        self._heartbeat = 0

        # Optional operator load setpoint (HR[160+i]).
        # 0 = autonomous, nonzero = latched target percentage.
        self.load_setpoint: float = 0.0
        self._sp_progress: Optional[float] = None
        self._sp_was_active: bool = False

        # Thread safety: protects ALL mutable state
        self._lock = threading.Lock()

    # ---- Public Command Interface (Thread-Safe) ----

    def start_motor(self) -> bool:
        """Request motor start. Returns False if blocked by lockout."""
        with self._lock:
            if self.protection.latched:
                print(f"[PROT] ⛔ {self.config.equipment_id} START inhibited - lockout")
                return False

            self._motor_state = MotorState.START_PENDING
            self._start_pending_until = time.time() + MOTOR_START_DELAY_SEC
            print(
                f"[PLC] ⏳ {self.config.equipment_id} Motor START PENDING "
                f"({MOTOR_START_DELAY_SEC:.1f}s delay)"
            )
            return True

    def stop_motor(self):
        """Stop motor and cancel any pending start."""
        with self._lock:
            self._motor_state = MotorState.STOPPED
            self._start_pending_until = 0.0
            print(f"[PLC] ⏹️  {self.config.equipment_id} Motor STOPPED")

    def emergency_stop(self):
        """Immediate stop + cancel pending start. Idempotent: silent when already stopped."""
        with self._lock:
            if self._motor_state == MotorState.STOPPED:
                return
            self._motor_state = MotorState.STOPPED
            self._start_pending_until = 0.0
            print(f"[E-STOP] {self.config.equipment_id} Emergency stop")

    def try_reset(self, I=None, V=None, temp=None) -> bool:
        """Reset protection lockout using current or provided values."""
        with self._lock:
            if I is None:
                I = self.data.current
            if V is None:
                V = self.data.voltage
            if temp is None:
                temp = self.data.temperature

            success = self.protection.try_reset(I, V, temp)
            if success:
                self._motor_state = MotorState.STOPPED
                print(f"[PROT] 🔓 {self.config.equipment_id} Protection RESET OK")
            else:
                print(f"[PROT] ❌ {self.config.equipment_id} Protection RESET refused")
            return success

    def set_load(self, load_percent: float):
        """Set load factor (0-100%).

        While an operator setpoint (HR[160+i]) is latched, autonomous
        load writes are IGNORED - the operator owns the load factor.
        On release (setpoint 0), autonomous generation resumes.
        """
        with self._lock:
            if getattr(self, "load_setpoint", 0.0) > 0:
                return
            self._load_factor = max(0.0, min(1.0, load_percent / 100.0))

    def inject_fault(self, fault_type: str) -> bool:
        """
        Inject a test fault.

        Drives BOTH the deterministic trip relay (via force_trip, which is
        sticky) AND the physical model (temperature / current / voltage /
        thermal capacity), so any downstream consumer — rule engine, HMI,
        dashboard, audit log — sees a coherent fault condition rather than
        a single bit that step() would otherwise wipe on the next tick.

        Supported: thermal, inst_oc, time_oc, undervolt, overvolt, overtemp.
        """
        with self._lock:
            bit = self._FAULT_BIT_MAP.get(fault_type)
            if bit is None:
                return False

            V_n = PROT_CONFIG["V_n"]
            I_n = self.config.base_current

            # 1) Physical model must agree with the trip.
            if fault_type == "thermal":
                # ANSI-49: θ > 1.0, hot machine, mild current elevation.
                self.protection.theta = max(self.protection.theta, 1.05)
                self.data.temperature = max(self.data.temperature, 96.0)
                self.data.current = max(self.data.current, I_n * 1.30)

            elif fault_type == "inst_oc":
                # ANSI-50: massive current spike.
                self.data.current = max(self.data.current, I_n * 3.00)

            elif fault_type == "time_oc":
                # ANSI-51: moderate sustained overcurrent.
                self.data.current = max(self.data.current, I_n * 1.50)

            elif fault_type == "undervolt":
                # ANSI-27: voltage collapse below 85% V_n.
                self.data.voltage = min(self.data.voltage, V_n * 0.80)

            elif fault_type == "overvolt":
                # ANSI-59: voltage rise above 110% V_n.
                self.data.voltage = max(self.data.voltage, V_n * 1.15)

            elif fault_type == "overtemp":
                # ANSI-38: winding/ambient temperature above trip limit.
                self.data.temperature = max(self.data.temperature, 100.0)

            # 2) Force the trip bit + latch (sticky across step()).
            self.protection.force_trip(bit)

            # 3) Match the state machine to the fault, so UI/HMI is consistent
            #    and further starts are inhibited until a RESET.
            self._motor_state = MotorState.FAULT_LOCKOUT
            self._start_pending_until = 0.0

            print(
                f"[FAULT] ⚠️  {self.config.equipment_id} Injected: {fault_type} "
                f"-> trip_word=0x{self.protection.trip_word:02x} "
                f"θ={self.protection.theta:.2f} "
                f"T={self.data.temperature:.1f}°C "
                f"I={self.data.current:.1f}A "
                f"V={self.data.voltage:.1f}V"
            )
            return True

    # ---- Simulation Update (Thread-Safe) ----

    def update(self):
        """Run one simulation step. Safe to call from a dedicated simulation thread."""
        with self._lock:
            now = time.time()
            dt = now - self._last_update_time
            self._last_update_time = now
            self._heartbeat = (self._heartbeat + 1) % 65536

            # STEP 1: State machine transitions
            if self._motor_state == MotorState.START_PENDING:
                if now >= self._start_pending_until:
                    self._motor_state = MotorState.RUNNING
                    self._start_time = now
                    self._start_pending_until = 0.0
                    print(f"[PLC] ✅ {self.config.equipment_id} Motor STARTED")

            # Check for protection trip -> lockout
            if self.protection.latched and self._motor_state != MotorState.FAULT_LOCKOUT:
                self._motor_state = MotorState.FAULT_LOCKOUT
                self._start_pending_until = 0.0
                print(
                    f"[PROT] ⚡ {self.config.equipment_id} TRIPPED "
                    f"(0x{self.protection.trip_word:02x})"
                )

            # STEP 2: Electrical simulation
            is_running = self._motor_state == MotorState.RUNNING
            self._update_electrical(is_running, dt)

            # STEP 3: Protection evaluation
            self.protection.step(
                I=self.data.current,
                V=self.data.voltage,
                temp=self.data.temperature,
                running=is_running,
                dt=dt,
            )

            # Auto-transition to lockout if tripped while running
            if self.protection.trip_word and is_running:
                self._motor_state = MotorState.FAULT_LOCKOUT
                self._start_pending_until = 0.0
                print(
                    f"[PROT] ⚡ {self.config.equipment_id} TRIPPED "
                    f"(0x{self.protection.trip_word:02x})"
                )

    def _update_electrical(self, is_running: bool, dt: float):
        """Internal electrical model update. MUST be called under lock."""
        if not is_running:
            self.data.voltage = self._base_voltage * (1 + random.uniform(-0.01, 0.01))
            self.data.current = 0.0
            self.data.active_power = 0.0
            self.data.reactive_power = 0.0
            self.data.apparent_power = 0.0
            self.data.power_factor = 0.0
            self.data.load = 0.0
            self._sp_was_active = False
            self.data.temperature += (PROT_CONFIG["ambient"] - self.data.temperature) * 0.01
        else:
            self.data.voltage = self._base_voltage * (1 + random.uniform(-0.02, 0.02))
            base_current = self.config.base_current * self._load_factor
            self.data.current = base_current * (1 + random.uniform(-0.05, 0.05))

            # Load-dependent power factor
            if self._load_factor > 0.3:
                pf = self.config.rated_pf * (0.8 + 0.2 * self._load_factor)
            else:
                pf = self.config.rated_pf * 0.6
            self.data.power_factor = max(0.3, min(0.99, pf + random.uniform(-0.02, 0.02)))

            # Three-phase power
            self.data.active_power = (
                math.sqrt(3) * self.data.voltage * self.data.current
                * self.data.power_factor / 1000
            )
            self.data.apparent_power = (
                math.sqrt(3) * self.data.voltage * self.data.current / 1000
            )
            if self.data.apparent_power > self.data.active_power:
                self.data.reactive_power = math.sqrt(
                    self.data.apparent_power ** 2 - self.data.active_power ** 2
                )
            else:
                self.data.reactive_power = 0.0

            self.data.frequency = 50.0 + random.uniform(-0.1, 0.1)
            self.data.energy += self.data.active_power * (dt / 3600)
            self.data.load = self._load_factor * 100

            # Operator setpoint override (HR[160+i]): ramp actual load
            # toward the operator target while the motor is running, so
            # current/power/thermal all follow realistically. 0 or unset
            # = autonomous generation (no override). Setpoint latches
            # across stop/restart; write 0 to release.
            _sp = getattr(self, "load_setpoint", 0.0)
            if _sp > 0 and is_running:
                prog = getattr(self, "_sp_progress", None)
                if prog is None or not getattr(self, "_sp_was_active", False):
                    prog = self._load_factor * 100.0
                _step = 2.0  # percent per tick
                _delta = _sp - prog
                prog += max(-_step, min(_step, _delta))
                self._sp_progress = prog
                self._sp_was_active = True
                self._load_factor = max(0.0, min(1.0, prog / 100.0))
                self.data.load = prog
            else:
                self._sp_was_active = False

            if self._start_time:
                self.data.running_time = (time.time() - self._start_time) / 60

            # Thermal model
            target_temp = PROT_CONFIG["ambient"] + (self._load_factor * 60)
            self.data.temperature += (target_temp - self.data.temperature) * 0.005
            self.data.temperature += random.uniform(-0.2, 0.2)

    # ---- Register Export (Thread-Safe Read) ----

    def get_registers(self) -> List[int]:
        """
        Return 18 holding registers. Thread-safe snapshot.

        Register map:
          [0]  Voltage        (V × 10)
          [1]  Current        (A × 10)
          [2]  Active Power   (kW × 10)
          [3]  Reactive Power (kVAR × 10)
          [4]  Apparent Power (kVA × 10)
          [5]  Power Factor   (pf × 100)
          [6]  Frequency      (Hz × 10)
          [7]  Energy         (kWh × 100)
          [8]  Motor State    (0=STOP, 1=RUN, 2=PENDING, 3=LOCKOUT)
          [9]  Alarm Flag     (1=any alarm active)
          [10] Trip Word      (fault bitmask, bits 0-5)
          [11] Running Time   (minutes)
          [12] Load           (%, 0-100)
          [13] Alarm Word     (warning bitmask, bits 6-7)
          [14] Lockout Status (1=latched, 0=clear)
          [15] Thermal Cap.   (θ × 1000)
          [16] Trip Count     (cumulative)
          [17] Heartbeat      (0-65535)
        """
        with self._lock:
            return [
                int(self.data.voltage * 10),
                int(self.data.current * 10),
                int(self.data.active_power * 10),
                int(self.data.reactive_power * 10),
                int(self.data.apparent_power * 10),
                int(self.data.power_factor * 100),
                int(self.data.frequency * 10),
                int(self.data.energy * 100),
                int(self._motor_state),
                1 if self.protection.alarm_word else 0,
                self.protection.trip_word,
                int(self.data.running_time),
                int(self.data.load),
                self.protection.alarm_word,
                1 if self.protection.latched else 0,
                int(self.protection.theta * 1000),
                self.protection.trip_count,
                self._heartbeat,
            ]


# ==========================================
# FACTORY PLC (TOP-LEVEL SIMULATOR)
# ==========================================

EQUIPMENT_LIST: List[EquipmentConfig] = [
    EquipmentConfig("STP-01", "Stamping Press Line 1", "Stamping", 450.0, 680.0, 0.88, 0.88, "press"),
    EquipmentConfig("WLD-01", "Body Welding Robots", "Body Shop", 260.0, 395.0, 0.85, 0.85, "welding"),
    EquipmentConfig("PNT-01", "Paint Booth HVAC", "Paint Shop", 350.0, 530.0, 0.92, 0.92, "paint"),
    EquipmentConfig("ASM-01", "Assembly Conveyor", "General Assembly", 120.0, 180.0, 0.90, 0.90, "assembly"),
    EquipmentConfig("UTI-01", "Compressed Air System", "Utilities", 200.0, 305.0, 0.85, 0.85, "compressor"),
    EquipmentConfig("UTI-02", "Chiller Plant", "Utilities", 300.0, 455.0, 0.88, 0.88, "chiller"),
]

EQ_ORDER = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]


class FactoryPLC:
    """
    Top-level PLC simulator managing 6 equipment.
    HR[120] = System Status Word (E-STOP + Any Trip)
    """

    REG_PER_EQ = 18
    SYS_STATUS_ADDR = 120

    def __init__(self):
        self.equipment: Dict[str, Equipment] = {
            cfg.equipment_id: Equipment(cfg) for cfg in EQUIPMENT_LIST
        }
        self.estop_latched = False
        self._estop_lock = threading.Lock()

        # Initialize all motors at 75% load
        for eq in self.equipment.values():
            eq.start_motor()
            eq.set_load(75)

    def update_all(self):
        """Update all equipment. E-STOP latch overrides everything."""
        with self._estop_lock:
            estop_active = self.estop_latched

        if estop_active:
            for eq in self.equipment.values():
                eq.emergency_stop()

        for eq in self.equipment.values():
            eq.update()

    def get_all_registers(self) -> List[int]:
        """Return all holding registers (6 × 18 = 108 registers)."""
        all_regs: List[int] = []
        for eq_id in EQ_ORDER:
            all_regs.extend(self.equipment[eq_id].get_registers())
        return all_regs

    def get_system_status_word(self) -> int:
        """Build HR[120] system status word."""
        word = 0
        with self._estop_lock:
            if self.estop_latched:
                word |= SYS_STATUS_ESTOP
        if any(eq.protection.latched for eq in self.equipment.values()):
            word |= SYS_STATUS_ANY_TRIP
        return word

    def emergency_stop_all(self):
        """Global E-STOP: latch + stop all motors."""
        with self._estop_lock:
            self.estop_latched = True
        for eq in self.equipment.values():
            eq.emergency_stop()
        print("[E-STOP] 🚨 ALL EQUIPMENT STOPPED + LATCHED")

    def set_load_setpoint(self, index: int, value: int) -> None:
        """Operator load setpoint (HR[160+i]).

        0 releases the machine back to autonomous load generation.
        Nonzero latches: while the motor runs, actual load ramps toward
        this target instead of the autonomous load factor.
        """
        if 0 <= index < len(EQ_ORDER):
            eq = self.equipment[EQ_ORDER[index]]
            eq.load_setpoint = float(max(0, min(100, value)))

    def clear_estop_latch(self) -> bool:
        """Clear global E-STOP latch."""
        with self._estop_lock:
            if self.estop_latched:
                self.estop_latched = False
                print("[E-STOP] 🔓 E-STOP latch cleared")
                return True
            return False

    def get_equipment_by_index(self, index: int) -> Optional[Equipment]:
        """Get equipment by index (0-5)."""
        if 0 <= index < len(EQ_ORDER):
            return self.equipment[EQ_ORDER[index]]
        return None