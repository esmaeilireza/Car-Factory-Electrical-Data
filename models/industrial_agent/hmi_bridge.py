"""
Small helper for HMI clients to consume agent status.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import requests
except Exception:
    requests = None


def fetch_agent_status(
    base_url: str = "http://localhost:8000",
    timeout: float = 1.0,
) -> Optional[Dict[str, Any]]:
    if requests is None:
        return None
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/agent/status", timeout=timeout)
        if r.ok:
            return r.json()
    except Exception:
        return None
    return None


def format_ai_line(status: Optional[Dict[str, Any]]) -> str:
    if not status:
        return "AI: offline"

    state = status.get("system_state", "UNKNOWN")
    mode = status.get("operating_mode", "ADVISORY")
    recs = status.get("recommendations", [])

    if recs:
        top = recs[0]
        msg = top.get("message", "")
        action = top.get("action", "manual_review")
        return f"AI [{state}/{mode}]: {msg} | Action: {action}"

    return f"AI [{state}/{mode}]: System nominal"
