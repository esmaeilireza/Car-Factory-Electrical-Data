"""
Statistical trend engine.

Threshold rules detect events.
Trend rules detect incoming failure.
"""

from __future__ import annotations

from typing import Dict, List

from .config import AgentConfig
from .memory import MetricWindow
from .models import EquipmentSnapshot, Finding, Severity


class TrendEngine:
    def __init__(self, config: AgentConfig):
        self.config = config

    def evaluate(
        self,
        snap: EquipmentSnapshot,
        windows: Dict[str, MetricWindow],
    ) -> List[Finding]:
        findings: List[Finding] = []
        cfg = self.config

        # Rapid temperature rise
        temp_win = windows.get("temperature")
        if temp_win and temp_win.count >= 5:
            slope = temp_win.slope_per_min
            if slope is not None and slope > cfg.temp_rise_warn_c_per_min:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="RAPID_TEMP_RISE",
                        severity=Severity.MEDIUM,
                        message=f"{snap.eq_id} temperature rising fast: {slope:.2f} C/min.",
                        source="TREND_ENGINE",
                        evidence={"slope_c_per_min": round(slope, 3)},
                        suggested_action="reduce_load",
                        safety_level=2,
                        auto_allowed=True,
                    )
                )

            z = temp_win.zscore()
            if z is not None and abs(z) > cfg.zscore_warn:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="TEMP_STATISTICAL_OUTLIER",
                        severity=Severity.LOW,
                        message=f"{snap.eq_id} temperature statistical outlier: z={z:.2f}.",
                        source="TREND_ENGINE",
                        evidence={"zscore": round(z, 3)},
                        suggested_action="inspect_process",
                        safety_level=1,
                        auto_allowed=False,
                    )
                )

        # Rapid current rise
        current_win = windows.get("current")
        if current_win and current_win.count >= 5:
            slope = current_win.slope_per_min
            if slope is not None and slope > cfg.current_rise_warn_a_per_min:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="RAPID_CURRENT_RISE",
                        severity=Severity.MEDIUM,
                        message=f"{snap.eq_id} current rising fast: {slope:.1f} A/min.",
                        source="TREND_ENGINE",
                        evidence={"slope_a_per_min": round(slope, 2)},
                        suggested_action="reduce_load",
                        safety_level=2,
                        auto_allowed=True,
                    )
                )

        # Power factor degradation
        pf_win = windows.get("power_factor")
        if pf_win and pf_win.count >= 10 and snap.motor_state == 1:
            slope = pf_win.slope_per_min
            if slope is not None and slope < cfg.pf_degrade_warn_per_min:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="PF_DEGRADING",
                        severity=Severity.LOW,
                        message=f"{snap.eq_id} power factor degrading: {slope:.4f}/min.",
                        source="TREND_ENGINE",
                        evidence={"slope_pf_per_min": round(slope, 5)},
                        suggested_action="check_capacitor_bank",
                        safety_level=1,
                        auto_allowed=False,
                    )
                )

        # Voltage instability
        voltage_win = windows.get("voltage")
        if voltage_win and voltage_win.count >= 10:
            std = voltage_win.std
            if std is not None and std > cfg.voltage_unstable_std:
                findings.append(
                    Finding(
                        eq_id=snap.eq_id,
                        code="VOLTAGE_UNSTABLE",
                        severity=Severity.MEDIUM,
                        message=f"{snap.eq_id} voltage unstable: std={std:.2f} V.",
                        source="TREND_ENGINE",
                        evidence={"voltage_std": round(std, 3)},
                        suggested_action="check_supply",
                        safety_level=2,
                        auto_allowed=False,
                    )
                )

        # Thermal capacity rising quickly
        theta_win = windows.get("theta")
        if theta_win and theta_win.count >= 5:
            slope_pm_per_min = theta_win.slope_per_min
            if slope_pm_per_min is not None:
                slope_pct_per_min = slope_pm_per_min / 10.0
                if slope_pct_per_min > cfg.theta_rise_warn_pct_per_min:
                    findings.append(
                        Finding(
                            eq_id=snap.eq_id,
                            code="THERMAL_CAPACITY_RISING",
                            severity=Severity.MEDIUM,
                            message=(
                                f"{snap.eq_id} thermal capacity rising fast: "
                                f"{slope_pct_per_min:.2f}%/min."
                            ),
                            source="TREND_ENGINE",
                            evidence={"theta_slope_pct_per_min": round(slope_pct_per_min, 3)},
                            suggested_action="reduce_load",
                            safety_level=2,
                            auto_allowed=True,
                        )
                    )

        return findings
