"""
Smoke tests for the industrial agent package.

Run from project root:

    python "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data\models\industrial_agent\tests\test_smoke.py"
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "models"

if str(MODELS_DIR) not in sys.path:
    sys.path.insert(0, str(MODELS_DIR))

from industrial_agent import (
    AgentConfig,
    EquipmentSnapshot,
    IndustrialCognitiveAgent,
    OperatingMode,
    Severity,
    SystemState,
)


def make_snapshot(**kwargs):
    base = dict(
        eq_id="STP-01",
        timestamp=time.time(),
        voltage=380.0,
        current=500.0,
        active_power=300.0,
        reactive_power=100.0,
        apparent_power=320.0,
        power_factor=0.90,
        frequency=50.0,
        energy_kwh=1000.0,
        load=75.0,
        temperature=60.0,
        running_time_min=10.0,
        motor_state=1,
        trip_word=0,
        alarm_word=0,
        lockout_status=0,
        theta_per_mille=500.0,
        trip_count=0,
        heartbeat=1,
    )
    base.update(kwargs)
    return EquipmentSnapshot(**base)


def test_overtemp_rule():
    agent = IndustrialCognitiveAgent(config=AgentConfig())
    snap = make_snapshot(temperature=96.0, heartbeat=1)
    agent.ingest_data({"STP-01": {
        "voltage": snap.voltage,
        "current": snap.current,
        "temperature": snap.temperature,
        "power_factor": snap.power_factor,
        "frequency": snap.frequency,
        "motor_status": snap.motor_state,
        "trip_word": snap.trip_word,
        "alarm_word": snap.alarm_word,
        "lockout_status": snap.lockout_status,
        "theta": snap.theta_per_mille / 1000.0,
        "trip_count": snap.trip_count,
        "heartbeat": snap.heartbeat,
        "load": snap.load,
        "running_time": snap.running_time_min,
    }})

    codes = {f.code for f in agent.findings}
    assert "OVERTEMP" in codes, f"Expected OVERTEMP, got {codes}"
    assert agent.state in {SystemState.CRITICAL, SystemState.WARNING}
    print("PASS: overtemp rule")


def test_advisory_mode_blocks_execution():
    executed = []

    def callback(action, context):
        executed.append((action, context))
        return True

    agent = IndustrialCognitiveAgent(
        config=AgentConfig(),
        command_callback=callback,
    )
    agent.set_operator_presence(True, OperatingMode.ADVISORY)

    agent.ingest_data({"STP-01": {
        "voltage": 380.0,
        "current": 1000.0,
        "temperature": 96.0,
        "power_factor": 0.9,
        "frequency": 50.0,
        "motor_status": 1,
        "trip_word": 0,
        "alarm_word": 0,
        "lockout_status": 0,
        "theta": 0.9,
        "trip_count": 0,
        "heartbeat": 1,
        "load": 100.0,
        "running_time": 10.0,
    }})

    assert executed == [], "Advisory mode must not execute actions"
    print("PASS: advisory mode blocks execution")


def test_autonomous_mode_allows_stop_equipment():
    executed = []

    def callback(action, context):
        executed.append((action, context))
        return True

    agent = IndustrialCognitiveAgent(
        config=AgentConfig(),
        command_callback=callback,
    )
    agent.set_operator_presence(False, OperatingMode.AUTONOMOUS)

    agent.ingest_data({"STP-01": {
        "voltage": 380.0,
        "current": 1050.0,
        "temperature": 96.0,
        "power_factor": 0.9,
        "frequency": 50.0,
        "motor_status": 1,
        "trip_word": 0,
        "alarm_word": 0,
        "lockout_status": 0,
        "theta": 1.0,
        "trip_count": 0,
        "heartbeat": 1,
        "load": 100.0,
        "running_time": 10.0,
    }})

    actions = [a for a, _ in executed]
    assert "stop_equipment" in actions, f"Expected stop_equipment, got {actions}"
    print("PASS: autonomous mode allows safe stop_equipment")


def test_estop_blocks_autonomous_actions():
    executed = []

    def callback(action, context):
        executed.append((action, context))
        return True

    agent = IndustrialCognitiveAgent(
        config=AgentConfig(),
        command_callback=callback,
    )
    agent.set_operator_presence(False, OperatingMode.AUTONOMOUS)

    agent.ingest_data(
        {"STP-01": {
            "voltage": 380.0,
            "current": 1050.0,
            "temperature": 96.0,
            "power_factor": 0.9,
            "frequency": 50.0,
            "motor_status": 1,
            "trip_word": 0,
            "alarm_word": 0,
            "lockout_status": 0,
            "theta": 1.0,
            "trip_count": 0,
            "heartbeat": 1,
            "load": 100.0,
            "running_time": 10.0,
        }},
        system_status={"estop_active": True},
    )

    assert executed == [], "E-STOP must block autonomous actions"
    assert agent.state == SystemState.ESTOP
    print("PASS: E-STOP blocks autonomous actions")


if __name__ == "__main__":
    test_overtemp_rule()
    test_advisory_mode_blocks_execution()
    test_autonomous_mode_allows_stop_equipment()
    test_estop_blocks_autonomous_actions()
    print("\nAll smoke tests passed.")
