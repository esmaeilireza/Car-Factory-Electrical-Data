"""
Data Generator for Delta PLC Simulator
Simulates 6 equipment from a car factory

NEW: Protection layer with ANSI device functions
NEW: E-STOP global latch with HR[120] status word
NEW: Realistic START PENDING simulation (2.5s delay before motor actually starts)
"""

import math
import random
import time
from dataclasses import dataclass, field
from typing import List, Dict


# ==========================================
# ANSI Protection Constants
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

ANSI_FUNCTIONS = [
    (0, "49", "Thermal Overload", "I²t accumulator > 100%"),
    (1, "50", "Instantaneous OC", "I > 2.5·Iₙ"),
    (2, "51", "Time Overcurrent", "IEC Standard Inverse"),
    (3, "27", "Undervoltage", "V < 0.85·Vₙ for 2s"),
    (4, "59", "Overvoltage", "V > 1.10·Vₙ"),
    (5, "38", "Over-Temperature", "Winding > 95°C"),
    (6, "46", "Phase Imbalance", "Alarm only"),
    (7, "37", "Loss of Load", "I < 0.3·Iₙ"),
]

# === SYSTEM STATUS WORD BIT DEFINITIONS (published at HR[120]) ===
# bit 0: E-STOP latched (global)
# bit 1: any equipment has latched trip
SYS_STATUS_ESTOP = 1 << 0
SYS_STATUS_ANY_TRIP = 1 << 1

# NEW: Realistic motor start delay (seconds)
# Simulates contactor pickup + mechanical acceleration time
MOTOR_START_DELAY_SEC = 2.5


# ==========================================
# Data Classes
# ==========================================

@dataclass
class EquipmentConfig:
    """Static configuration for a piece of equipment."""
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
    motor_status: bool = False
    alarm: bool = False
    alarm_code: int = 0
    running_time: float = 0.0
    load: float = 0.0
    temperature: float = 35.0


# ==========================================
# Protection Layer
# ==========================================

class Protection:
    """
    ANSI 49/50/51/27/59/38 protection per equipment.
    
    Trip and alarm words are bitmasks:
      bit 0: 49 Thermal Overload
      bit 1: 50 Instantaneous OC
      bit 2: 51 Time Overcurrent
      bit 3: 27 Undervoltage
      bit 4: 59 Overvoltage
      bit 5: 38 Over-Temperature
      bit 6: 46 Phase Imbalance (alarm only)
      bit 7: 37 Loss of Load (alarm only)
    """

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

    def step(self, I: float, V: float, temp: float, running: bool, dt: float):
        """
        Execute one protection step.
        
        Updates theta (thermal accumulator), trip_word, alarm_word,
        and latches protection on first trip event.
        """
        trips = 0
        alarms = 0

        # ---- 49 Thermal ----
        if running:
            i_ratio_sq = (I / self.I_n) ** 2 if self.I_n > 0 else 0
            self.theta += (dt / self.tau) * (i_ratio_sq - self.theta)
            self.theta = max(0.0, self.theta)
        else:
            self.theta += (dt / (self.tau * 2)) * (0 - self.theta)
            self.theta = max(0.0, self.theta)

        if self.theta > 1.0:
            trips |= 1 << 0
        elif self.theta > 0.85:
            alarms |= 1 << 0

        # ---- 50 Instantaneous OC ----
        if I > 2.5 * self.I_n:
            trips |= 1 << 1

        # ---- 51 Time OC (IEC Standard Inverse) ----
        if running and I > 1.15 * self.I_n:
            i_ratio = I / self.I_n
            denominator = (i_ratio ** 0.02) - 1
            if denominator > 0:
                t_req = (0.14 * self.TMS) / denominator
                self.t_oc += dt
                if self.t_oc >= t_req:
                    trips |= 1 << 2
            else:
                self.t_oc = 0.0
        else:
            self.t_oc = max(0.0, self.t_oc - dt * 0.5)

        # ---- 27 Undervoltage (2s delay) ----
        if running and V < 0.85 * self.V_n:
            self.t_uv += dt
            if self.t_uv >= 2.0:
                trips |= 1 << 3
        else:
            self.t_uv = 0.0

        # ---- 59 Overvoltage ----
        if V > 1.10 * self.V_n:
            trips |= 1 << 4

        # ---- 38 Over-Temperature ----
        if temp > PROT_CONFIG["temp_trip"]:
            trips |= 1 << 5
        elif temp > PROT_CONFIG["temp_alarm"]:
            alarms |= 1 << 5

        # ---- 46 Phase Imbalance (rare random) ----
        if running and random.random() < 0.0001:
            alarms |= 1 << 6

        # ---- 37 Loss of Load ----
        if running and 0 < I < 0.3 * self.I_n:
            alarms |= 1 << 7

        self.trip_word = trips
        self.alarm_word = alarms

        # Latch on first trip
        if trips and not self.latched:
            self.latched = True
            self.trip_count += 1
            self.last_trip_time = time.time()

        return trips, alarms

    def try_reset(self, I: float, V: float, temp: float) -> bool:
        """
        Attempt to reset protection lockout.
        Refuses if current is still high or voltage is out of range.
        """
        if I > 1.15 * self.I_n:
            return False
        if V < 0.85 * self.V_n or V > 1.10 * self.V_n:
            return False

        # Simulator tweak: instant cooldown for fast testing
        self.latched = False
        self.trip_word = 0
        self.t_oc = 0.0
        self.t_uv = 0.0
        self.theta = 0.0
        return True

    def force_trip(self, ansi_bit: int):
        """Force a specific ANSI trip for testing."""
        self.trip_word |= 1 << ansi_bit
        if not self.latched:
            self.latched = True
            self.trip_count += 1
            self.last_trip_time = time.time()


# ==========================================
# Equipment Class
# ==========================================

class Equipment:
    """
    One piece of factory equipment.
    
    State machine for motor:
      STOPPED  --[start_motor()]--> START PENDING --[2.5s]--> RUNNING
      RUNNING  --[stop_motor()]----> STOPPED
      RUNNING  --[emergency_stop()]--> STOPPED
      *RUNNING --[protection trip]--> STOPPED (latched)
    """

    def __init__(self, config: EquipmentConfig):
        self.config = config
        self.data = ElectricalData(equipment_id=config.equipment_id)
        self.motor_on = False
        self.start_time = None
        self.load_factor = 0.75
        self.base_voltage = 380.0
        self.last_update_time = time.time()
        self.heartbeat = 0
        self.protection = Protection(I_n=config.base_current)

        # NEW: START PENDING state tracking
        # When start_motor() is called, this is set to (now + MOTOR_START_DELAY_SEC)
        # The motor will actually start when time.time() >= this value
        self.start_pending_until = 0.0

    def start_motor(self):
        """
        Request motor start.
        
        Motor enters START PENDING state and actually starts after
        MOTOR_START_DELAY_SEC seconds (simulating contactor pickup +
        mechanical acceleration delay).
        
        Returns:
            bool: True if start was accepted, False if blocked by protection.
        """
        if self.protection.latched:
            print(f"[PROT] ⛔ {self.config.equipment_id} START inhibited - lockout")
            return False

        # Schedule motor start with realistic delay
        self.start_pending_until = time.time() + MOTOR_START_DELAY_SEC
        print(
            f"[PLC] ⏳ {self.config.equipment_id} Motor START PENDING "
            f"({MOTOR_START_DELAY_SEC:.1f}s delay)"
        )
        return True

    def stop_motor(self):
        """Stop the motor and cancel any pending start."""
        self.motor_on = False
        self.start_pending_until = 0.0
        print(f"[PLC] ⏹️  {self.config.equipment_id} Motor STOPPED")

    def emergency_stop(self):
        """Emergency stop - immediately stop motor and cancel pending start."""
        self.motor_on = False
        self.start_pending_until = 0.0
        print(f"[E-STOP] 🚨 {self.config.equipment_id} Emergency stop")

    # ============================================================
    # Reset method – supports both calling styles
    # ============================================================

    def try_reset(self, I=None, V=None, temp=None):
        """
        Reset protection lockout.
        
        Supports both eq.try_reset() and eq.try_reset(I, V, temp).
        Uses current equipment values when called without arguments.
        """
        # If no arguments provided, use current equipment values
        if I is None:
            I = self.data.current
        if V is None:
            V = self.data.voltage
        if temp is None:
            temp = self.data.temperature

        success = self.protection.try_reset(I, V, temp)

        if success:
            print(f"[PROT] 🔓 {self.config.equipment_id} Protection RESET OK")
        else:
            print(f"[PROT] ❌ {self.config.equipment_id} Protection RESET refused")

        return success

    def set_load(self, load_percent: float):
        """Set the load factor (0-100%)."""
        self.load_factor = max(0.0, min(1.0, load_percent / 100.0))

    def inject_fault(self, fault_type: str):
        """
        Inject a specific fault for testing.
        
        Supported fault types:
            thermal, inst_oc, time_oc, undervolt, overvolt, overtemp
        """
        fault_map = {
            "thermal": 0,
            "inst_oc": 1,
            "time_oc": 2,
            "undervolt": 3,
            "overvolt": 4,
            "overtemp": 5,
        }
        if fault_type in fault_map:
            self.protection.force_trip(fault_map[fault_type])
            print(f"[FAULT] ⚠️  {self.config.equipment_id} Injected: {fault_type}")
            return True
        return False

    # ============================================================
    # Main Update Loop
    # ============================================================

    def update(self):
        """
        Update equipment state for one simulation step.
        
        Handles:
          1. START PENDING → RUNNING transition (after delay)
          2. Electrical measurements (voltage, current, power)
          3. Thermal model (temperature)
          4. Protection logic (trip/alarm words)
          5. Auto-trip on protection fault
        """
        now = time.time()
        dt = now - self.last_update_time
        self.last_update_time = now
        self.heartbeat = (self.heartbeat + 1) % 65536

        # ==========================================
        # STEP 1: Check if pending start should transition to running
        # ==========================================
        if not self.motor_on and self.start_pending_until > 0:
            if now >= self.start_pending_until:
                # Pending delay expired - motor actually starts now
                self.motor_on = True
                self.start_time = now
                self.start_pending_until = 0.0
                print(f"[PLC] ✅ {self.config.equipment_id} Motor STARTED (after pending)")

        # ==========================================
        # STEP 2: Electrical state update
        # ==========================================
        if not self.motor_on:
            # Motor is OFF (either stopped or in START PENDING state)
            self.data.voltage = self.base_voltage * (1 + random.uniform(-0.01, 0.01))
            self.data.current = 0.0
            self.data.active_power = 0.0
            self.data.reactive_power = 0.0
            self.data.apparent_power = 0.0
            self.data.power_factor = 0.0
            self.data.motor_status = False
            self.data.load = 0.0
            self.data.temperature += (
                (PROT_CONFIG["ambient"] - self.data.temperature) * 0.01
            )
        else:
            # Motor is RUNNING - full electrical simulation
            self.data.voltage = self.base_voltage * (1 + random.uniform(-0.02, 0.02))
            base_current = self.config.base_current * self.load_factor
            self.data.current = base_current * (1 + random.uniform(-0.05, 0.05))

            # Power factor depends on load
            if self.load_factor > 0.3:
                pf = self.config.rated_pf * (0.8 + 0.2 * self.load_factor)
            else:
                pf = self.config.rated_pf * 0.6
            self.data.power_factor = pf + random.uniform(-0.02, 0.02)
            self.data.power_factor = max(0.3, min(0.99, self.data.power_factor))

            # Three-phase power calculations
            self.data.active_power = (
                math.sqrt(3)
                * self.data.voltage
                * self.data.current
                * self.data.power_factor
                / 1000
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

            # Frequency and energy integration
            self.data.frequency = 50.0 + random.uniform(-0.1, 0.1)
            self.data.energy += self.data.active_power * (dt / 3600)

            # Running time counter
            if self.start_time:
                self.data.running_time = (time.time() - self.start_time) / 60

            self.data.motor_status = True
            self.data.load = self.load_factor * 100

            # Thermal model: temperature rises based on load
            target_temp = PROT_CONFIG["ambient"] + (self.load_factor * 60)
            self.data.temperature += (
                (target_temp - self.data.temperature) * 0.005
            )
            self.data.temperature += random.uniform(-0.2, 0.2)

        # ==========================================
        # STEP 3: Protection step
        # ==========================================
        self.protection.step(
            I=self.data.current,
            V=self.data.voltage,
            temp=self.data.temperature,
            running=self.motor_on,
            dt=dt,
        )

        # Auto-trip on protection fault
        if self.protection.trip_word and self.motor_on:
            self.motor_on = False
            self.start_pending_until = 0.0  # Cancel any pending start
            print(
                f"[PROT] ⚡ {self.config.equipment_id} TRIPPED "
                f"(0x{self.protection.trip_word:02x})"
            )

        self.data.alarm = bool(self.protection.alarm_word)
        self.data.alarm_code = self.protection.trip_word

    # ============================================================
    # Register Export (18 registers per equipment)
    # ============================================================

    def get_registers(self) -> List[int]:
        """
        Return 18 holding registers for this equipment.

        Register map:
          [0]  Voltage       (V * 10)
          [1]  Current       (A * 10)
          [2]  Active Power  (kW * 10)
          [3]  Reactive Power (kVAR * 10)
          [4]  Apparent Power (kVA * 10)
          [5]  Power Factor  (pf * 100)
          [6]  Frequency     (Hz * 10)
          [7]  Energy        (kWh * 100)
          [8]  Motor status  (1=on, 0=off or pending)
          [9]  Alarm flag    (1=active)
          [10] Alarm code    (raw trip_word)
          [11] Running time  (minutes, integer)
          [12] Load          (%, 0-100)
          [13] Trip word     (bitmask of active trips)
          [14] Alarm word    (bitmask of alarms + bit15=lockout)
          [15] Thermal cap.  (theta * 1000, per mille)
          [16] Trip count    (cumulative)
          [17] Heartbeat     (0-65535 counter)
        """
        # Expose lockout status via bit 15 of alarm_word
        alarm_w = self.protection.alarm_word
        if self.protection.latched:
            alarm_w |= 1 << 15

        return [
            int(self.data.voltage * 10),
            int(self.data.current * 10),
            int(self.data.active_power * 10),
            int(self.data.reactive_power * 10),
            int(self.data.apparent_power * 10),
            int(self.data.power_factor * 100),
            int(self.data.frequency * 10),
            int(self.data.energy * 100),
            1 if self.data.motor_status else 0,
            1 if self.data.alarm else 0,
            self.data.alarm_code,
            int(self.data.running_time),
            int(self.data.load),
            self.protection.trip_word,
            alarm_w,
            int(self.protection.theta * 1000),
            self.protection.trip_count,
            self.heartbeat,
        ]


# ==========================================
# Factory PLC (Top-Level Simulator)
# ==========================================

class FactoryPLC:
    """
    Factory PLC simulator managing 6 equipment.
    
    Uses HR[120] for system status word:
      bit 0: global E-STOP latched
      bit 1: any equipment has protection latched
    """

    REG_PER_EQ = 18
    # === HR[120] reserved for system status word ===
    SYS_STATUS_ADDR = 120

    def __init__(self):
        self.equipment: Dict[str, Equipment] = {
            cfg.equipment_id: Equipment(cfg) for cfg in EQUIPMENT_LIST
        }
        # Global E-STOP latch (persists across individual equipment RESETs)
        self.estop_latched = False

        # Start all motors at 75% load on initialization
        for eq in self.equipment.values():
            eq.start_motor()
            eq.set_load(75)

    def update_all(self):
        """
        Update all equipment states for one simulation step.
        
        E-STOP latch overrides all motors and cancels pending starts.
        """
        # E-STOP latch overrides all motors
        if self.estop_latched:
            for eq in self.equipment.values():
                if eq.motor_on:
                    eq.motor_on = False
                    eq.start_pending_until = 0.0  # Cancel pending starts too
                    print(
                        f"[E-STOP] ⛔ {eq.config.equipment_id} "
                        f"held stopped by E-STOP latch"
                    )

        for eq in self.equipment.values():
            eq.update()

    def get_all_registers(self) -> List[int]:
        """
        Return all holding registers for all equipment in order.
        
        Each equipment contributes 18 registers (6 × 18 = 108 total).
        HR[120] is reserved for system status word (handled externally).
        """
        all_regs = []
        for eq_id in ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]:
            all_regs.extend(self.equipment[eq_id].get_registers())
        return all_regs

    def get_system_status_word(self) -> int:
        """
        Build system status word for HR[120]:
          bit 0: E-STOP latched
          bit 1: any equipment has protection.latched
        """
        word = 0
        if self.estop_latched:
            word |= SYS_STATUS_ESTOP
        if any(eq.protection.latched for eq in self.equipment.values()):
            word |= SYS_STATUS_ANY_TRIP
        return word

    def emergency_stop_all(self):
        """E-STOP: latch globally + stop all motors immediately."""
        self.estop_latched = True
        for eq in self.equipment.values():
            eq.emergency_stop()
        print("[E-STOP] 🚨 ALL EQUIPMENT STOPPED + LATCHED")

    def clear_estop_latch(self):
        """Clear E-STOP latch (RESET button action)."""
        if self.estop_latched:
            self.estop_latched = False
            print("[E-STOP] 🔓 E-STOP latch cleared")
            return True
        return False

    def get_equipment_by_index(self, index: int) -> Equipment:
        """Get equipment by its index (0-5). Returns None if out of range."""
        eq_ids = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
        if 0 <= index < len(eq_ids):
            return self.equipment[eq_ids[index]]
        return None


# ==========================================
# Equipment Configuration List
# ==========================================

EQUIPMENT_LIST: List[EquipmentConfig] = [
    EquipmentConfig(
        "STP-01",
        "Stamping Press Line 1",
        "Stamping",
        450.0,
        680.0,
        0.88,
        0.88,
        "press",
    ),
    EquipmentConfig(
        "WLD-01",
        "Body Welding Robots",
        "Body Shop",
        260.0,
        395.0,
        0.85,
        0.85,
        "welding",
    ),
    EquipmentConfig(
        "PNT-01",
        "Paint Booth HVAC",
        "Paint Shop",
        350.0,
        530.0,
        0.92,
        0.92,
        "paint",
    ),
    EquipmentConfig(
        "ASM-01",
        "Assembly Conveyor",
        "General Assembly",
        120.0,
        180.0,
        0.90,
        0.90,
        "assembly",
    ),
    EquipmentConfig(
        "UTI-01",
        "Compressed Air System",
        "Utilities",
        200.0,
        305.0,
        0.85,
        0.85,
        "compressor",
    ),
    EquipmentConfig(
        "UTI-02",
        "Chiller Plant",
        "Utilities",
        300.0,
        455.0,
        0.88,
        0.88,
        "chiller",
    ),
]