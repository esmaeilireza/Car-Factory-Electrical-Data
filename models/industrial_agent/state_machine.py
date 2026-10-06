"""
Deterministic system-state machine for NEXUS SCADA.

This module converts rule/trend findings plus global safety flags into a
single coarse system state:

    NORMAL
    WARNING
    CRITICAL

Design rules:
- Deterministic: no LLM, no probabilistic reasoning.
- Safety-first: E-STOP or lockout immediately forces CRITICAL.
- Backward-compatible constructor: accepts optional config.
- Config-aware but not config-dependent: if config is None, safe defaults apply.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, Optional

from .models import SystemState


def _state(name: str, default: SystemState) -> SystemState:
    """
    Resolve a SystemState enum member safely.

    If the project enum does not define a particular state, fall back to a
    safe default instead of raising AttributeError.
    """
    return getattr(SystemState, name, default)


class StateMachine:
    """
    Deterministic system-state machine.

    The machine is intentionally simple and auditable:

        estop_active          -> CRITICAL
        any_lockout           -> CRITICAL
        any CRITICAL finding  -> CRITICAL
        any HIGH finding      -> CRITICAL
        any MEDIUM finding    -> WARNING
        otherwise             -> NORMAL

    It stores transition history for dashboard/debug use, but the history is
    bounded and never affects control decisions.
    """

    def __init__(self, config: Optional[Any] = None):
        """
        Backward-compatible constructor.

        Previously this class may have had no __init__, which caused:

            TypeError: StateMachine() takes no arguments

        when agent.py called:

            StateMachine(config)

        Now both styles work:

            StateMachine()
            StateMachine(config)
        """
        self.config = config

        self.normal_state = _state("NORMAL", SystemState.NORMAL)
        self.warning_state = _state("WARNING", self.normal_state)
        self.critical_state = _state("CRITICAL", self.warning_state)

        self.state: SystemState = self.normal_state
        self.last_transition_time: float = time.time()
        self.transition_count: int = 0

        # Bounded transition history for observability only.
        self.history: Deque[Dict[str, Any]] = deque(maxlen=200)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        findings: Iterable[Any],
        estop_active: bool = False,
        any_lockout: bool = False,
    ) -> SystemState:
        """
        Evaluate current findings and return the new system state.

        Args:
            findings: Iterable of Finding objects or finding-like dicts.
            estop_active: True if global E-STOP is latched/active.
            any_lockout: True if any equipment is in lockout/trip-locked state.

        Returns:
            SystemState enum value.
        """
        candidate = self._evaluate(findings, estop_active, any_lockout)

        if candidate != self.state:
            self._record_transition(candidate)
            self.state = candidate

        return self.state

    def reset(self) -> SystemState:
        """
        Reset machine to NORMAL.

        This does not clear equipment trips or E-STOP state in the PLC.
        It only resets the in-memory supervisor state.
        """
        old = self.state
        self.state = self.normal_state
        self.last_transition_time = time.time()

        if old != self.state:
            self.transition_count += 1
            self.history.append(
                {
                    "ts": self.last_transition_time,
                    "from": self._state_name(old),
                    "to": self._state_name(self.state),
                    "reason": "manual_reset",
                }
            )

        return self.state

    def snapshot(self) -> Dict[str, Any]:
        """
        Return a small dashboard-friendly snapshot of machine state.
        """
        return {
            "state": self._state_name(self.state),
            "transition_count": self.transition_count,
            "last_transition_time": self.last_transition_time,
            "history_len": len(self.history),
        }

    # ------------------------------------------------------------------
    # Internal evaluation
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        findings: Iterable[Any],
        estop_active: bool,
        any_lockout: bool,
    ) -> SystemState:
        """
        Deterministic state selection.
        """
        # Absolute safety precedence.
        if estop_active:
            return self.critical_state

        if any_lockout:
            return self.critical_state

        has_critical = False
        has_high = False
        has_medium = False

        for f in findings or []:
            token = self._severity_token(f)

            if token in {"CRITICAL"}:
                has_critical = True
                break

            if token in {"HIGH", "SEVERE", "EMERGENCY"}:
                has_high = True
                continue

            if token in {"MEDIUM", "WARNING", "WARN", "ALARM"}:
                has_medium = True
                continue

        if has_critical or has_high:
            return self.critical_state

        if has_medium:
            return self.warning_state

        return self.normal_state

    def _severity_token(self, finding: Any) -> str:
        """
        Extract a normalized severity string from Finding objects or dicts.
        """
        if isinstance(finding, dict):
            sev = finding.get("severity")
        else:
            sev = getattr(finding, "severity", None)

        if sev is None:
            return ""

        # Enum-like: prefer .value, then .name.
        value = getattr(sev, "value", None)
        if value is not None:
            return str(value).upper()

        name = getattr(sev, "name", None)
        if name is not None:
            return str(name).upper()

        return str(sev).upper()

    def _state_name(self, state: Any) -> str:
        """
        Safe state-to-string conversion for logs/history.
        """
        value = getattr(state, "value", None)
        if value is not None:
            return str(value)

        name = getattr(state, "name", None)
        if name is not None:
            return str(name)

        return str(state)

    def _record_transition(self, new_state: SystemState) -> None:
        """
        Append transition to bounded history.
        """
        self.transition_count += 1
        self.last_transition_time = time.time()

        self.history.append(
            {
                "ts": self.last_transition_time,
                "from": self._state_name(self.state),
                "to": self._state_name(new_state),
                "reason": "finding_evaluation",
            }
        )