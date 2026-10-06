"""
Main industrial cognitive agent.

This version includes:
- backward-compatible StateMachine(config)
- episodic memory wiring to Obsidian vault
- robust alarm_events persistence into SQLite
- defensive severity normalization
- defensive equipment-id resolution
- defensive DB-signature adaptation
- trip-edge logging for fault-injection verification
- explicit LLM attribution through focus_eq_id / primary_eq_id
- evidence-consistency guard:
    LLM output is overridden when it contradicts deterministic
    HIGH/CRITICAL findings, e.g. "no active protection events"
    while PROTECTION_TRIP was just emitted.
- deterministic finding enrichment with live snapshot trip/lockout evidence
- strict remediation hook trigger:
    only active deterministic protective HIGH/CRITICAL findings trigger
    remediation; LLM_DIAGNOSIS and weak ANSI_ALARM_5 findings do not.
"""

from __future__ import annotations

import inspect
import json
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

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


_KNOWN_EQ_IDS = (
    "STP-01",
    "WLD-01",
    "PNT-01",
    "ASM-01",
    "UTI-01",
    "UTI-02",
)


class IndustrialCognitiveAgent:
    """
    Lightweight industrial supervisor agent.

    Pipeline:
        telemetry -> data quality -> rules -> trends -> state machine
                -> action policy -> optional LLM explanation -> audit/dashboard

    Attribution contract:
        The deterministic rule engine knows which equipment tripped.
        The LLM explains. The Obsidian vault records.
        The agent must never let LLM prose override deterministic attribution.
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

        # Optional backend wiring.
        self.db: Any = None
        self.remediation_engine: Optional[Any] = None

        self.mode = OperatingMode.ADVISORY
        self.operator_present = True
        self.estop_active = False
        self.state = SystemState.NORMAL

        self.snapshots: Deque[EquipmentSnapshot] = deque(
            maxlen=self.config.working_memory_len
        )
        self.windows: Dict[str, Dict[str, MetricWindow]] = {}
        self.findings: List[Finding] = []
        self.recommendations: List[Dict[str, Any]] = []
        self.action_decisions: List[Dict[str, Any]] = []

        self.anomaly_log: Deque[Dict[str, Any]] = deque(
            maxlen=self.config.anomaly_log_len
        )
        self._logged_fingerprints: Dict[str, float] = {}
        self._latest_stats: Dict[str, Dict[str, Any]] = {}

        # Trip-edge tracking for fault-injection verification and
        # focus-equipment selection.
        self._trip_edges: Dict[str, bool] = {}
        self._last_trip_word: Dict[str, int] = {}
        self._last_lockout: Dict[str, bool] = {}
        self._last_trip_change: Dict[str, float] = {}

        # Remediation edge dedupe.
        self._remediation_triggered_edges: Dict[str, bool] = {}

        # LLM urgent-edge dedupe.
        self._last_llm_edge_fp: str = ""

        self.rule_engine = RuleEngine(self.config)
        self.trend_engine = TrendEngine(self.config)

        # Supports both StateMachine() and StateMachine(config).
        self.state_machine = StateMachine(self.config)

        self.llm_reasoner = LLMReasoner(ai_engine, self.config)
        self.action_policy = ActionPolicy(self.config)
        self.audit = AuditLogger(audit_path)

        self.last_update_time: float = 0.0
        self.last_llm_result: Optional[Dict[str, Any]] = None
        self._last_llm_call: float = 0.0

        self.audit.log(
            "AGENT_START",
            {
                "mode": self.mode.value,
                "operator_present": self.operator_present,
                "llm_available": self.llm_reasoner.available(),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_operator_presence(
        self,
        present: bool,
        mode: Optional[OperatingMode] = None,
    ) -> None:
        self.operator_present = present

        if mode is not None:
            self.mode = mode
        else:
            self.mode = (
                OperatingMode.ADVISORY
                if present
                else OperatingMode.AUTONOMOUS
            )

        self.audit.log(
            "OPERATOR_PRESENCE_CHANGED",
            {
                "operator_present": present,
                "mode": self.mode.value,
            },
        )

    def ingest_data(
        self,
        equipment_data: Dict[str, Dict[str, Any]],
        system_status: Optional[Dict[str, Any]] = None,
        stats: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        now = time.time()
        self.last_update_time = now
        self._latest_stats = stats or {}

        if system_status:
            self.estop_active = bool(
                system_status.get("estop_active", self.estop_active)
            )

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

            # ------------------------------------------------------------------
            # Enrich deterministic findings with live equipment attribution and
            # trip/lockout evidence.
            #
            # This is critical for remediation hook and Obsidian promotion.
            # Without this, the hook may see trip_word=0 even though the PLC
            # snapshot has trip_word=32 and lockout=1.
            # ------------------------------------------------------------------
            self._enrich_deterministic_findings(rule_findings, snap)
            self._enrich_deterministic_findings(trend_findings, snap)

            all_findings.extend(rule_findings)
            all_findings.extend(trend_findings)

        self.findings = all_findings
        self.state = self.state_machine.update(
            all_findings,
            self.estop_active,
            any_lockout,
        )

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
                    executed = bool(
                        self.command_callback(
                            decision["action"],
                            {
                                "eq_id": f.eq_id,
                                "code": f.code,
                                "severity": f.severity.value,
                                "evidence": f.evidence,
                                "safety_level": f.safety_level,
                            },
                        )
                    )
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

        # ------------------------------------------------------------------
        # Focus-equipment selection
        # ------------------------------------------------------------------
        focus_eq_id = self._select_focus_eq_id()

        if self._should_consult_llm(now, all_findings):
            _prior = None
            try:
                import obsidian_bridge as _ob

                if hasattr(_ob, "get_recent_incidents"):
                    _prior = _ob.get_recent_incidents(limit=5)
            except Exception:
                _prior = None

            llm_result = self.llm_reasoner.analyze(
                state=self.state,
                mode=self.mode,
                findings=all_findings,
                snapshots=new_snapshots,
                prior_incidents=_prior,
                focus_eq_id=focus_eq_id,
            )

            if llm_result:
                self.last_llm_result = llm_result
                llm_finding = self._llm_to_finding(
                    llm_result,
                    focus_eq_id=focus_eq_id,
                )

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
                            executed = bool(
                                self.command_callback(
                                    decision["action"],
                                    {
                                        "eq_id": llm_finding.eq_id,
                                        "code": llm_finding.code,
                                        "severity": llm_finding.severity.value,
                                        "evidence": llm_finding.evidence,
                                        "safety_level": llm_finding.safety_level,
                                        "source": "LLM",
                                    },
                                )
                            )
                            decision["executed"] = executed
                            if executed:
                                self.action_policy.record_execution(decision)
                        except Exception as e:
                            decision["executed"] = False
                            decision["execution_error"] = str(e)
                    else:
                        decision["executed"] = False

        self.recommendations = self._build_recommendations()

        self.audit.log(
            "AGENT_UPDATE",
            {
                "state": self.state.value,
                "mode": self.mode.value,
                "findings_count": len(self.findings),
                "approved_actions": sum(
                    1 for d in decisions if d.get("approved")
                ),
                "executed_actions": sum(
                    1 for d in decisions if d.get("executed")
                ),
                "focus_eq_id": focus_eq_id,
            },
        )

    def get_dashboard_summary(self) -> Dict[str, Any]:
        summary = {
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

        try:
            if hasattr(self.state_machine, "snapshot"):
                summary["state_machine"] = self.state_machine.snapshot()
        except Exception:
            pass

        try:
            summary["focus_eq_id"] = self._select_focus_eq_id()
            summary["active_trip_words"] = dict(self._last_trip_word)
            summary["active_lockouts"] = {
                k: v for k, v in self._last_lockout.items() if v
            }
        except Exception:
            pass

        return summary

    def get_operator_message(self) -> str:
        if not self.recommendations:
            return "System nominal. No abnormality detected."

        top = self.recommendations[0]
        msg = top.get("message", "")
        action = top.get("action", "manual_review")
        return f"{msg} Recommended action: {action}."

    # ------------------------------------------------------------------
    # Deterministic finding enrichment
    # ------------------------------------------------------------------

    def _enrich_deterministic_findings(
        self,
        findings: List[Finding],
        snap: EquipmentSnapshot,
    ) -> None:
        if not findings:
            return

        try:
            snap_trip_word = int(getattr(snap, "trip_word", 0) or 0)
        except Exception:
            snap_trip_word = 0

        try:
            snap_lockout = bool(getattr(snap, "lockout_status", False))
        except Exception:
            snap_lockout = False

        snap_motor_state = 0
        try:
            snap_motor_state = int(getattr(snap, "motor_state", 0) or 0)
        except Exception:
            snap_motor_state = 0

        if snap_motor_state == 3:
            snap_lockout = True

        snap_eq_id = str(getattr(snap, "eq_id", "") or "").strip().upper()

        for f in findings:
            if not isinstance(getattr(f, "evidence", None), dict):
                try:
                    f.evidence = {}
                except Exception:
                    pass

            ev = getattr(f, "evidence", {})
            if not isinstance(ev, dict):
                ev = {}
                try:
                    f.evidence = ev
                except Exception:
                    pass

            ev.setdefault("trip_word", snap_trip_word)
            ev.setdefault("lockout_status", snap_lockout)
            ev.setdefault("motor_state", snap_motor_state)

            if snap_eq_id and snap_eq_id != "SYSTEM":
                ev.setdefault("primary_eq_id", snap_eq_id)
                ev.setdefault("target_eq_id", snap_eq_id)
                ev.setdefault("device_id", snap_eq_id)
                ev.setdefault("eq_id", snap_eq_id)

            f_eq = str(getattr(f, "eq_id", "") or "").strip().upper()
            if (not f_eq or f_eq == "SYSTEM") and snap_eq_id and snap_eq_id != "SYSTEM":
                try:
                    f.eq_id = snap_eq_id
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Focus-equipment selector
    # ------------------------------------------------------------------

    def _select_focus_eq_id(self) -> str:
        """
        Select the equipment that should be treated as the primary subject
        for the current LLM consultation.
        """
        now = time.time()
        candidates: List[Tuple[float, str]] = []

        for eq_id, trip_word in self._last_trip_word.items():
            lockout = self._last_lockout.get(eq_id, False)

            if trip_word <= 0 and not lockout:
                continue

            changed_at = self._last_trip_change.get(eq_id, 0.0)
            score = changed_at

            if trip_word > 0:
                score += 1000.0
            if lockout:
                score += 500.0

            if score > now + 60.0:
                score = now

            candidates.append((score, eq_id))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return candidates[0][1]

        severity_rank = {
            "CRITICAL": 4,
            "HIGH": 3,
            "MEDIUM": 2,
            "LOW": 1,
        }

        best_score = -1.0
        best_eq = ""

        for f in self.findings:
            eq = str(getattr(f, "eq_id", "") or "").strip().upper()
            if not eq or eq == "SYSTEM":
                continue

            sev = self._severity_text(f.severity)
            rank = severity_rank.get(sev, 0)

            ts = float(getattr(f, "timestamp", 0.0) or 0.0)
            score = rank * 1_000_000.0 + ts

            if score > best_score:
                best_score = score
                best_eq = eq

        if best_eq:
            return best_eq

        for f in self.findings:
            eq = str(getattr(f, "eq_id", "") or "").strip().upper()
            if eq and eq != "SYSTEM":
                return eq

        return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _severity_text(value: Any) -> str:
        text = str(getattr(value, "value", value) or "").strip().upper()

        if text.startswith("SEVERITY."):
            text = text.split(".", 1)[1]

        return text

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

        try:
            trip_word = int(getattr(snap, "trip_word", 0) or 0)
        except Exception:
            trip_word = 0

        try:
            lockout = bool(getattr(snap, "lockout_status", False))
        except Exception:
            lockout = False

        prev_trip = self._last_trip_word.get(snap.eq_id, 0)
        prev_lock = self._last_lockout.get(snap.eq_id, False)

        if (prev_trip == 0 and trip_word > 0) or (not prev_lock and lockout):
            self._last_trip_change[snap.eq_id] = time.time()

        self._last_trip_word[snap.eq_id] = trip_word
        self._last_lockout[snap.eq_id] = lockout

        if trip_word == 0 and not lockout:
            prefix = f"{snap.eq_id}:"

            for key in list(self._trip_edges.keys()):
                if key.startswith(prefix):
                    self._trip_edges[key] = False

            for key in list(self._remediation_triggered_edges.keys()):
                if key.startswith(prefix):
                    del self._remediation_triggered_edges[key]

    def _urgent_llm_edge_fp(self, findings: List[Finding]) -> str:
        """
        Build a fingerprint for urgent deterministic trip/lockout findings.
        """
        parts: List[str] = []

        trip_codes = {
            "PROTECTION_TRIP",
            "ANSI_TRIP",
            "TRIP",
            "OVERCURRENT",
            "OVERTEMPERATURE",
            "THERMAL_OVERLOAD",
        }

        for f in findings:
            eq = str(getattr(f, "eq_id", "") or "").strip().upper()

            if not eq or eq == "SYSTEM":
                continue

            sev = self._severity_text(getattr(f, "severity", ""))

            if sev not in {"HIGH", "CRITICAL"}:
                continue

            evidence = getattr(f, "evidence", {}) or {}
            if not isinstance(evidence, dict):
                evidence = {}

            try:
                trip_word = int(
                    evidence.get("trip_word")
                    or self._last_trip_word.get(eq, 0)
                    or 0
                )
            except Exception:
                trip_word = 0

            lockout = bool(
                evidence.get("lockout_status")
                or evidence.get("lockout")
                or self._last_lockout.get(eq, False)
            )

            code = str(getattr(f, "code", "") or "").strip().upper()

            if trip_word > 0 or lockout or code in trip_codes:
                parts.append(f"{eq}:{code}:{trip_word}:{int(lockout)}")

        return "|".join(sorted(set(parts)))

    def _should_consult_llm(self, now: float, findings: List[Finding]) -> bool:
        if not self.llm_reasoner.available():
            return False

        # Urgent trip-edge path.
        edge_fp = self._urgent_llm_edge_fp(findings)

        if edge_fp:
            if edge_fp != self._last_llm_edge_fp:
                self._last_llm_edge_fp = edge_fp

                if now - self._last_llm_call >= 3.0:
                    self._last_llm_call = now
                    return True

            if now - self._last_llm_call >= 20.0:
                self._last_llm_call = now
                return True

        has_serious = any(
            self._severity_text(f.severity) in {"HIGH", "CRITICAL"}
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

    def _llm_to_finding(
        self,
        llm_result: Dict[str, Any],
        focus_eq_id: str = "",
    ) -> Optional[Finding]:
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

        rec = llm_result.get("remediation_recommendation") or {}
        if not isinstance(rec, dict):
            rec = {}

        action = str(
            rec.get("recommended_action")
            or llm_result.get("recommended_action")
            or "manual_review"
        ).strip() or "manual_review"

        confidence = float(llm_result.get("confidence", 0.0) or 0.0)

        safety_level = 1
        if severity in {Severity.HIGH, Severity.CRITICAL}:
            safety_level = 3
        elif severity == Severity.MEDIUM:
            safety_level = 2

        primary_eq_id = str(focus_eq_id or "").strip().upper()

        if not primary_eq_id or primary_eq_id == "SYSTEM":
            lp = str(llm_result.get("primary_eq_id") or "").strip().upper()
            if lp and lp != "SYSTEM":
                primary_eq_id = lp

        if not primary_eq_id or primary_eq_id == "SYSTEM":
            for f in self.findings:
                if (
                    f.eq_id
                    and f.eq_id.upper() != "SYSTEM"
                    and self._severity_text(f.severity) in {"HIGH", "CRITICAL"}
                ):
                    primary_eq_id = f.eq_id.upper()
                    break

        if not primary_eq_id or primary_eq_id == "SYSTEM":
            for f in self.findings:
                if f.eq_id and f.eq_id.upper() != "SYSTEM":
                    primary_eq_id = f.eq_id.upper()
                    break

        if not primary_eq_id or primary_eq_id == "SYSTEM":
            for s in list(self.snapshots)[-12:]:
                try:
                    trip = int(getattr(s, "trip_word", 0) or 0)
                    lock = bool(getattr(s, "lockout_status", False))
                    if trip > 0 or lock:
                        primary_eq_id = s.eq_id.upper()
                        break
                except Exception:
                    continue

        serious_entries: List[Tuple[str, str, str]] = []

        for f in self.findings:
            sev_val = self._severity_text(getattr(f, "severity", ""))
            eq = str(getattr(f, "eq_id", "") or "").strip().upper()

            if sev_val in {"HIGH", "CRITICAL"} and eq and eq != "SYSTEM":
                msg = str(getattr(f, "message", "") or "").strip()
                serious_entries.append((eq, sev_val, msg))

        has_active_serious = bool(serious_entries)

        top_serious_eq = ""
        top_serious_message = ""

        if serious_entries:
            for eq, _sev, msg in serious_entries:
                if eq == primary_eq_id:
                    top_serious_eq = eq
                    top_serious_message = msg
                    break

            if not top_serious_eq:
                rank = {"CRITICAL": 2, "HIGH": 1}
                serious_entries.sort(
                    key=lambda x: rank.get(x[1], 0),
                    reverse=True,
                )
                top_serious_eq, _, top_serious_message = serious_entries[0]

        parsed_diag_lower = str(
            llm_result.get("diagnosis", "") or ""
        ).lower()
        parsed_msg_lower = str(
            llm_result.get("operator_message", "") or ""
        ).lower()

        inconsistent = (
            has_active_serious
            and (
                severity_str in {"low", "medium"}
                or "no active" in parsed_diag_lower
                or "no protection" in parsed_diag_lower
                or "no protection" in parsed_msg_lower
                or "normal" in parsed_diag_lower
                or "operating normally" in parsed_diag_lower
            )
        )

        if inconsistent:
            severity = Severity.HIGH
            safety_level = 3
            confidence = min(confidence, 0.25)

            if top_serious_eq:
                primary_eq_id = top_serious_eq

            if top_serious_message:
                message = (
                    f"{top_serious_eq}: {top_serious_message}"
                    if top_serious_eq
                    else top_serious_message
                )
            elif primary_eq_id and primary_eq_id != "SYSTEM":
                message = (
                    f"{primary_eq_id}: Protection trip detected. "
                    "Deterministic rule engine confirms active fault; "
                    "LLM output was inconsistent and has been overridden."
                )

            llm_result["llm_consistency_warning"] = (
                "LLM output was inconsistent with deterministic HIGH/CRITICAL "
                "findings. Operator message was reinforced from deterministic "
                "evidence."
            )

            print(
                "[AGENT] LLM consistency guard triggered: "
                f"primary={primary_eq_id}, llm_said={parsed_diag_lower[:120]!r}"
            )

        real_primary = (
            primary_eq_id
            if primary_eq_id and primary_eq_id != "SYSTEM"
            else ""
        )

        if real_primary:
            if not message.upper().startswith(real_primary.upper()):
                message = f"{real_primary}: {message}"

        llm_trip_word = 0
        llm_lockout = False

        if real_primary:
            try:
                llm_trip_word = int(self._last_trip_word.get(real_primary, 0) or 0)
            except Exception:
                llm_trip_word = 0

            llm_lockout = bool(self._last_lockout.get(real_primary, False))

        return Finding(
            eq_id=real_primary or "SYSTEM",
            code="LLM_DIAGNOSIS",
            severity=severity,
            message=message,
            source="LLM",
            evidence={
                "diagnosis": llm_result.get("diagnosis"),
                "recommended_action": action,
                "confidence": confidence,
                "remediation_recommendation": rec,
                "prior_incidents_count": llm_result.get(
                    "prior_incidents_count",
                    0,
                ),
                "prior_incidents": llm_result.get("prior_incidents", []),
                "primary_eq_id": real_primary,
                "target_eq_id": real_primary,
                "device_id": real_primary,
                "focus_eq_id": str(focus_eq_id or "").strip().upper(),
                "trip_word": llm_trip_word,
                "lockout_status": llm_lockout,
                "llm_consistency_warning": llm_result.get(
                    "llm_consistency_warning",
                    "",
                ),
            },
            suggested_action=action,
            safety_level=safety_level,
            auto_allowed=False,
        )

    # ------------------------------------------------------------------
    # Trip-edge-aware finding logger
    # ------------------------------------------------------------------

    def _log_finding_if_new(self, finding: Finding, now: float) -> None:
        evidence = getattr(finding, "evidence", {}) or {}
        if not isinstance(evidence, dict):
            evidence = {}

        trip_word = 0
        try:
            trip_word = int(
                evidence.get("trip_word")
                or self._last_trip_word.get(finding.eq_id, 0)
                or 0
            )
        except Exception:
            trip_word = 0

        actual_lockout = bool(
            evidence.get("lockout_status")
            or evidence.get("lockout")
        )

        lockout = actual_lockout or bool(
            getattr(finding, "safety_level", 0) >= 3
        )

        fp = (
            f"{finding.eq_id}:{finding.code}:{self._severity_text(finding.severity)}:"
            f"{trip_word}:{int(actual_lockout)}"
        )

        edge_key = f"{finding.eq_id}:{finding.code}"
        code_upper = str(getattr(finding, "code", "") or "").strip().upper()
        sev_val = self._severity_text(getattr(finding, "severity", ""))

        trip_codes_exact = {
            "PROTECTION_TRIP",
            "ANSI_TRIP",
            "TRIP",
            "OVERCURRENT",
            "OVERTEMPERATURE",
            "THERMAL_OVERLOAD",
            "LLM_DIAGNOSIS",
        }

        trip_prefixes = (
            "PROTECTION_TRIP",
            "ANSI_TRIP",
            "TRIP",
            "OVERCURRENT",
            "OVERVOLTAGE",
            "OVERTEMPERATURE",
            "THERMAL_OVERLOAD",
            "INSTANTANEOUS_OVERCURRENT",
            "TIME_OVERCURRENT",
        )

        is_trip_like = (
            code_upper in trip_codes_exact
            or code_upper.startswith(trip_prefixes)
            or trip_word > 0
            or actual_lockout
            or lockout
        )

        force_log = False

        if is_trip_like:
            if trip_word == 0 and not actual_lockout:
                self._trip_edges[edge_key] = False
            else:
                if not self._trip_edges.get(edge_key, False):
                    self._trip_edges[edge_key] = True
                    force_log = True

                if (
                    sev_val in {"HIGH", "CRITICAL"}
                    and (trip_word > 0 or actual_lockout)
                ):
                    last = self._logged_fingerprints.get(fp)
                    if last is None or now - last > 10.0:
                        force_log = True

        last = self._logged_fingerprints.get(fp)

        if force_log or last is None or now - last > 30.0:
            self._logged_fingerprints[fp] = now

            entry = finding.to_dict()

            if not isinstance(entry.get("evidence"), dict):
                entry["evidence"] = {}

            ev = entry["evidence"]
            ev.setdefault("trip_word", trip_word)
            ev.setdefault("lockout_status", actual_lockout)

            if finding.eq_id and finding.eq_id.upper() != "SYSTEM":
                ev.setdefault("primary_eq_id", finding.eq_id)
                ev.setdefault("target_eq_id", finding.eq_id)
                ev.setdefault("device_id", finding.eq_id)

            self.anomaly_log.append(entry)
            self.audit.log("FINDING", entry)

            # Persist HIGH/CRITICAL deterministic findings into SQLite
            # alarm_events. Skip pure LLM diagnosis to reduce duplicate rows.
            if code_upper != "LLM_DIAGNOSIS":
                self._persist_alarm_event(entry)

            # ------------------------------------------------------------------
            # NEXUS_REMEDIATION_HOOK_V2
            #
            # Trigger remediation only for active deterministic protective
            # findings. The LLM may explain, but the deterministic rule engine
            # owns remediation triggering.
            # ------------------------------------------------------------------
            is_protective_code = (
                code_upper in trip_codes_exact
                or code_upper.startswith(trip_prefixes)
            )

            is_protective_trip = (
                is_protective_code
                or trip_word > 0
                or actual_lockout
            )

            is_high_or_critical = sev_val in {"HIGH", "CRITICAL"}

            rem_edge_key = f"{finding.eq_id}:{code_upper}"

            first_remediation_edge = (
                is_trip_like
                and not self._remediation_triggered_edges.get(rem_edge_key, False)
            )

            should_trigger_remediation = (
                code_upper != "LLM_DIAGNOSIS"
                and is_protective_trip
                and is_high_or_critical
                and force_log
                and first_remediation_edge
            )

            if should_trigger_remediation:
                self._remediation_triggered_edges[rem_edge_key] = True

                try:
                    from .remediation_hook import (
                        run_remediation_after_finding as _nexus_run_rem,
                    )

                    self.audit.log(
                        "REMEDIATION_HOOK_TRIGGERED",
                        {
                            "eq_id": finding.eq_id,
                            "code": finding.code,
                            "severity": finding.severity.value,
                            "safety_level": finding.safety_level,
                            "trip_word": trip_word,
                            "lockout_status": actual_lockout,
                            "force_log": force_log,
                            "primary_eq_id": ev.get("primary_eq_id", ""),
                            "target_eq_id": ev.get("target_eq_id", ""),
                            "device_id": ev.get("device_id", ""),
                        },
                    )

                    _nexus_run_rem(self, entry)

                except Exception as _nexus_rem_err:
                    try:
                        self.audit.log(
                            "REMEDIATION_HOOK_ERROR",
                            {
                                "eq_id": finding.eq_id,
                                "code": finding.code,
                                "severity": finding.severity.value,
                                "trip_word": trip_word,
                                "lockout_status": actual_lockout,
                                "error": str(_nexus_rem_err),
                            },
                        )
                    except Exception:
                        pass

            print(
                f"[AGENT] [{finding.severity.value}] "
                f"{finding.eq_id}: {finding.message}"
            )

    # ------------------------------------------------------------------
    # Alarm-event persistence helpers
    # ------------------------------------------------------------------

    def _persist_alarm_event(self, entry: Dict[str, Any]) -> None:
        db = getattr(self, "db", None)
        if db is None:
            return

        sev = self._normalize_alarm_severity(entry.get("severity"))
        if sev not in {"HIGH", "CRITICAL"}:
            return

        eq_id = self._resolve_alarm_eq_id(entry)
        if not eq_id:
            return

        evidence = entry.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {"value": evidence}

        code = (
            entry.get("code")
            or evidence.get("ansi_code")
            or evidence.get("ansi_name")
            or "UNKNOWN"
        )
        code = self._normalize_alarm_text(code)

        ansi_code = self._normalize_alarm_text(
            evidence.get("ansi_code")
            or evidence.get("ansi_name")
            or ""
        )

        message = self._normalize_alarm_text(
            entry.get("message")
            or entry.get("diagnosis")
            or code
            or "Alarm event"
        )

        source = self._normalize_alarm_text(
            entry.get("source")
            or "agent"
        )

        try:
            evidence_json = json.dumps(evidence, sort_keys=True, default=str)
        except Exception:
            evidence_json = str(evidence)

        description = message
        if source:
            description = f"{description} | source={source}"
        if evidence_json:
            description = f"{description} | evidence={evidence_json}"

        if len(description) > 1800:
            description = description[:1797] + "..."

        fn = (
            getattr(db, "save_alarm_event", None)
            or getattr(db, "record_alarm_event", None)
            or getattr(db, "insert_alarm_event", None)
            or getattr(db, "add_alarm_event", None)
            or getattr(db, "log_alarm_event", None)
            or getattr(db, "create_alarm_event", None)
        )

        if not callable(fn):
            try:
                self.audit.log(
                    "ALARM_EVENT_PERSIST_SKIPPED",
                    {
                        "eq_id": eq_id,
                        "code": code,
                        "severity": sev,
                        "reason": "no_alarm_event_method_found",
                        "db_type": type(db).__name__,
                        "db_attrs_with_alarm": [
                            name
                            for name in dir(db)
                            if "alarm" in name.lower()
                        ],
                    },
                )
            except Exception:
                pass
            return

        errors: List[Dict[str, Any]] = []

        try:
            fn(
                equipment_id=eq_id,
                alarm_type=code,
                ansi_code=ansi_code or None,
                description=description,
                severity=sev,
            )
            return
        except TypeError as exc:
            errors.append(
                {
                    "stage": "canonical_signature",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "stage": "canonical_signature",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

        candidates = {
            "equipment_id": eq_id,
            "eq_id": eq_id,
            "device_id": eq_id,
            "machine_id": eq_id,
            "asset_id": eq_id,
            "alarm_type": code,
            "code": code,
            "alarm_code": code,
            "fault_code": code,
            "protection_code": code,
            "type": code,
            "event_type": code,
            "ansi_code": ansi_code or None,
            "ansi": ansi_code or None,
            "severity": sev,
            "level": sev,
            "alarm_severity": sev,
            "priority_level": sev,
            "description": description,
            "message": message,
            "details": message,
            "text": message,
            "summary": message,
            "diagnosis": entry.get("diagnosis"),
            "source": source,
            "origin": source,
            "producer": source,
            "detector": source,
            "evidence": evidence_json,
            "evidence_json": evidence_json,
            "payload": evidence_json,
            "data": evidence_json,
            "metadata": evidence_json,
            "timestamp": entry.get("timestamp") or time.time(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "occurred_at": datetime.now().isoformat(timespec="seconds"),
            "alarm_time": datetime.now().isoformat(timespec="seconds"),
            "detected_at": datetime.now().isoformat(timespec="seconds"),
            "status": "ACTIVE",
            "state": "ACTIVE",
            "alarm_status": "ACTIVE",
            "acknowledged": 0,
            "acknowledged_at": None,
            "acknowledged_by": None,
            "cleared": 0,
            "cleared_at": None,
        }

        try:
            self._call_db_function_with_signature(fn, candidates)
            return
        except Exception as exc:
            errors.append(
                {
                    "stage": "signature_aware",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

        try:
            fn(**candidates)
            return
        except Exception as exc:
            errors.append(
                {
                    "stage": "broad_kwargs",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

        try:
            fn(
                eq_id,
                code,
                ansi_code or None,
                description,
                sev,
            )
            return
        except Exception as exc:
            errors.append(
                {
                    "stage": "positional_common",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

        try:
            self.audit.log(
                "ALARM_EVENT_PERSIST_FAILED",
                {
                    "eq_id": eq_id,
                    "code": code,
                    "severity": sev,
                    "db_type": type(db).__name__,
                    "fn_name": getattr(fn, "__name__", str(fn)),
                    "errors": errors,
                },
            )
        except Exception:
            print(f"[AGENT] alarm event persist failed: {errors}")

    @staticmethod
    def _normalize_alarm_severity(value: Any) -> str:
        if value is None:
            return ""

        inner = getattr(value, "value", value)
        s = str(inner).strip().upper()

        if s.startswith("SEVERITY."):
            s = s.split(".", 1)[1]

        s = s.strip("'\"<>")

        return s

    @staticmethod
    def _normalize_alarm_text(value: Any) -> str:
        if value is None:
            return ""

        inner = getattr(value, "value", value)
        s = str(inner).strip()

        if s.upper().startswith("SEVERITY."):
            s = s.split(".", 1)[1]

        return s

    def _resolve_alarm_eq_id(self, entry: Dict[str, Any]) -> str:
        evidence = entry.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}

        direct = (
            evidence.get("primary_eq_id")
            or evidence.get("target_eq_id")
            or evidence.get("device_id")
            or evidence.get("eq_id")
            or entry.get("eq_id")
            or entry.get("equipment_id")
            or entry.get("device_id")
            or entry.get("machine_id")
            or entry.get("asset_id")
            or entry.get("equipment")
            or entry.get("device")
            or entry.get("machine")
        )

        if isinstance(direct, (list, tuple, set)):
            direct = next((x for x in direct if x), None)

        eq = self._normalize_alarm_text(direct)

        if eq and eq.upper() != "SYSTEM":
            return eq.upper()

        haystack = " ".join(
            [
                str(entry.get("message", "")),
                str(entry.get("diagnosis", "")),
                str(entry.get("evidence", {})),
            ]
        ).upper()

        for known in _KNOWN_EQ_IDS:
            if known in haystack:
                return known

        return ""

    @staticmethod
    def _call_db_function_with_signature(fn: Any, candidates: Dict[str, Any]) -> None:
        try:
            sig = inspect.signature(fn)
        except Exception:
            fn(**candidates)
            return

        params = list(sig.parameters.values())
        params = [p for p in params if p.name != "self"]

        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params
        )
        has_var_positional = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
        )

        if has_var_keyword:
            fn(**candidates)
            return

        kwargs: Dict[str, Any] = {}
        missing_required: List[str] = []
        positional_names: List[str] = []

        for p in params:
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                positional_names.append(p.name)

                if p.name in candidates:
                    kwargs[p.name] = candidates[p.name]
                elif p.default is inspect.Parameter.empty:
                    missing_required.append(p.name)

            elif p.kind == inspect.Parameter.KEYWORD_ONLY:
                if p.name in candidates:
                    kwargs[p.name] = candidates[p.name]
                elif p.default is inspect.Parameter.empty:
                    missing_required.append(p.name)

        if not missing_required:
            fn(**kwargs)
            return

        args: List[Any] = []
        for name in positional_names:
            if name in candidates:
                args.append(candidates[name])
            else:
                break

        if has_var_positional:
            fn(*args)
            return

        fn(*args)

    # ------------------------------------------------------------------
    # Recommendation builder
    # ------------------------------------------------------------------

    def _build_recommendations(self) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []

        for f in self.findings:
            recs.append(
                {
                    "equipment": f.eq_id,
                    "code": f.code,
                    "severity": f.severity.value,
                    "message": f.message,
                    "action": f.suggested_action,
                    "source": f.source,
                }
            )

        if self.last_llm_result:
            recs.append(
                {
                    "equipment": self.last_llm_result.get(
                        "primary_eq_id",
                        "SYSTEM",
                    ),
                    "code": "LLM_ADVICE",
                    "severity": self.last_llm_result.get("severity", "low"),
                    "message": self.last_llm_result.get(
                        "operator_message",
                        "",
                    ),
                    "action": self.last_llm_result.get(
                        "recommended_action",
                        "manual_review",
                    ),
                    "source": "LLM",
                    "confidence": self.last_llm_result.get("confidence", 0.0),
                }
            )

        order = {
            "CRITICAL": 0,
            "HIGH": 1,
            "MEDIUM": 2,
            "LOW": 3,
            "INFO": 4,
        }

        recs.sort(
            key=lambda r: order.get(str(r.get("severity", "LOW")).upper(), 9)
        )

        return recs[:10]