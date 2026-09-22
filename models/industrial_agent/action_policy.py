"""
Action policy and safety guardrails.

This module decides whether a recommended action may be executed.
The LLM never directly controls the PLC.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Dict, Tuple

from .config import AgentConfig
from .models import Finding, OperatingMode, Severity


SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class ActionPolicy:
    AUTO_ALLOWED_ACTIONS = {
        "stop_equipment",
        "reduce_load",
        "raise_alarm",
        "reconnect_modbus",
        "flush_session_cache",
        "restart_poller",
    }

    HUMAN_REQUIRED_ACTIONS = {
        "reset_protection",
        "request_reset",
        "clear_estop",
        "manual_estop_reset_required",
        "start_equipment",
        "stop_and_inspect",
        "inspect_process",
        "inspect_sensors",
        "check_supply",
        "check_capacitor_bank",
        "inspect_configuration",
        "manual_review",
        "nominal",
        "none",
    }

    BLOCKED_ALWAYS = {
        "bypass_interlock",
        "change_safety_setpoint",
        "modify_plc_logic",
        "ignore_trip",
        "disable_protection",
        "force_start",
    }

    MIN_SEVERITY_FOR_AUTO = {
        "stop_equipment": Severity.HIGH,
        "reduce_load": Severity.MEDIUM,
        "raise_alarm": Severity.LOW,
        "reconnect_modbus": Severity.MEDIUM,
        "flush_session_cache": Severity.LOW,
        "restart_poller": Severity.MEDIUM,
    }

    def __init__(self, config: AgentConfig):
        self.config = config
        self.recent_actions: deque = deque(maxlen=200)
        self.cooldown: Dict[str, float] = {}

    @staticmethod
    def normalize_action(action: str) -> str:
        if not action:
            return "manual_review"
        a = str(action).strip().lower()
        a = a.replace(" ", "_").replace("-", "_").replace("()", "")
        return a or "manual_review"

    def decide(
        self,
        finding: Finding,
        mode: OperatingMode,
        operator_present: bool,
        estop_active: bool,
    ) -> Dict[str, Any]:
        action = self.normalize_action(finding.suggested_action)
        now = time.time()

        base = {
            "eq_id": finding.eq_id,
            "code": finding.code,
            "action": action,
            "approved": False,
            "reason": "",
            "severity": finding.severity.value,
            "safety_level": finding.safety_level,
            "operator_present": operator_present,
            "mode": mode.value,
            "timestamp": now,
        }

        if action in self.BLOCKED_ALWAYS:
            base["reason"] = "Blocked by hard safety policy"
            return base

        if action in self.HUMAN_REQUIRED_ACTIONS:
            base["reason"] = "Human-required action"
            return base

        if estop_active and action not in {
            "raise_alarm",
            "reconnect_modbus",
            "flush_session_cache",
        }:
            base["reason"] = "E-STOP active; autonomous control blocked"
            return base

        if mode == OperatingMode.ADVISORY:
            base["reason"] = "Advisory mode: recommendation only"
            return base

        if action not in self.AUTO_ALLOWED_ACTIONS:
            base["reason"] = "Action not in auto-allowed allowlist"
            return base

        min_sev = self.MIN_SEVERITY_FOR_AUTO.get(action, Severity.HIGH)
        if SEVERITY_RANK[finding.severity] < SEVERITY_RANK[min_sev]:
            base["reason"] = "Severity below minimum for automatic action"
            return base

        if finding.safety_level > 3:
            base["reason"] = "Safety level too high for automatic action"
            return base

        if mode == OperatingMode.SUPERVISORY:
            if finding.severity not in {Severity.HIGH, Severity.CRITICAL}:
                base["reason"] = "Supervisory mode requires HIGH or CRITICAL severity"
                return base
        elif mode == OperatingMode.AUTONOMOUS:
            if not finding.auto_allowed:
                base["reason"] = "Autonomous mode: finding not marked auto-allowed"
                return base
        else:
            base["reason"] = "Unknown operating mode"
            return base

        allowed, rate_reason = self._check_rate_limit(now)
        if not allowed:
            base["reason"] = rate_reason
            return base

        allowed, cooldown_reason = self._check_cooldown(finding.eq_id, action, now)
        if not allowed:
            base["reason"] = cooldown_reason
            return base

        base["approved"] = True
        base["reason"] = f"{mode.value} mode: safe automatic action approved"
        return base

    def record_execution(self, decision: Dict[str, Any]) -> None:
        now = time.time()
        self.recent_actions.append(now)
        key = self._cooldown_key(decision["eq_id"], decision["action"])
        self.cooldown[key] = now + self.config.action_cooldown_sec

    def _check_rate_limit(self, now: float) -> Tuple[bool, str]:
        cutoff = now - 60.0
        while self.recent_actions and self.recent_actions[0] < cutoff:
            self.recent_actions.popleft()

        if len(self.recent_actions) >= self.config.max_auto_actions_per_min:
            return False, "Automatic action rate limit exceeded"
        return True, ""

    def _check_cooldown(self, eq_id: str, action: str, now: float) -> Tuple[bool, str]:
        key = self._cooldown_key(eq_id, action)
        until = self.cooldown.get(key, 0.0)
        if now < until:
            return False, f"Action cooldown active for {until - now:.1f}s"
        return True, ""

    @staticmethod
    def _cooldown_key(eq_id: str, action: str) -> str:
        return f"{eq_id}:{action}"
