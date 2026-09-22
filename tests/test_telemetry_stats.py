"""Unit tests for ScadaDatabase.get_equipment_statistics using a fixture DB."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import ScadaDatabase


def _make_fixture_db() -> str:
    """Create a temp DB with 24h of normal data plus one recent alarm row."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = ScadaDatabase(db_path=tmp.name)
    now = datetime.now()

    # 24h of normal telemetry: one row per 30 minutes = 48 rows
    for i in range(48, 0, -1):
        db.save_equipment_data("STP-01", {
            "voltage": 380.0, "current": 100.0 + i * 0.1,
            "active_power": 55.0, "reactive_power": 10.0,
            "apparent_power": 56.0, "power_factor": 0.95,
            "frequency": 50.0, "energy_kwh": 100.0 + i,
            "status": 1, "load": 70,
            "running_time_min": 600 - i, "trip_word": 0,
            "alarm_word": 0, "theta_per_mille": 400 + i,
            "heartbeat": i,
        })

    # One alarm event, backdated to 20 minutes ago
    db.save_alarm_event("STP-01", "38 Over-Temperature",
                        ansi_code="38", description="fixture alarm",
                        severity="MEDIUM")
    conn = db._get_connection()
    conn.execute(
        "UPDATE alarm_events SET timestamp = ? WHERE id = 1",
        ((now - timedelta(minutes=20)).isoformat(),),
    )
    conn.commit()
    conn.close()
    return tmp.name


def test_stats_with_data():
    path = _make_fixture_db()
    db = ScadaDatabase(db_path=path)
    s = db.get_equipment_statistics("STP-01", hours=24)

    assert s["sample_count"] == 48, f"sample_count={s['sample_count']}"
    assert 100.0 < s["current_mean"] < 105.0, f"current_mean={s['current_mean']}"
    assert s["current_std"] > 0.0
    assert 40.0 < s["theta_mean"] < 50.0, f"theta_mean={s['theta_mean']}"
    assert s["alarm_count_1h"] == 1, f"alarm_count_1h={s['alarm_count_1h']}"
    assert s["minutes_since_last_trip"] == -1
    os.unlink(path)


def test_stats_empty_equipment():
    path = _make_fixture_db()
    db = ScadaDatabase(db_path=path)
    s = db.get_equipment_statistics("UTI-01", hours=24)
    assert s["sample_count"] == 0
    assert s["alarm_count_1h"] == 0
    os.unlink(path)
