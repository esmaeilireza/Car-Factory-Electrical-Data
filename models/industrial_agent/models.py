"""
Core data models for the industrial agent.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class OperatingMode(Enum):
    ADVISORY = "ADVISORY"
    SUPERVISORY = "SUPERVISORY"
    AUTONOMOUS = "AUTONOMOUS"


class SystemState(Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    LOCKED_OUT = "LOCKED_OUT"
    ESTOP = "ESTOP"


class Severity(Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


ANSI_NAMES = {
    0: "49 Thermal Overload",
    1: "50 Instantaneous Overcurrent",
    2: "51 Time Overcurrent",
    3: "27 Undervoltage",
    4: "59 Overvoltage",
    5: "38 Over-Temperature",
    6: "46 Phase Imbalance",
    7: "37 Loss of Load",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


@dataclass
class EquipmentSnapshot:
    """
    Atomic perception object: one time-aligned observation for one equipment.
    """
    eq_id: str
    timestamp: float

    voltage: float = 0.0
    current: float = 0.0
    active_power: float = 0.0
    reactive_power: float = 0.0
    apparent_power: float = 0.0
    power_factor: float = 0.0
    frequency: float = 50.0
    energy_kwh: float = 0.0

    load: float = 0.0
    temperature: float = 35.0
    running_time_min: float = 0.0

    motor_state: int = 0
    trip_word: int = 0
    alarm_word: int = 0
    lockout_status: int = 0
    theta_per_mille: float = 0.0
    trip_count: int = 0
    heartbeat: int = 0

    @classmethod
    def from_dict(
        cls,
        eq_id: str,
        data: Dict[str, Any],
        ts: Optional[float] = None,
    ) -> "EquipmentSnapshot":
        ts = ts if ts is not None else time.time()

        theta_raw = data.get("theta_per_mille", data.get("theta", 0.0))
        theta_value = _safe_float(theta_raw, 0.0)

        # Accept either theta fraction (0..1) or theta_per_mille (0..1000)
        if 0.0 <= theta_value <= 2.0:
            theta_per_mille = theta_value * 1000.0
        else:
            theta_per_mille = theta_value

        motor_state = _safe_int(data.get("motor_status", data.get("motor_state", 0)), 0)
        lockout_status = _safe_int(
            data.get("lockout_status", data.get("lockout", 0)),
            1 if motor_state == 3 else 0,
        )

        return cls(
            eq_id=eq_id,
            timestamp=ts,
            voltage=_safe_float(data.get("voltage", 0.0), 0.0),
            current=_safe_float(data.get("current", 0.0), 0.0),
            active_power=_safe_float(data.get("active_power", 0.0), 0.0),
            reactive_power=_safe_float(data.get("reactive_power", 0.0), 0.0),
            apparent_power=_safe_float(data.get("apparent_power", 0.0), 0.0),
            power_factor=_safe_float(data.get("power_factor", 0.0), 0.0),
            frequency=_safe_float(data.get("frequency", 50.0), 50.0),
            energy_kwh=_safe_float(
                data.get("energy", data.get("energy_kwh", 0.0)),
                0.0,
            ),
            load=_safe_float(data.get("load", 0.0), 0.0),
            temperature=_safe_float(data.get("temperature", 35.0), 35.0),
            running_time_min=_safe_float(
                data.get("running_time", data.get("running_time_min", 0.0)),
                0.0,
            ),
            motor_state=motor_state,
            trip_word=_safe_int(data.get("trip_word", 0), 0),
            alarm_word=_safe_int(data.get("alarm_word", 0), 0),
            lockout_status=lockout_status,
            theta_per_mille=theta_per_mille,
            trip_count=_safe_int(data.get("trip_count", 0), 0),
            heartbeat=_safe_int(data.get("heartbeat", 0), 0),
        )


@dataclass
class Finding:
    """
    Structured abnormality detected by rules, trends, watchdog, or LLM.
    """
    eq_id: str
    code: str
    severity: Severity
    message: str
    source: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    suggested_action: str = "none"
    safety_level: int = 1
    auto_allowed: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d
