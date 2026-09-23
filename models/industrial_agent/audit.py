"""
Tamper-evident audit logger.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from obsidian_bridge import log_incident
    OBSIDIAN_AVAILABLE = True
except Exception:
    OBSIDIAN_AVAILABLE = False


class AuditLogger:
    def __init__(self, path: Optional[str] = None):
        if path is None:
            # package is in models/industrial_agent, project root is parents[2]
            project_root = Path(__file__).resolve().parents[2]
            path = project_root / "data" / "agent_audit.jsonl"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Chain resume: seed from the last record on disk so a restart
        # continues the existing chain instead of forking it. Tamper
        # evidence must survive process restarts.
        self.prev_hash = "GENESIS"
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("hash"):
                            self.prev_hash = rec["hash"]
            except Exception as e:
                print(f"[AUDIT] Chain resume failed, starting fresh: {e}")

    def log(self, event: str, payload: Dict[str, Any]) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "payload": payload,
            "prev_hash": self.prev_hash,
        }

        serialized = json.dumps(record, sort_keys=True, default=str)
        record_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        record["hash"] = record_hash
        self.prev_hash = record_hash

        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            print(f"[AUDIT] Write failed: {e}")

        # --- Obsidian incident logging (Feature B) ---
        # Only high-severity events become vault documents, so the
        # incident graph stays meaningful instead of filling with noise.
        # Fire-and-forget: audit JSONL write always completes first.
        try:
            if OBSIDIAN_AVAILABLE:
                severity = str(payload.get("severity", "low")).lower()
                safety_level = int(payload.get("safety_level", 1) or 1)
                if severity in ("high", "critical") or safety_level >= 3:
                    log_incident(
                        equipment_ids=payload.get("affected_equipment", [])
                            or payload.get("equipment", []),
                        fault_type=str(
                            payload.get("iec_reference", "")
                            or payload.get("code", "")
                            or "Unclassified"
                        ),
                        diagnosis=str(
                            payload.get("diagnosis", "")
                            or payload.get("message", "")
                            or event
                        ),
                        recommended_action=str(
                            payload.get("recommended_action", "")
                            or payload.get("action", "")
                            or "manual_review"
                        ),
                        severity=severity.upper(),
                        source=payload.get("source", "agent_audit"),
                    )
        except Exception:
            pass  # fire-and-forget - audit logging must never be blocked