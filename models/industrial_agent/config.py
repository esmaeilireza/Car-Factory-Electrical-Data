"""
Central configuration for the industrial agent.

All thresholds are kept here so the agent can be tuned per factory,
per area, and per equipment without modifying rule logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class EquipmentProfile:
    eq_id: str
    name: str
    area: str
    rated_kw: float
    rated_current_a: float
    rated_pf: float
    temp_alarm_c: float = 80.0
    temp_trip_c: float = 95.0


DEFAULT_PROFILES: Dict[str, EquipmentProfile] = {
    "STP-01": EquipmentProfile(
        eq_id="STP-01",
        name="Stamping Press Line 1",
        area="Stamping",
        rated_kw=450.0,
        rated_current_a=680.0,
        rated_pf=0.88,
    ),
    "WLD-01": EquipmentProfile(
        eq_id="WLD-01",
        name="Body Welding Robots",
        area="Body Shop",
        rated_kw=260.0,
        rated_current_a=395.0,
        rated_pf=0.85,
    ),
    "PNT-01": EquipmentProfile(
        eq_id="PNT-01",
        name="Paint Booth HVAC",
        area="Paint Shop",
        rated_kw=350.0,
        rated_current_a=530.0,
        rated_pf=0.92,
    ),
    "ASM-01": EquipmentProfile(
        eq_id="ASM-01",
        name="Assembly Conveyor",
        area="General Assembly",
        rated_kw=120.0,
        rated_current_a=180.0,
        rated_pf=0.90,
    ),
    "UTI-01": EquipmentProfile(
        eq_id="UTI-01",
        name="Compressed Air System",
        area="Utilities",
        rated_kw=200.0,
        rated_current_a=305.0,
        rated_pf=0.85,
    ),
    "UTI-02": EquipmentProfile(
        eq_id="UTI-02",
        name="Chiller Plant",
        area="Utilities",
        rated_kw=300.0,
        rated_current_a=455.0,
        rated_pf=0.88,
    ),
}


@dataclass
class AgentConfig:
    # Electrical nominal values
    nominal_voltage: float = 400.0
    nominal_frequency: float = 50.0

    # Voltage limits
    min_voltage_ratio: float = 0.85
    max_voltage_ratio: float = 1.10

    # Frequency limits
    max_freq_dev_hz: float = 0.50

    # Current limits
    warning_current_ratio: float = 1.15
    max_current_ratio: float = 1.50
    loss_of_load_ratio: float = 0.30

    # Power factor
    min_pf: float = 0.70
    target_pf: float = 0.90

    # Thermal capacity
    theta_warning_pct: float = 85.0
    theta_trip_pct: float = 100.0

    # Communication / data quality
    heartbeat_stale_sec: float = 5.0
    data_stale_sec: float = 10.0

    # Memory
    working_memory_len: int = 300
    anomaly_log_len: int = 1000

    # LLM throttling
    llm_interval_sec: float = 30.0
    llm_min_interval_sec: float = 5.0
    llm_max_tokens: int = 256
    llm_temperature: float = 0.05

    # Action policy
    max_auto_actions_per_min: int = 6
    action_cooldown_sec: float = 15.0

    # Maintenance
    max_continuous_run_min: float = 480.0

    # Trend thresholds
    temp_rise_warn_c_per_min: float = 1.5
    current_rise_warn_a_per_min: float = 50.0
    pf_degrade_warn_per_min: float = -0.02
    voltage_unstable_std: float = 8.0
    theta_rise_warn_pct_per_min: float = 5.0
    zscore_warn: float = 4.0

    # Equipment profiles
    profiles: Dict[str, EquipmentProfile] = field(
        default_factory=lambda: dict(DEFAULT_PROFILES)
    )

    def profile(self, eq_id: str) -> Optional[EquipmentProfile]:
        return self.profiles.get(eq_id)

    def rated_current(self, eq_id: str) -> float:
        p = self.profile(eq_id)
        return p.rated_current_a if p else 0.0

    def temp_alarm(self, eq_id: str) -> float:
        p = self.profile(eq_id)
        return p.temp_alarm_c if p else 80.0

    def temp_trip(self, eq_id: str) -> float:
        p = self.profile(eq_id)
        return p.temp_trip_c if p else 95.0
