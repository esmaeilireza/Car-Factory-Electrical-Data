from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_POLICY_PATH = Path("configs/remediation_policy.json")


@dataclass
class RemediationPolicy:
    raw: Dict[str, Any]
    version: str = "0"
    mode: str = "advisory"
    allowed_actions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    forbidden_actions: List[str] = field(default_factory=list)
    global_limits: Dict[str, Any] = field(default_factory=dict)
    escalation: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemediationPolicy":
        return cls(
            raw=data,
            version=str(data.get("version", "0")),
            mode=str(data.get("mode", "advisory")),
            allowed_actions=data.get("allowed_actions", {}) or {},
            forbidden_actions=data.get("forbidden_actions", []) or [],
            global_limits=data.get("global_limits", {}) or {},
            escalation=data.get("escalation", {}) or {},
        )

    def is_action_allowed(self, action_type: str) -> bool:
        if action_type in self.forbidden_actions:
            return False

        cfg = self.allowed_actions.get(action_type)
        if not cfg:
            return False

        return bool(cfg.get("enabled", False))

    def action_config(self, action_type: str) -> Dict[str, Any]:
        return self.allowed_actions.get(action_type, {}) or {}


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> RemediationPolicy:
    p = Path(path)
    if not p.is_file():
        return RemediationPolicy.from_dict({})

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return RemediationPolicy.from_dict({})

    return RemediationPolicy.from_dict(data)
