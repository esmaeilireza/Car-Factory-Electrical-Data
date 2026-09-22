"""
Tamper-evident audit logger.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


class AuditLogger:
    def __init__(self, path: Optional[str] = None):
        if path is None:
            # package is in models/industrial_agent, project root is parents[2]
            project_root = Path(__file__).resolve().parents[2]
            path = project_root / "data" / "agent_audit.jsonl"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev_hash = "GENESIS"

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
