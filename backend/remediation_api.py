from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/agent/remediation", tags=["remediation"])
ROOT = Path(__file__).resolve().parents[1]
AUDIT_AGENT = ROOT / "data" / "agent_audit.jsonl"
POLICY_PATH = ROOT / "configs" / "remediation_policy.json"


class ModeRequest(BaseModel):
    mode: str


def _agent() -> Optional[Any]:
    try:
        from .api import get_agent
        return get_agent()
    except Exception:
        return None


def _engine(a):
    if a is None: return None, "agent unavailable"
    e = getattr(a, "remediation_engine", None)
    if e is not None: return e, None
    try:
        from industrial_agent.remediation_hook import ensure_engine
        return ensure_engine(a), None
    except Exception as ex:
        return None, str(ex)


@router.get("/status")
def status():
    e, err = _engine(_agent())
    if e is None:
        return {"available": False, "mode": "unavailable", "reason": err or "engine not initialized"}
    p = getattr(e, "policy", None)
    return {"available": True, "mode": getattr(p, "mode", "?"),
            "policy_version": getattr(p, "version", "?"),
            "allowed_actions": list(getattr(p, "allowed_actions", {}).keys()),
            "forbidden_actions": list(getattr(p, "forbidden_actions", [])),
            "has_plc_command_path": getattr(e, "client", None) is not None}


@router.post("/mode")
def set_mode(req: ModeRequest):
    e, err = _engine(_agent())
    if e is None: return {"ok": False, "error": err or "engine not initialized"}
    ok = e.set_mode(req.mode)
    return {"ok": ok, "mode": e.policy.mode, "error": None if ok else f"unsupported: {req.mode}"}


@router.get("/policy")
def policy():
    if not POLICY_PATH.is_file(): return {"error": "missing", "path": str(POLICY_PATH)}
    try: return json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except Exception as ex: return {"error": str(ex)}


@router.get("/history")
def history(limit: int = 50):
    if not AUDIT_AGENT.is_file(): return {"events": [], "reason": "audit missing"}
    want = {"REMEDIATION_DECISION","AUTO_REMEDIATION_EXECUTED","REMEDIATION_NOT_EXECUTED",
            "REMEDIATION_HOOK_TRIGGERED","REMEDIATION_HOOK_SKIPPED","REMEDIATION_HOOK_ERROR",
            "REMEDIATION_ENGINE_READY","REMEDIATION_HOOK_COMPLETED"}
    out = []
    try: lines = AUDIT_AGENT.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as ex: return {"events": [], "reason": str(ex)}
    for ln in reversed(lines[-2000:]):
        ln = ln.strip()
        if not ln: continue
        try: rec = json.loads(ln)
        except Exception: continue
        if rec.get("event") in want:
            out.append(rec)
            if len(out) >= limit: break
    out.reverse()
    return {"events": out, "count": len(out)}
