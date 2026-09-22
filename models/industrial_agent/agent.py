"""
Main industrial cognitive agent.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from .action_policy import ActionPolicy
from .audit import AuditLogger
from .config import AgentConfig
from .llm_reasoner import LLMReasoner
from .memory import MetricWindow
from .models import (
    EquipmentSnapshot,
    Finding,
    OperatingMode,
    Severity,
    SystemState,
)
from .rules import RuleEngine
from .state_machine import StateMachine
from .trends import TrendEngine


class IndustrialCognitiveAgent:
    """
    Lightweight industrial supervisor agent.

    Pipeline:
        telemetry -> data quality -> rules -> trends -> state machine
                -> action policy -> optional LLM explanation -> audit/dashboard
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        ai_engine: Any = None,
        command_callback: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        audit_path: Optional[str] = None,
    ):
        self.config = config or AgentConfig()
        self.ai_engine = ai_engine
        self.command_callback = command_callback

        self.mode = OperatingMode.ADVISORY
        self.operator_present = True
        self.estop_active = False
        self.state = SystemState.NORMAL

        self.snapshots: Deque[EquipmentSnapshot] = deque(maxlen=self.config.working_memory_len)
        self.windows: Dict[str, Dict[str, MetricWindow]] = {}
        self.findings: List[Finding] = []
        self.recommendations: List[Dict[str, Any]] = []
        self.action_decisions: List[Dict[str, Any]] = []

        self.anomaly_log: Deque[Dict[str, Any]] = deque(maxlen=self.config.anomaly_log_len)
        self._logged_fingerprints: Dict[str, float] = {}
        self._latest_stats: Dict[str, Dict[str, Any]] = {}

        self.rule_engine = RuleEngine(self.config)
        self.trend_engine = TrendEngine(self.config)
        self.state_machine = StateMachine()
        self.llm_reasoner = LLMReasoner(ai_engine, self.config)
        self.action_policy = ActionPolicy(self.config)
        self.audit = AuditLogger(audit_path)

        self.last_update_time: float = 0.0
        self.last_llm_result: Optional[Dict[str, Any]] = None
        self._last_llm_call: float = 0.0

        self.audit.log("AGENT_START", {
            "mode": self.mode.value,
            "operator_present": self.operator_present,
            "llm_available": self.llm_reasoner.available(),
        })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_operator_presence(self, present: bool, mode: Optional[OperatingMode] = None) -> None:
        self.operator_present = present
        if mode is not None:
            self.mode = mode
        else:
            self.mode = OperatingMode.ADVISORY if present else OperatingMode.AUTONOMOUS

        self.audit.log("OPERATOR_PRESENCE_CHANGED", {
            "operator_present": present,
            "mode": self.mode.value,
        })

    def ingest_data(
        self,
        equipment_data: Dict[str, Dict[str, Any]],
        system_status: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        self.last_update_time = now

        if system_status:
            self.estop_active = bool(system_status.get("estop_active", self.estop_active))

        new_snapshots: List[EquipmentSnapshot] = []

        for eq_id, raw in equipment_data.items():
            snap = EquipmentSnapshot.from_dict(eq_id, raw, ts=now)
            new_snapshots.append(snap)
            self._update_windows(snap)

        self.snapshots.extend(new_snapshots)

        all_findings: List[Finding] = []
        any_lockout = False

        for snap in new_snapshots:
            if bool(snap.lockout_status) or snap.motor_state == 3:
                any_lockout = True

            eq_windows = self.windows.get(snap.eq_id, {})
            rule_findings = self.rule_engine.evaluate(
                snap=snap,
                windows=eq_windows,
                estop_active=self.estop_active,
                now=now,
            )
            trend_findings = self.trend_engine.evaluate(snap, eq_windows)
            all_findings.extend(rule_findings)
            all_findings.extend(trend_findings)

        self.findings = all_findings
        self.state = self.state_machine.update(all_findings, self.estop_active, any_lockout)

        decisions: List[Dict[str, Any]] = []

        for f in all_findings:
            decision = self.action_policy.decide(
                finding=f,
                mode=self.mode,
                operator_present=self.operator_present,
                estop_active=self.estop_active,
            )
            decisions.append(decision)

            if decision["approved"] and self.command_callback is not None:
                try:
                    executed = bool(self.command_callback(decision["action"], {
                        "eq_id": f.eq_id,
                        "code": f.code,
                        "severity": f.severity.value,
                        "evidence": f.evidence,
                        "safety_level": f.safety_level,
                    }))
                    decision["executed"] = executed
                    if executed:
                        self.action_policy.record_execution(decision)
                except Exception as e:
                    decision["executed"] = False
                    decision["execution_error"] = str(e)
            else:
                decision["executed"] = False

        self.action_decisions = decisions

        for f in all_findings:
            self._log_finding_if_new(f, now)

        if self._should_consult_llm(now, all_findings):
            llm_result = self.llm_reasoner.analyze(
                state=self.state,
                mode=self.mode,
                findings=all_findings,
                snapshots=new_snapshots,
            )
            if llm_result:
                self.last_llm_result = llm_result
                llm_finding = self._llm_to_finding(llm_result)
                if llm_finding:
                    self.findings.append(llm_finding)
                    self._log_finding_if_new(llm_finding, now)

                    decision = self.action_policy.decide(
                        finding=llm_finding,
                        mode=self.mode,
                        operator_present=self.operator_present,
                        estop_active=self.estop_active,
                    )
                    decisions.append(decision)

                    if decision["approved"] and self.command_callback is not None:
                        try:
                            executed = bool(self.command_callback(decision["action"], {
                                "eq_id": llm_finding.eq_id,
                                "code": llm_finding.code,
                                "severity": llm_finding.severity.value,
                                "evidence": llm_finding.evidence,
                                "safety_level": llm_finding.safety_level,
                                "source": "LLM",
                            }))
                            decision["executed"] = executed
                            if executed:
                                self.action_policy.record_execution(decision)
                        except Exception as e:
                            decision["executed"] = False
                            decision["execution_error"] = str(e)
                    else:
                        decision["executed"] = False

        self.recommendations = self._build_recommendations()

        self.audit.log("AGENT_UPDATE", {
            "state": self.state.value,
            "mode": self.mode.value,
            "findings_count": len(self.findings),
            "approved_actions": sum(1 for d in decisions if d.get("approved")),
            "executed_actions": sum(1 for d in decisions if d.get("executed")),
        })

    def get_dashboard_summary(self) -> Dict[str, Any]:
        return {
            "agent_status": "ACTIVE",
            "system_state": self.state.value,
            "operating_mode": self.mode.value,
            "operator_present": self.operator_present,
            "estop_active": self.estop_active,
            "llm_available": self.llm_reasoner.available(),
            "last_update": self.last_update_time,
            "active_findings_count": len(self.findings),
            "recommendations": self.recommendations,
            "action_decisions": self.action_decisions[-10:],
            "recent_anomalies": list(self.anomaly_log)[-10:],
            "working_memory_size": len(self.snapshots),
            "tracked_equipment": list(self.windows.keys()),
            "last_llm_result": self.last_llm_result,
        }

    def get_operator_message(self) -> str:
        if not self.recommendations:
            return "System nominal. No abnormality detected."

        top = self.recommendations[0]
        msg = top.get("message", "")
        action = top.get("action", "manual_review")
        return f"{msg} Recommended action: {action}."

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_windows(self, snap: EquipmentSnapshot) -> None:
        if snap.eq_id not in self.windows:
            self.windows[snap.eq_id] = {
                "voltage": MetricWindow(self.config.working_memory_len),
                "current": MetricWindow(self.config.working_memory_len),
                "power_factor": MetricWindow(self.config.working_memory_len),
                "temperature": MetricWindow(self.config.working_memory_len),
                "load": MetricWindow(self.config.working_memory_len),
                "frequency": MetricWindow(self.config.working_memory_len),
                "theta": MetricWindow(self.config.working_memory_len),
                "heartbeat": MetricWindow(60),
            }

        w = self.windows[snap.eq_id]
        w["voltage"].update(snap.timestamp, snap.voltage)
        w["current"].update(snap.timestamp, snap.current)
        w["power_factor"].update(snap.timestamp, snap.power_factor)
        w["temperature"].update(snap.timestamp, snap.temperature)
        w["load"].update(snap.timestamp, snap.load)
        w["frequency"].update(snap.timestamp, snap.frequency)
        w["theta"].update(snap.timestamp, snap.theta_per_mille)
        w["heartbeat"].update(snap.timestamp, snap.heartbeat)

    def _should_consult_llm(self, now: float, findings: List[Finding]) -> bool:
        if not self.llm_reasoner.available():
            return False

        has_serious = any(
            f.severity in {Severity.HIGH, Severity.CRITICAL}
            for f in findings
        )

        if has_serious:
            if now - self._last_llm_call >= self.config.llm_min_interval_sec:
                self._last_llm_call = now
                return True

        if now - self._last_llm_call >= self.config.llm_interval_sec:
            self._last_llm_call = now
            return True

        return False

    def _llm_to_finding(self, llm_result: Dict[str, Any]) -> Optional[Finding]:
        severity_str = str(llm_result.get("severity", "low")).lower()
        sev_map = {
            "low": Severity.LOW,
            "medium": Severity.MEDIUM,
            "high": Severity.HIGH,
            "critical": Severity.CRITICAL,
        }
        severity = sev_map.get(severity_str, Severity.LOW)

        message = str(
            llm_result.get("operator_message")
            or llm_result.get("diagnosis")
            or "LLM diagnosis"
        )

        action = str(llm_result.get("recommended_action", "manual_review"))

        safety_level = 1
        if severity in {Severity.HIGH, Severity.CRITICAL}:
            safety_level = 3
        elif severity == Severity.MEDIUM:
            safety_level = 2

        return Finding(
            eq_id="SYSTEM",
            code="LLM_DIAGNOSIS",
            severity=severity,
            message=message,
            source="LLM",
            evidence={
                "diagnosis": llm_result.get("diagnosis"),
                "recommended_action": action,
                "confidence": llm_result.get("confidence", 0.0),
            },
            suggested_action=action,
            safety_level=safety_level,
            auto_allowed=False,
        )

    def _log_finding_if_new(self, finding: Finding, now: float) -> None:
        fp = f"{finding.eq_id}:{finding.code}:{finding.severity.value}"
        last = self._logged_fingerprints.get(fp)

        if last is None or now - last > 30.0:
            self._logged_fingerprints[fp] = now
            entry = finding.to_dict()
            self.anomaly_log.append(entry)
            self.audit.log("FINDING", entry)
            print(f"[AGENT] [{finding.severity.value}] {finding.eq_id}: {finding.message}")

    def _build_recommendations(self) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []

        for f in self.findings:
            recs.append({
                "equipment": f.eq_id,
                "code": f.code,
                "severity": f.severity.value,
                "message": f.message,
                "action": f.suggested_action,
                "source": f.source,
            })

        if self.last_llm_result:
            recs.append({
                "equipment": "SYSTEM",
                "code": "LLM_ADVICE",
                "severity": self.last_llm_result.get("severity", "low"),
                "message": self.last_llm_result.get("operator_message", ""),
                "action": self.last_llm_result.get("recommended_action", "manual_review"),
                "source": "LLM",
                "confidence": self.last_llm_result.get("confidence", 0.0),
            })

        order = {
            "CRITICAL": 0,
            "HIGH": 1,
            "MEDIUM": 2,
            "LOW": 3,
            "INFO": 4,
        }
        recs.sort(key=lambda r: order.get(str(r.get("severity", "LOW")).upper(), 9))
        return recs[:10]