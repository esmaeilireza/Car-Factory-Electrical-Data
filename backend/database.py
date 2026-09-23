"""
SQLite database layer for Nexus SCADA
Lightweight version with no external dependencies (only Python standard library)

FIXES APPLIED:
- try/finally on every connection to prevent leaks
- WAL mode + busy_timeout on every connection to prevent locking
- acknowledge_alarm returns False when no row was actually updated
- Constructor accepts optional db_path for unit testing
- get_equipment_statistics provides LLM-ready statistical context
- get_equipment_statistics uses SQL-side aggregation (avoids pulling ~20k rows into Python)
"""
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional


class ScadaDatabase:
    """SQLite database manager for storing factory data."""

    def __init__(self, db_path: Optional[str] = None):
        """Allow db_path override for unit tests; default is the project data dir."""
        if db_path is not None:
            self.db_path = db_path
        else:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            data_dir = os.path.join(project_root, "data")
            os.makedirs(data_dir, exist_ok=True)
            self.db_path = os.path.join(data_dir, "scada.db")

        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Create a new connection with concurrency-safe pragmas.
        FIX: WAL mode allows concurrent readers during Modbus writes.
        FIX: busy_timeout prevents immediate SQLITE_BUSY errors.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_database(self):
        """Create initial tables."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Equipment data table (Time-Series)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS equipment_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equipment_id TEXT NOT NULL,
                    voltage REAL DEFAULT 0,
                    current REAL DEFAULT 0,
                    active_power REAL DEFAULT 0,
                    reactive_power REAL DEFAULT 0,
                    apparent_power REAL DEFAULT 0,
                    power_factor REAL DEFAULT 0,
                    frequency REAL DEFAULT 50,
                    energy_kwh REAL DEFAULT 0,
                    status TEXT DEFAULT 'UNKNOWN',
                    load REAL DEFAULT 0,
                    running_time_min INTEGER DEFAULT 0,
                    trip_word INTEGER DEFAULT 0,
                    alarm_word INTEGER DEFAULT 0,
                    theta_per_mille INTEGER DEFAULT 0,
                    heartbeat INTEGER DEFAULT 0
                )
            """)

            # Index for faster queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_equipment_time
                ON equipment_data(equipment_id, timestamp)
            """)

            # Alarm events table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alarm_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equipment_id TEXT NOT NULL,
                    alarm_type TEXT NOT NULL,
                    ansi_code TEXT,
                    description TEXT,
                    severity TEXT DEFAULT 'MEDIUM',
                    status TEXT DEFAULT 'ACTIVE',
                    acknowledged INTEGER DEFAULT 0,
                    acknowledged_at TEXT,
                    acknowledged_by TEXT
                )
            """)

            # Operation logs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user TEXT,
                    action TEXT NOT NULL,
                    equipment_id TEXT,
                    details TEXT
                )
            """)

            conn.commit()
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

        print(f"[DB] Database ready: {self.db_path}")

    def save_equipment_data(self, equipment_id: str, data: Dict) -> bool:
        """Save one data record for an equipment."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO equipment_data
                (timestamp, equipment_id, voltage, current, active_power,
                 reactive_power, apparent_power, power_factor, frequency,
                 energy_kwh, status, load, running_time_min, trip_word,
                 alarm_word, theta_per_mille, heartbeat)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                equipment_id,
                data.get('voltage', 0),
                data.get('current', 0),
                data.get('active_power', 0),
                data.get('reactive_power', 0),
                data.get('apparent_power', 0),
                data.get('power_factor', 0),
                data.get('frequency', 50),
                data.get('energy_kwh', 0),
                data.get('status', 'UNKNOWN'),
                data.get('load', 0),
                data.get('running_time_min', 0),
                data.get('trip_word', 0),
                data.get('alarm_word', 0),
                data.get('theta_per_mille', 0),
                data.get('heartbeat', 0),
            ))
            conn.commit()
            return True
        except Exception as e:
            print(f"[DB Error] save_equipment_data: {e}")
            return False
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def get_latest_data(self, equipment_id: str) -> Optional[Dict]:
        """Get the latest record for an equipment."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM equipment_data
                WHERE equipment_id = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (equipment_id,))
            row = cursor.fetchone()
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row)) if row else None
        except Exception as e:
            print(f"[DB Error] get_latest_data: {e}")
            return None
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def get_history(self, equipment_id: str, hours: int = 24) -> List[Dict]:
        """Get historical data for an equipment."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            threshold = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor.execute("""
                SELECT * FROM equipment_data
                WHERE equipment_id = ? AND timestamp > ?
                ORDER BY timestamp
            """, (equipment_id, threshold))
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            print(f"[DB Error] get_history: {e}")
            return []
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def save_alarm_event(self, equipment_id: str, alarm_type: str,
                         ansi_code: str = None, description: str = None,
                         severity: str = 'MEDIUM') -> bool:
        """Log an alarm event."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO alarm_events
                (timestamp, equipment_id, alarm_type, ansi_code, description, severity)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                equipment_id, alarm_type, ansi_code, description, severity
            ))
            conn.commit()
            return True
        except Exception as e:
            print(f"[DB Error] save_alarm_event: {e}")
            return False
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def get_active_alarms(self) -> List[Dict]:
        """Get active (unacknowledged) alarms."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM alarm_events
                WHERE status = 'ACTIVE' AND acknowledged = 0
                ORDER BY timestamp DESC
            """)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            print(f"[DB Error] get_active_alarms: {e}")
            return []
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def acknowledge_alarm(self, alarm_id: int, user: str = 'system') -> bool:
        """
        Acknowledge an alarm.
        FIX: Returns False when no matching row exists (checks rowcount).
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE alarm_events
                SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ?
                WHERE id = ? AND acknowledged = 0
            """, (datetime.now().isoformat(), user, alarm_id))
            conn.commit()
            # FIX: Only return True if a row was actually modified
            return cursor.rowcount > 0
        except Exception as e:
            print(f"[DB Error] acknowledge_alarm: {e}")
            return False
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def cleanup_old_data(self, days: int = 30):
        """Delete old data to prevent drive filling up."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            threshold = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute("DELETE FROM equipment_data WHERE timestamp < ?", (threshold,))
            deleted = cursor.rowcount
            conn.commit()
            print(f"[DB] {deleted} old records deleted")
        except Exception as e:
            print(f"[DB Error] cleanup: {e}")
        finally:
            conn.close()  # FIX: Guaranteed close via try/finally

    def get_equipment_statistics(self, equipment_id: str, hours: int = 24) -> Dict:
        """
        Compute statistical context for one equipment over a time window.

        Returns a compact dict suitable for injection into LLM diagnostic
        prompts. Pure read-only function - no side effects.
        """
        conn = self._get_connection()
        try:
            now = datetime.now()
            window_start = (now - timedelta(hours=hours)).isoformat()
            hour_start = (now - timedelta(hours=1)).isoformat()

            # --- Telemetry aggregates for the window (SQL-side; avoids
            #     pulling ~20k rows into Python per agent cycle) ---
            row = conn.execute(
                """
                SELECT COUNT(*),
                       AVG(current),
                       AVG(current * current),
                       AVG(theta_per_mille),
                       MAX(theta_per_mille)
                FROM equipment_data
                WHERE equipment_id = ? AND timestamp > ?
                """,
                (equipment_id, window_start),
            ).fetchone()

            n = int(row[0]) if row[0] is not None else 0
            if n > 0:
                current_mean = float(row[1])
                mean_sq = float(row[2])
                # E[x^2] - E[x]^2; clamp tiny negative float error
                current_std = max(0.0, mean_sq - current_mean ** 2) ** 0.5
                theta_mean = float(row[3]) / 10.0   # stored x10 -> percent
                theta_max = float(row[4]) / 10.0
            else:
                current_mean = current_std = theta_mean = theta_max = 0.0

            # --- Alarms in the last hour (from the alarm_events table) ---
            alarm_count_1h = conn.execute(
                """
                SELECT COUNT(*) FROM alarm_events
                WHERE equipment_id = ? AND timestamp > ?
                """,
                (equipment_id, hour_start),
            ).fetchone()[0]

            # --- Minutes since the last protection trip ---
            last_trip_row = conn.execute(
                """
                SELECT timestamp FROM equipment_data
                WHERE equipment_id = ? AND trip_word > 0
                ORDER BY id DESC LIMIT 1
                """,
                (equipment_id,),
            ).fetchone()

            if last_trip_row:
                try:
                    last_trip = datetime.fromisoformat(last_trip_row[0])
                    minutes_since_last_trip = int((now - last_trip).total_seconds() // 60)
                except ValueError:
                    minutes_since_last_trip = -1
            else:
                minutes_since_last_trip = -1  # never tripped in recorded history

            return {
                "current_mean": round(current_mean, 2),
                "current_std": round(current_std, 2),
                "theta_mean": round(theta_mean, 1),
                "theta_max": round(theta_max, 1),
                "alarm_count_1h": int(alarm_count_1h),
                "minutes_since_last_trip": minutes_since_last_trip,
                "sample_count": n,
            }
        except Exception as e:
            print(f"[DB Error] get_equipment_statistics: {e}")
            return {
                "current_mean": 0.0, "current_std": 0.0,
                "theta_mean": 0.0, "theta_max": 0.0,
                "alarm_count_1h": 0, "minutes_since_last_trip": -1,
                "sample_count": 0,
            }
        finally:
            conn.close()


# A Singleton instance for use across the entire application
db = ScadaDatabase()