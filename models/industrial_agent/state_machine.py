"""
Formal system state machine.
"""

from __future__ import annotations

from typing import List

from .models import Finding, Severity, SystemState


class StateMachine:
    def update(
        self,
        findings: List[Finding],
        estop_active: bool,
        any_lockout: bool,
    ) -> SystemState:
        if estop_active:
            return SystemState.ESTOP

        if any_lockout:
            return SystemState.LOCKED_OUT

        severities = {f.severity for f in findings}

        if Severity.CRITICAL in severities:
            return SystemState.CRITICAL
        if Severity.HIGH in severities:
            return SystemState.CRITICAL
        if Severity.MEDIUM in severities:
            return SystemState.WARNING
        if Severity.LOW in severities:
            return SystemState.DEGRADED

        return SystemState.NORMAL
