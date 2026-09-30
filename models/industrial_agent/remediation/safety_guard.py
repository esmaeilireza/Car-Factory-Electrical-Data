from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .policy import RemediationPolicy


@dataclass
class EquipmentState:
    eq_id: str
    motor_state: Optional[int] = None
    alarm_flag: Optional[int] = None
    trip_word: Optional[int] = None
    lockout_status: Optional[int] = None
    thermal_per_mille: Optional[int] = None
    load_setpoint: Optional[int] = None
    trip_count: Optional[int] = None
    global_estop_latched: bool = False
    any_equipment_tripped: bool = False


@dataclass
class GuardDecision:
    approved: bool
    reason: str
    action_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class SafetyGuard:
    """
    Deterministic safety gate.

    The LLM may recommend actions.
    This guard decides whether the action is mechanically and procedurally safe.

    Important rule:
    The guard must not silently rewrite the caller's requested action.
    If a requested load reduction would breach the policy floor, it must reject
    and force the engine to propose a smaller step or escalate to the operator.
    """

    HARD_FORBIDDEN = {
        "RESET_GLOBAL_ESTOP",
        "CLEAR_GLOBAL_ESTOP_LATCH",
        "INCREASE_LOAD",
        "BYPASS_ANSI_TRIP",
        "WRITE_SAFETY_COILS",
        "START_LOCKED_EQUIPMENT",
        "DISABLE_PROTECTION_RELAYS",
    }

    LOCK_SAFE_ACTIONS = {
        "REQUEST_OPERATOR_ACK",
        "HOLD_STATE",
    }

    def __init__(self, policy: RemediationPolicy):
        self.policy = policy
        self._last_action_time: Dict[str, float] = {}
        self._action_counts: Dict[str, int] = {}

    def reset_state(self) -> None:
        self._last_action_time.clear()
        self._action_counts.clear()

    def validate(
        self,
        action_type: str,
        state: EquipmentState,
        params: Optional[Dict[str, Any]] = None,
    ) -> GuardDecision:
        params = params or {}
        metadata: Dict[str, Any] = {
            "action_type": action_type,
            "eq_id": state.eq_id,
            "params": params,
            "policy_version": self.policy.version,
            "mode": self.policy.mode,
        }

        if action_type in self.HARD_FORBIDDEN:
            return GuardDecision(
                approved=False,
                reason=f"Action {action_type} is hard-forbidden by safety policy.",
                action_type=action_type,
                metadata=metadata,
            )

        if not self.policy.is_action_allowed(action_type):
            return GuardDecision(
                approved=False,
                reason=f"Action {action_type} is not enabled in remediation policy.",
                action_type=action_type,
                metadata=metadata,
            )

        if self.policy.mode == "advisory":
            return GuardDecision(
                approved=False,
                reason="Policy mode is advisory. Human operator approval required.",
                action_type=action_type,
                metadata=metadata,
            )

        if state.global_estop_latched:
            if action_type not in self.LOCK_SAFE_ACTIONS:
                return GuardDecision(
                    approved=False,
                    reason=(
                        "Global E-STOP is latched. "
                        "Only operator acknowledgment or hold-state is allowed."
                    ),
                    action_type=action_type,
                    metadata=metadata,
                )

        if state.lockout_status == 1:
            if action_type not in self.LOCK_SAFE_ACTIONS:
                return GuardDecision(
                    approved=False,
                    reason=(
                        "Equipment is locked out. "
                        "Automatic recovery is not allowed without operator reset."
                    ),
                    action_type=action_type,
                    metadata=metadata,
                )

        cooldown = float(self.policy.global_limits.get("cooldown_seconds", 30))
        key = f"{state.eq_id}:{action_type}"
        now = time.time()
        last = self._last_action_time.get(key, 0.0)

        if cooldown > 0 and now - last < cooldown:
            return GuardDecision(
                approved=False,
                reason=f"Cooldown active for {action_type} on {state.eq_id}.",
                action_type=action_type,
                metadata={
                    **metadata,
                    "cooldown_remaining_s": round(cooldown - (now - last), 1),
                },
            )

        max_same = int(self.policy.global_limits.get("max_consecutive_same_action", 3))
        if self._action_counts.get(key, 0) >= max_same:
            return GuardDecision(
                approved=False,
                reason=(
                    f"Maximum consecutive {action_type} actions reached. "
                    "Escalate to operator."
                ),
                action_type=action_type,
                metadata=metadata,
            )

        if action_type == "REDUCE_LOAD":
            cfg = self.policy.action_config("REDUCE_LOAD")
            current_load = state.load_setpoint

            if current_load is None:
                return GuardDecision(
                    approved=False,
                    reason="Current load setpoint unavailable.",
                    action_type=action_type,
                    metadata=metadata,
                )

            min_load = int(cfg.get("min_load_percent", 20))
            max_step = int(cfg.get("max_step_percent", 10))
            requested_step = int(params.get("step_percent", max_step))

            if requested_step <= 0:
                return GuardDecision(
                    approved=False,
                    reason="Load reduction step must be positive.",
                    action_type=action_type,
                    metadata=metadata,
                )

            if requested_step > max_step:
                return GuardDecision(
                    approved=False,
                    reason=(
                        f"Requested load reduction {requested_step}% exceeds "
                        f"policy max {max_step}%."
                    ),
                    action_type=action_type,
                    metadata=metadata,
                )

            target_load = current_load - requested_step

            if target_load < min_load:
                return GuardDecision(
                    approved=False,
                    reason=(
                        f"Requested {requested_step}% reduction would drive load "
                        f"({current_load}%) below the policy minimum ({min_load}%). "
                        "Use a smaller step or escalate to the operator."
                    ),
                    action_type=action_type,
                    metadata={
                        **metadata,
                        "current_load": current_load,
                        "requested_step": requested_step,
                        "target_load": target_load,
                        "min_load": min_load,
                    },
                )

            if current_load <= min_load:
                return GuardDecision(
                    approved=False,
                    reason="Load already at or below the policy minimum.",
                    action_type=action_type,
                    metadata={
                        **metadata,
                        "current_load": current_load,
                        "min_load": min_load,
                    },
                )

            metadata.update(
                {
                    "current_load": current_load,
                    "new_load": target_load,
                    "min_load": min_load,
                    "requested_step": requested_step,
                }
            )

        if action_type in {"ISOLATE_NON_CRITICAL", "SAFE_STOP_NON_CRITICAL"}:
            cfg = self.policy.action_config(action_type)
            allowed_eq = set(cfg.get("equipment_classes", []))

            if state.eq_id not in allowed_eq:
                return GuardDecision(
                    approved=False,
                    reason=(
                        f"{state.eq_id} is not classified as non-critical "
                        f"for automatic {action_type}."
                    ),
                    action_type=action_type,
                    metadata=metadata,
                )

        return GuardDecision(
            approved=True,
            reason="Action approved by deterministic safety guard.",
            action_type=action_type,
            metadata=metadata,
        )

    def record_executed(self, eq_id: str, action_type: str) -> None:
        key = f"{eq_id}:{action_type}"
        self._last_action_time[key] = time.time()
        self._action_counts[key] = self._action_counts.get(key, 0) + 1

    def record_failed(self, eq_id: str, action_type: str) -> None:
        key = f"{eq_id}:{action_type}"
        self._action_counts[key] = self._action_counts.get(key, 0) + 1
