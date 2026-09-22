"""
Deterministic rule engine.

This layer is fast, explainable, and safety-critical.
It must not depend on the LLM.
"""

from __future__ import annotations

import time
from typing import Dict, List

from .config import AgentConfig
from .memory import MetricWindow
from .models import ANSI_NAMES, EquipmentSnapshot, Finding, Severity


class RuleEngine:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.last_heartbeat: Dict[str, int] = {}
        self.last_heartbeat_time: Dict[str, float] = {}

    def evaluate(
        self,
        snap: EquipmentSnapshot,
        windows: Dict[str, MetricWindow],
        estop_active: bool,
        now: float | None = None,
    ) -> List[Finding]:
        now = now if now is not None else time.time()
        findings: List[Finding] = []
        cfg = self.config

        profile = cfg.profile(snap.eq_id)
        if profile is None:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="UNKNOWN_EQUIPMENT",
                    severity=Severity.LOW,
                    message=f"{snap.eq_id} has no configured equipment profile.",
                    source="RULE_ENGINE",
                    evidence={},
                    suggested_action="inspect_configuration",
                    safety_level=1,
                    auto_allowed=False,
                )
            )
            return findings

        # ------------------------------------------------------------------
        # 1. Data staleness
        # ------------------------------------------------------------------
        if now - snap.timestamp > cfg.data_stale_sec:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="DATA_STALE",
                    severity=Severity.HIGH,
                    message=(
                        f"{snap.eq_id} telemetry is stale "
                        f"({now - snap.timestamp:.1f}s old)."
                    ),
                    source="RULE_ENGINE",
                    evidence={"age_sec": round(now - snap.timestamp, 2)},
                    suggested_action="reconnect_modbus",
                    safety_level=3,
                    auto_allowed=True,
                )
            )
            return findings

        # ------------------------------------------------------------------
        # 2. Heartbeat watchdog
        # ------------------------------------------------------------------
        prev_hb = self.last_heartbeat.get(snap.eq_id)
        prev_hb_ts = self.last_heartbeat_time.get(snap.eq_id, now)

        if prev_hb is None:
            self.last_heartbeat[snap.eq_id] = snap.heartbeat
            self.last_heartbeat_time[snap.eq_id] = now
        elif snap.heartbeat != prev_hb:
            self.last_heartbeat[snap.eq_id] = snap.heartbeat
            self.last_heartbeat_time[snap.eq_id] = now
        elif now - prev_hb_ts > cfg.heartbeat_stale_sec:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="COMM_HEARTBEAT_STALE",
                    severity=Severity.HIGH,
                    message=(
                        f"{snap.eq_id} heartbeat unchanged for "
                        f"{now - prev_hb_ts:.1f}s. Telemetry may be stale."
                    ),
                    source="RULE_ENGINE",
                    evidence={
                        "heartbeat": snap.heartbeat,
                        "stale_seconds": round(now - prev_hb_ts, 2),
                    },
                    suggested_action="reconnect_modbus",
                    safety_level=3,
                    auto_allowed=True,
                )
            )
            return findings

        # ------------------------------------------------------------------
        # 3. Sensor plausibility
        # ------------------------------------------------------------------
        data_issues = []

        if snap.voltage < 0.0 or snap.voltage > 500.0:
            data_issues.append("VOLTAGE_OUT_OF_RANGE")
        if snap.current < 0.0 or snap.current > 5000.0:
            data_issues.append("CURRENT_OUT_OF_RANGE")
        if snap.temperature < -20.0 or snap.temperature > 150.0:
            data_issues.append("TEMPERATURE_OUT_OF_RANGE")
        if snap.power_factor < 0.0 or snap.power_factor > 1.0:
            data_issues.append("POWER_FACTOR_OUT_OF_RANGE")
        if snap.frequency < 45.0 or snap.frequency > 55.0:
            data_issues.append("FREQUENCY_OUT_OF_RANGE")
        if snap.theta_per_mille < 0.0 or snap.theta_per_mille > 2000.0:
            data_issues.append("THETA_OUT_OF_RANGE")

        if data_issues:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="DATA_QUALITY",
                    severity=Severity.HIGH,
                    message=f"{snap.eq_id} sensor data quality issue: {', '.join(data_issues)}.",
                    source="RULE_ENGINE",
                    evidence={
                        "issues": data_issues,
                        "voltage": snap.voltage,
                        "current": snap.current,
                        "temperature": snap.temperature,
                        "power_factor": snap.power_factor,
                        "frequency": snap.frequency,
                        "theta_per_mille": snap.theta_per_mille,
                    },
                    suggested_action="inspect_sensors",
                    safety_level=3,
                    auto_allowed=False,
                )
            )
            return findings

        # ------------------------------------------------------------------
        # 4. Global E-STOP
        # ------------------------------------------------------------------
        if estop_active:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="SYSTEM_ESTOP_ACTIVE",
                    severity=Severity.CRITICAL,
                    message=f"{snap.eq_id} is affected by global E-STOP.",
                    source="RULE_ENGINE",
                    evidence={"estop_active": True},
                    suggested_action="manual_estop_reset_required",
                    safety_level=4,
                    auto_allowed=False,
                )
            )

        # ------------------------------------------------------------------
        # 5. Protection lockout / trip
        # ------------------------------------------------------------------
        is_locked = bool(snap.lockout_status) or snap.motor_state == 3

        if is_locked:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="PROTECTION_LOCKOUT",
                    severity=Severity.CRITICAL,
                    message=f"{snap.eq_id} protection lockout is active.",
                    source="RULE_ENGINE",
                    evidence={
                        "lockout_status": snap.lockout_status,
                        "motor_state": snap.motor_state,
                        "trip_word": snap.trip_word,
                    },
                    suggested_action="request_reset",
                    safety_level=4,
                    auto_allowed=False,
                )
            )
        elif snap.trip_word != 0:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="PROTECTION_TRIP",
                    severity=Severity.HIGH,
                    message=f"{snap.eq_id} ANSI trip detected: bitmask={snap.trip_word}.",
                    source="RULE_ENGINE",
                    evidence={"trip_word": snap.trip_word},
                    suggested_action="stop_and_inspect",
                    safety_level=3,
                    auto_allowed=False,
                )
            )

        # ------------------------------------------------------------------
        # 6. ANSI bit decoding
        # ------------------------------------------------------------------
        for bit, name in ANSI_NAMES.items():
            mask = 1 << bit

            if snap.trip_word & mask:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code=f"ANSI_TRIP_{bit}",
                        severity=Severity.HIGH,
                        message=f"{snap.eq_id}: {name} trip active.",
                        source="RULE_ENGINE",
                        evidence={"ansi_bit": bit, "ansi_name": name},
                        suggested_action="stop_and_inspect",
                        safety_level=3,
                        auto_allowed=False,
                    )
                )

            if snap.alarm_word & mask:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code=f"ANSI_ALARM_{bit}",
                        severity=Severity.MEDIUM,
                        message=f"{snap.eq_id}: {name} alarm active.",
                        source="RULE_ENGINE",
                        evidence={"ansi_bit": bit, "ansi_name": name},
                        suggested_action="inspect_process",
                        safety_level=2,
                        auto_allowed=False,
                    )
                )

        # ------------------------------------------------------------------
        # 7. Temperature
        # ------------------------------------------------------------------
        temp_trip = cfg.temp_trip(snap.eq_id)
        temp_alarm = cfg.temp_alarm(snap.eq_id)

        if snap.temperature >= temp_trip:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="OVERTEMP",
                    severity=Severity.CRITICAL,
                    message=f"{snap.eq_id} overtemperature: {snap.temperature:.1f} C.",
                    source="RULE_ENGINE",
                    evidence={"temperature": snap.temperature, "limit": temp_trip},
                    suggested_action="stop_equipment",
                    safety_level=3,
                    auto_allowed=True,
                )
            )
        elif snap.temperature >= temp_alarm:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="TEMP_WARNING",
                    severity=Severity.MEDIUM,
                    message=f"{snap.eq_id} temperature elevated: {snap.temperature:.1f} C.",
                    source="RULE_ENGINE",
                    evidence={"temperature": snap.temperature, "alarm_limit": temp_alarm},
                    suggested_action="reduce_load",
                    safety_level=2,
                    auto_allowed=True,
                )
            )

        # ------------------------------------------------------------------
        # 8. Thermal capacity / ANSI 49 margin
        # ------------------------------------------------------------------
        theta_pct = snap.theta_per_mille / 10.0

        if theta_pct >= cfg.theta_trip_pct:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="THERMAL_CAPACITY_CRITICAL",
                    severity=Severity.CRITICAL,
                    message=f"{snap.eq_id} thermal capacity at {theta_pct:.1f}%.",
                    source="RULE_ENGINE",
                    evidence={"theta_percent": round(theta_pct, 2)},
                    suggested_action="stop_equipment",
                    safety_level=3,
                    auto_allowed=True,
                )
            )
        elif theta_pct >= cfg.theta_warning_pct:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="THERMAL_CAPACITY_WARNING",
                    severity=Severity.MEDIUM,
                    message=f"{snap.eq_id} thermal capacity high: {theta_pct:.1f}%.",
                    source="RULE_ENGINE",
                    evidence={"theta_percent": round(theta_pct, 2)},
                    suggested_action="reduce_load",
                    safety_level=2,
                    auto_allowed=True,
                )
            )

        # ------------------------------------------------------------------
        # 9. Voltage
        # ------------------------------------------------------------------
        min_v = cfg.nominal_voltage * cfg.min_voltage_ratio
        max_v = cfg.nominal_voltage * cfg.max_voltage_ratio

        if snap.motor_state == 1:
            if snap.voltage < min_v:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="UNDERVOLTAGE",
                        severity=Severity.HIGH,
                        message=f"{snap.eq_id} undervoltage: {snap.voltage:.1f} V.",
                        source="RULE_ENGINE",
                        evidence={"voltage": snap.voltage, "limit": min_v},
                        suggested_action="stop_equipment",
                        safety_level=3,
                        auto_allowed=True,
                    )
                )
            elif snap.voltage > max_v:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="OVERVOLTAGE",
                        severity=Severity.HIGH,
                        message=f"{snap.eq_id} overvoltage: {snap.voltage:.1f} V.",
                        source="RULE_ENGINE",
                        evidence={"voltage": snap.voltage, "limit": max_v},
                        suggested_action="stop_equipment",
                        safety_level=3,
                        auto_allowed=True,
                    )
                )
        else:
            if snap.voltage < min_v or snap.voltage > max_v:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="SUPPLY_VOLTAGE_ABNORMAL",
                        severity=Severity.MEDIUM,
                        message=(
                            f"{snap.eq_id} supply voltage abnormal while not running: "
                            f"{snap.voltage:.1f} V."
                        ),
                        source="RULE_ENGINE",
                        evidence={"voltage": snap.voltage},
                        suggested_action="check_supply",
                        safety_level=2,
                        auto_allowed=False,
                    )
                )

        # ------------------------------------------------------------------
        # 10. Current / overload / loss of load
        # ------------------------------------------------------------------
        rated_i = cfg.rated_current(snap.eq_id)

        if rated_i > 0.0 and snap.motor_state == 1:
            ratio = snap.current / rated_i

            if ratio >= cfg.max_current_ratio:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="OVERCURRENT",
                        severity=Severity.CRITICAL,
                        message=(
                            f"{snap.eq_id} overcurrent: {snap.current:.1f} A "
                            f"({ratio * 100:.0f}% of rated)."
                        ),
                        source="RULE_ENGINE",
                        evidence={
                            "current": snap.current,
                            "rated_current": rated_i,
                            "ratio": round(ratio, 3),
                        },
                        suggested_action="stop_equipment",
                        safety_level=3,
                        auto_allowed=True,
                    )
                )
            elif ratio >= cfg.warning_current_ratio:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="CURRENT_WARNING",
                        severity=Severity.MEDIUM,
                        message=(
                            f"{snap.eq_id} current high: {snap.current:.1f} A "
                            f"({ratio * 100:.0f}% of rated)."
                        ),
                        source="RULE_engine",
                        evidence={
                            "current": snap.current,
                            "rated_current": rated_i,
                            "ratio": round(ratio, 3),
                        },
                        suggested_action="reduce_load",
                        safety_level=2,
                        auto_allowed=True,
                    )
                )

            if ratio < cfg.loss_of_load_ratio:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="LOSS_OF_LOAD",
                        severity=Severity.LOW,
                        message=f"{snap.eq_id} possible loss of load.",
                        source="RULE_ENGINE",
                        evidence={
                            "current": snap.current,
                            "rated_current": rated_i,
                            "ratio": round(ratio, 3),
                        },
                        suggested_action="inspect_process",
                        safety_level=1,
                        auto_allowed=False,
                    )
                )

        # ------------------------------------------------------------------
        # 11. Power factor
        # ------------------------------------------------------------------
        if (
            snap.motor_state == 1
            and rated_i > 0.0
            and snap.current > cfg.loss_of_load_ratio * rated_i
            and 0.0 < snap.power_factor < cfg.min_pf
        ):
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="LOW_POWER_FACTOR",
                    severity=Severity.MEDIUM,
                    message=f"{snap.eq_id} low power factor: {snap.power_factor:.3f}.",
                    source="RULE_ENGINE",
                    evidence={"power_factor": snap.power_factor, "limit": cfg.min_pf},
                    suggested_action="check_capacitor_bank",
                    safety_level=2,
                    auto_allowed=False,
                )
            )

        # ------------------------------------------------------------------
        # 12. Frequency
        # ------------------------------------------------------------------
        freq_dev = abs(snap.frequency - cfg.nominal_frequency)
        if freq_dev > cfg.max_freq_dev_hz:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="FREQUENCY_DEVIATION",
                    severity=Severity.HIGH,
                    message=f"{snap.eq_id} frequency deviation: {snap.frequency:.2f} Hz.",
                    source="RULE_ENGINE",
                    evidence={"frequency": snap.frequency, "deviation": round(freq_dev, 3)},
                    suggested_action="check_supply",
                    safety_level=3,
                    auto_allowed=False,
                )
            )

        # ------------------------------------------------------------------
        # 13. Continuous runtime / maintenance
        # ------------------------------------------------------------------
        if snap.motor_state == 1 and snap.running_time_min > cfg.max_continuous_run_min:
            findings.append(
                Finding(
                    eq_id=snap.eq_id,
                    code="MAINTENANCE_DUE",
                    severity=Severity.MEDIUM,
                    message=(
                        f"{snap.eq_id} continuous runtime {snap.running_time_min:.0f} min "
                        f"exceeds {cfg.max_continuous_run_min:.0f} min."
                    ),
                    source="RULE_ENGINE",
                    evidence={"running_time_min": snap.running_time_min},
                    suggested_action="inspect_process",
                    safety_level=2,
                    auto_allowed=False,
                )
            )

        return findings
