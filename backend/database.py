"""
SQLite database layer for Nexus SCADA
Lightweight version with no external dependencies (only Python standard library)
"""
import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional


class ScadaDatabase:
    """SQLite database manager for storing factory data"""

    def __init__(self):
        # Database path: 'data' folder in the project root
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "scada.db")

        self._init_database()

    def _init_database(self):
        """Create initial tables"""
        conn = sqlite3.connect(self.db_path)
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
        conn.close()
        print(f"[DB] Database ready: {self.db_path}")

    def save_equipment_data(self, equipment_id: str, data: Dict) -> bool:
        """Save one data record for an equipment"""
        try:
            conn = sqlite3.connect(self.db_path)
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
            conn.close()
            return True
        except Exception as e:
            print(f"[DB Error] save_equipment_data: {e}")
            return False

    def get_latest_data(self, equipment_id: str) -> Optional[Dict]:
        """Get the latest record for an equipment"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM equipment_data
                WHERE equipment_id = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (equipment_id,))
            row = cursor.fetchone()
            columns = [desc[0] for desc in cursor.description]
            conn.close()
            return dict(zip(columns, row)) if row else None
        except Exception as e:
            print(f"[DB Error] get_latest_data: {e}")
            return None

    def get_history(self, equipment_id: str, hours: int = 24) -> List[Dict]:
        """Get historical data for an equipment"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            threshold = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor.execute("""
                SELECT * FROM equipment_data
                WHERE equipment_id = ? AND timestamp > ?
                ORDER BY timestamp
            """, (equipment_id, threshold))
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            conn.close()
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            print(f"[DB Error] get_history: {e}")
            return []

    def save_alarm_event(self, equipment_id: str, alarm_type: str,
                         ansi_code: str = None, description: str = None,
                         severity: str = 'MEDIUM') -> bool:
        """Log an alarm event"""
        try:
            conn = sqlite3.connect(self.db_path)
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
            conn.close()
            return True
        except Exception as e:
            print(f"[DB Error] save_alarm_event: {e}")
            return False

    def get_active_alarms(self) -> List[Dict]:
        """Get active (unacknowledged) alarms"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM alarm_events
                WHERE status = 'ACTIVE' AND acknowledged = 0
                ORDER BY timestamp DESC
            """)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            conn.close()
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            print(f"[DB Error] get_active_alarms: {e}")
            return []

    def acknowledge_alarm(self, alarm_id: int, user: str = 'system') -> bool:
        """Acknowledge an alarm"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE alarm_events
                SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), user, alarm_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[DB Error] acknowledge_alarm: {e}")
            return False

    def cleanup_old_data(self, days: int = 30):
        """Delete old data to prevent drive filling up"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            threshold = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute("DELETE FROM equipment_data WHERE timestamp < ?", (threshold,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            print(f"[DB] {deleted} old records deleted")
        except Exception as e:
            print(f"[DB Error] cleanup: {e}")


# A Singleton instance for use across the entire application
db = ScadaDatabase()