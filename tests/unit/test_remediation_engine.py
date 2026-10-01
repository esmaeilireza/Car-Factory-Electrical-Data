import json
from pathlib import Path

import pytest

from models.industrial_agent.remediation.policy import load_policy
from models.industrial_agent.remediation.safety_guard import (
    EquipmentState,
    SafetyGuard,
)


@pytest.fixture
def policy(tmp_path: Path):
    data = {
        "version": "test",
        "mode": "limited_autonomous",
        "supported_modes": ["advisory", "limited_autonomous"],
        "global_limits": {
            "cooldown_seconds": 0,
            "max_consecutive_same_action": 2,
        },
        "allowed_actions": {
            "REDUCE_LOAD": {
                "enabled": True,
                "max_step_percent": 10,
                "min_load_percent": 20,
            },
            "REQUEST_OPERATOR_ACK": {
                "enabled": True,
            },
            "HOLD_STATE": {
                "enabled": True,
            },
        },
        "forbidden_actions": [
            "RESET_GLOBAL_ESTOP",
            "INCREASE_LOAD",
        ],
    }

    p = tmp_path / "policy.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return load_policy(p)


def test_hard_forbidden_reset_estop(policy):
    guard = SafetyGuard(policy)
    state = EquipmentState(eq_id="STP-01", load_setpoint=100)

    decision = guard.validate("RESET_GLOBAL_ESTOP", state, {})

    assert decision.approved is False
    assert "hard-forbidden" in decision.reason.lower()


def test_increase_load_rejected(policy):
    guard = SafetyGuard(policy)
    state = EquipmentState(eq_id="STP-01", load_setpoint=80)

    decision = guard.validate("INCREASE_LOAD", state, {"step_percent": 10})

    assert decision.approved is False


def test_reduce_load_approved(policy):
    guard = SafetyGuard(policy)
    state = EquipmentState(eq_id="UTI-01", load_setpoint=100)

    decision = guard.validate("REDUCE_LOAD", state, {"step_percent": 10})

    assert decision.approved is True
    assert decision.metadata["new_load"] == 90


def test_reduce_load_respects_min_policy(policy):
    guard = SafetyGuard(policy)
    state = EquipmentState(eq_id="UTI-01", load_setpoint=25)

    decision = guard.validate("REDUCE_LOAD", state, {"step_percent": 10})

    assert decision.approved is False
    assert "minimum" in decision.reason.lower()


def test_advisory_mode_blocks_execution(policy):
    policy.mode = "advisory"
    guard = SafetyGuard(policy)
    state = EquipmentState(eq_id="UTI-01", load_setpoint=100)

    decision = guard.validate("REDUCE_LOAD", state, {"step_percent": 10})

    assert decision.approved is False
    assert "advisory" in decision.reason.lower()


def test_global_estop_blocks_load_reduction(policy):
    guard = SafetyGuard(policy)
    state = EquipmentState(
        eq_id="UTI-01",
        load_setpoint=100,
        global_estop_latched=True,
    )

    decision = guard.validate("REDUCE_LOAD", state, {"step_percent": 10})

    assert decision.approved is False
    assert "E-STOP" in decision.reason


def test_operator_ack_allowed_during_estop(policy):
    guard = SafetyGuard(policy)
    state = EquipmentState(
        eq_id="UTI-01",
        load_setpoint=100,
        global_estop_latched=True,
    )

    decision = guard.validate("REQUEST_OPERATOR_ACK", state, {})

    assert decision.approved is True
