"""
NEXUS SCADA remediation API.

This module is the single active remediation API layer.

Important ownership rules:
  - agent.py owns REMEDIATION_HOOK_TRIGGERED
  - remediation_hook.py owns REMEDIATION_HOOK_RECEIVED / COMPLETED / ERROR
  - remediation/engine.py owns REMEDIATION_DECISION and execution events
  - this API layer must NOT emit REMEDIATION_DECISION
  - this API layer must NOT create a second remediation router with the same prefix
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/remediation", tags=["remediation"])

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
MODELS_DIR = PROJECT_ROOT / "models"

# Make project imports reliable even if this module is imported directly.
for _path_entry in (str(BASE_DIR), str(MODELS_DIR), str(PROJECT_ROOT)):
    if _path_entry not in sys.path:
        sys.path.insert(0, _path_entry)

AUDIT_AGENT = PROJECT_ROOT / "data" / "agent_audit.jsonl"
POLICY_PATH = Path(
    os.environ.get(
        "NEXUS_REMEDIATION_POLICY_PATH",
        str(PROJECT_ROOT / "configs" / "remediation_policy.json"),
    )
)

ALLOWED_MODES = {
    "advisory",
    "limited_autonomous",
}

MODE_ALIASES = {
    "adv": "advisory",
    "advisory": "advisory",
    "manual": "advisory",
    "recommend": "advisory",
    "auto": "limited_autonomous",
    "autonomous": "limited_autonomous",
    "full_autonomous": "limited_autonomous",
    "limited": "limited_autonomous",
    "limited_auto": "limited_autonomous",
    "limited-autonomous": "limited_autonomous",
    "limited_autonomous": "limited_autonomous",
}

# Optional agent provider injected by backend/api.py after app.state.agent exists.
_agent_provider: Optional[Callable[[], Any]] = None


class ModeRequest(BaseModel):
    mode: str
    operator_present: Optional[bool] = None


class TriggerRequest(BaseModel):
    eq_id: str
    code: str = "MANUAL_TRIGGER"
    severity: str = "HIGH"
    safety_level: int = 3
    trip_word: int = 0
    lockout_status: bool = True
    evidence: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public wiring helper
# ---------------------------------------------------------------------------

def set_agent_provider(provider: Optional[Callable[[], Any]]) -> None:
    """
    Allow backend/api.py to inject a callable that returns the live agent.

    This avoids fragile sys.modules lookups and circular imports.
    """
    global _agent_provider
    _agent_provider = provider


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _normalize_mode(value: Any) -> str:
    raw = _enum_text(value).lower().strip()
    raw = raw.replace("-", "_").replace(" ", "_")

    mapped = MODE_ALIASES.get(raw, raw)

    if mapped not in ALLOWED_MODES:
        raise ValueError(f"unsupported remediation mode: {value!r}")

    return mapped


def verify_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> bool:
    """
    Lightweight API-key guard.

    If NEXUS_API_KEY is unset or empty, authentication is disabled.
    This matches the local prototype/test behavior.
    """
    expected = os.getenv("NEXUS_API_KEY", "").strip()

    if not expected:
        return True

    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    return True


def _get_agent(request: Optional[Request] = None) -> Any:
    # Preferred: FastAPI request app state.
    if request is not None:
        try:
            agent = getattr(request.app.state, "agent", None)
            if agent is not None:
                return agent
        except Exception:
            pass

    # Injected provider from backend/api.py.
    if _agent_provider is not None:
        try:
            agent = _agent_provider()
            if agent is not None:
                return agent
        except Exception:
            pass

    # Fallback: already-loaded modules.
    for modname in ("backend.api", "api", "app", "backend", "main"):
        mod = sys.modules.get(modname)
        if mod is not None:
            agent = getattr(mod, "agent", None)
            if agent is not None:
                return agent

    # Last fallback: try importing backend.api.
    try:
        mod = importlib.import_module("backend.api")
        return getattr(mod, "agent", None)
    except Exception:
        return None


def _hook_module() -> Any:
    for name in (
        "industrial_agent.remediation_hook",
        "models.industrial_agent.remediation_hook",
    ):
        try:
            return importlib.import_module(name)
        except Exception:
            continue

    return None


def _obsidian_module() -> Any:
    try:
        import obsidian_bridge as ob
        return ob
    except Exception:
        pass

    try:
        from models import obsidian_bridge as ob
        return ob
    except Exception:
        pass

    return None


def _attach_engine(engine: Any, agent: Any) -> None:
    """
    Safely attach runtime dependencies to the remediation engine.

    This is intentionally defensive: the engine implementation may vary
    across project revisions.
    """
    if engine is None or agent is None:
        return

    audit = getattr(agent, "audit", None)
    obsidian = _obsidian_module()
    db = getattr(agent, "db", None)
    config = getattr(agent, "config", None)
    command_callback = getattr(agent, "command_callback", None)

    if not getattr(engine, "agent", None):
        try:
            engine.agent = agent
        except Exception:
            pass

    if audit is not None:
        if getattr(engine, "audit", None) is None:
            try:
                engine.audit = audit
            except Exception:
                pass

        if hasattr(engine, "audit_logger") and getattr(engine, "audit_logger", None) is None:
            try:
                engine.audit_logger = audit
            except Exception:
                pass

    if obsidian is not None and getattr(engine, "obsidian", None) is None:
        try:
            engine.obsidian = obsidian
        except Exception:
            pass

    if obsidian is not None and hasattr(engine, "obsidian_bridge") and getattr(engine, "obsidian_bridge", None) is None:
        try:
            engine.obsidian_bridge = obsidian
        except Exception:
            pass

    if db is not None and getattr(engine, "db", None) is None:
        try:
            engine.db = db
        except Exception:
            pass

    if config is not None and getattr(engine, "config", None) is None:
        try:
            engine.config = config
        except Exception:
            pass

    if command_callback is not None and not getattr(engine, "command_callback", None):
        try:
            engine.command_callback = command_callback
        except Exception:
            pass


def resolve_engine(
    agent: Any,
    retries: int = 5,
    delay: float = 0.5,
) -> Tuple[Optional[Any], Optional[str]]:
    """
    Resolve the remediation engine from the agent.

    FIX: retry logic for timing resilience.

    If the probe hits /mode or /status immediately after backend start,
    the agent may exist but not yet have attached its remediation_engine.
    We wait briefly and re-check.
    """
    if agent is None:
        return None, "agent unavailable"

    # Fast path: engine already attached.
    engine = getattr(agent, "remediation_engine", None)
    if engine is not None:
        _attach_engine(engine, agent)
        return engine, None

    hook = _hook_module()
    if hook is None:
        return None, "remediation_hook unavailable"

    ensure_names = (
        "ensure_remediation_engine",
        "ensure_engine",
        "get_remediation_engine",
        "create_remediation_engine",
        "build_remediation_engine",
    )

    last_error: Optional[str] = None

    for attempt in range(max(0, int(retries)) + 1):
        for fname in ensure_names:
            fn = getattr(hook, fname, None)

            if not callable(fn):
                continue

            try:
                engine = fn(agent)

                # Some variants may return (engine, error) or similar.
                if isinstance(engine, tuple):
                    engine = engine[0] if engine else None

                if engine is not None:
                    _attach_engine(engine, agent)
                    try:
                        setattr(agent, "remediation_engine", engine)
                    except Exception:
                        pass

                    return engine, None

                last_error = f"{fname} returned None"
            except Exception as exc:
                last_error = f"{fname} failed: {exc}"

        if attempt < retries:
            time.sleep(delay * (attempt + 1))

    return None, last_error or "engine unavailable"


def _set_engine_mode(engine: Any, mode: str) -> bool:
    if engine is None:
        return False

    method_names = (
        "set_mode",
        "set_operating_mode",
        "configure_mode",
        "set_remediation_mode",
    )

    for name in method_names:
        fn = getattr(engine, name, None)

        if not callable(fn):
            continue

        try:
            rv = fn(mode)
            if rv is None:
                return True
            return bool(rv)
        except TypeError:
            try:
                rv = fn(mode=mode)
                if rv is None:
                    return True
                return bool(rv)
            except Exception:
                continue
        except Exception:
            continue

    # Attribute fallback.
    for attr in ("mode", "operating_mode", "remediation_mode"):
        if hasattr(engine, attr):
            try:
                setattr(engine, attr, mode)
                return True
            except Exception:
                continue

    return False


def _operating_mode_cls() -> Any:
    for modname in (
        "industrial_agent.models",
        "models.industrial_agent.models",
    ):
        try:
            mod = importlib.import_module(modname)
            cls = getattr(mod, "OperatingMode", None)
            if cls is not None:
                return cls
        except Exception:
            continue

    return None


def _set_agent_mode(
    agent: Any,
    mode: str,
    operator_present: Optional[bool] = None,
) -> List[str]:
    changed: List[str] = []

    if agent is None:
        return changed

    if operator_present is not None and hasattr(agent, "set_operator_presence"):
        try:
            agent.set_operator_presence(bool(operator_present))
            changed.append("operator_presence")
        except Exception:
            pass

    # Try to map to OperatingMode enum if available.
    try:
        cls = _operating_mode_cls()

        if cls is not None and hasattr(agent, "mode"):
            member = None
            upper = mode.upper()

            members = getattr(cls, "__members__", {})

            if upper in members:
                member = members[upper]
            else:
                for m in members.values():
                    if _enum_text(m).lower() == mode:
                        member = m
                        break

            if member is not None:
                agent.mode = member
                changed.append("agent.mode")
    except Exception:
        pass

    method_names = (
        "set_operating_mode",
        "set_mode",
        "set_remediation_mode",
    )

    for name in method_names:
        fn = getattr(agent, name, None)

        if not callable(fn):
            continue

        try:
            fn(mode)
            changed.append(name)
            break
        except TypeError:
            try:
                fn(mode=mode)
                changed.append(name)
                break
            except Exception:
                continue
        except Exception:
            continue

    return changed


def _current_mode(agent: Any, engine: Any) -> str:
    for obj in (engine, agent):
        if obj is None:
            continue

        policy = getattr(obj, "policy", None)
        if policy is not None:
            value = getattr(policy, "mode", None)
            text = _enum_text(value).lower()
            if text:
                return text

        for attr in ("mode", "operating_mode", "remediation_mode"):
            value = getattr(obj, attr, None)
            text = _enum_text(value).lower()
            if text:
                return text

    return "unknown"


def _policy_mode(policy: Any) -> str:
    if policy is None:
        return "unknown"

    value = getattr(policy, "mode", None)
    text = _enum_text(value).lower()

    return text or "unknown"


def _policy_version(policy: Any) -> Optional[str]:
    if policy is None:
        return None

    value = getattr(policy, "version", None)
    text = _enum_text(value)

    return text or None


def _policy_allowed_actions(policy: Any) -> List[str]:
    if policy is None:
        return []

    allowed = getattr(policy, "allowed_actions", None)

    if isinstance(allowed, dict):
        return [str(k) for k in allowed.keys()]

    if isinstance(allowed, (list, tuple, set)):
        return [str(x) for x in allowed]

    raw = getattr(policy, "raw", None)

    if isinstance(raw, dict):
        allowed_raw = raw.get("allowed_actions", [])

        if isinstance(allowed_raw, dict):
            return [str(k) for k in allowed_raw.keys()]

        if isinstance(allowed_raw, (list, tuple, set)):
            return [str(x) for x in allowed_raw]

    return []


def _policy_forbidden_actions(policy: Any) -> List[str]:
    if policy is None:
        return []

    forbidden = getattr(policy, "forbidden_actions", None)

    if isinstance(forbidden, (list, tuple, set)):
        return [str(x) for x in forbidden]

    raw = getattr(policy, "raw", None)

    if isinstance(raw, dict):
        forbidden_raw = raw.get("forbidden_actions", [])

        if isinstance(forbidden_raw, (list, tuple, set)):
            return [str(x) for x in forbidden_raw]

    return []


def _load_policy_from_disk() -> Dict[str, Any]:
    if not POLICY_PATH.is_file():
        return {
            "available": False,
            "source": "file",
            "error": "policy file missing",
            "path": str(POLICY_PATH),
        }

    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        return {
            "available": True,
            "source": "file",
            "policy": raw,
        }
    except Exception as exc:
        return {
            "available": False,
            "source": "file",
            "error": f"failed to load policy: {type(exc).__name__}: {exc}",
        }


def _engine_has_plc_command_path(engine: Any) -> bool:
    if engine is None:
        return False

    if hasattr(engine, "can_command_plc"):
        try:
            return bool(engine.can_command_plc())
        except Exception:
            pass

    if getattr(engine, "client", None) is not None:
        return True

    if getattr(engine, "_direct_client", None) is not None:
        return True

    if getattr(engine, "modbus_client", None) is not None:
        return True

    return False


def _audit(agent: Any, event: str, payload: Dict[str, Any]) -> None:
    audit = getattr(agent, "audit", None)

    if audit is None:
        return

    payload = payload if isinstance(payload, dict) else {"value": payload}

    attempts = (
        lambda: audit.log(event, payload),
        lambda: audit.log(event=event, payload=payload),
        lambda: audit.write(event, payload),
        lambda: audit.write({"event": event, **payload}),
    )

    last_exc: Optional[Exception] = None

    for attempt in attempts:
        try:
            attempt()
            return
        except Exception as exc:
            last_exc = exc

    logger.warning(
        "remediation_api: audit write failed event=%s eq_id=%s error=%s",
        event,
        payload.get("eq_id"),
        last_exc,
        exc_info=True,
    )


def _read_recent_audit_records(
    path: Path,
    max_records: int = 500,
    max_bytes: int = 2_000_000,
) -> List[Dict[str, Any]]:
    """
    Read recent JSONL audit records without loading the entire file.
    """
    try:
        if not path.exists():
            return []

        size = path.stat().st_size
        if size == 0:
            return []

        read_size = min(size, max_bytes)

        with open(path, "rb") as f:
            f.seek(size - read_size)
            data = f.read(read_size)

        lines = data.splitlines()

        # If we did not read from the beginning, the first line may be partial.
        if size > read_size and lines:
            lines = lines[1:]

        records: List[Dict[str, Any]] = []

        for raw in reversed(lines):
            try:
                line = raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue

            if not line:
                continue

            try:
                rec = json.loads(line)
            except Exception:
                continue

            if isinstance(rec, dict):
                records.append(rec)

            if len(records) >= max_records:
                break

        records.reverse()
        return records

    except Exception:
        return []


def _payload_eq_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    eq = payload.get("eq_id")
    if eq:
        return str(getattr(eq, "value", eq) or "").strip().upper()

    decision = payload.get("decision")
    if isinstance(decision, dict):
        eq = decision.get("eq_id")
        if eq:
            return str(getattr(eq, "value", eq) or "").strip().upper()

        evidence = decision.get("evidence")
        if isinstance(evidence, dict):
            for k in ("primary_eq_id", "target_eq_id", "device_id", "eq_id"):
                v = evidence.get(k)
                if v:
                    return str(getattr(v, "value", v) or "").strip().upper()

    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        for k in ("primary_eq_id", "target_eq_id", "device_id", "eq_id"):
            v = evidence.get(k)
            if v:
                return str(getattr(v, "value", v) or "").strip().upper()

    return ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
def remediation_status(request: Request) -> Dict[str, Any]:
    """
    Report current remediation mode, policy, and PLC connectivity.

    Always returns 200. When the engine is not initialized, reports
    available=false with a consistent shape.
    """
    agent = _get_agent(request)

    if agent is None:
        return {
            "available": False,
            "agent_available": False,
            "engine_available": False,
            "mode": "unknown",
            "policy_version": None,
            "allowed_actions": [],
            "forbidden_actions": [],
            "has_plc_command_path": False,
            "has_obsidian_bridge": False,
            "reason": "agent unavailable",
        }

    engine, engine_error = resolve_engine(agent, retries=0, delay=0.0)

    if engine is None:
        return {
            "available": False,
            "agent_available": True,
            "engine_available": False,
            "mode": _current_mode(agent, None),
            "policy_version": None,
            "allowed_actions": [],
            "forbidden_actions": [],
            "has_plc_command_path": False,
            "has_obsidian_bridge": False,
            "reason": engine_error or "Remediation engine not initialized.",
        }

    policy = getattr(engine, "policy", None)

    return {
        "available": True,
        "agent_available": True,
        "engine_available": True,
        "mode": _policy_mode(policy) or _current_mode(agent, engine),
        "policy_version": _policy_version(policy),
        "allowed_actions": _policy_allowed_actions(policy),
        "forbidden_actions": _policy_forbidden_actions(policy),
        "has_plc_command_path": _engine_has_plc_command_path(engine),
        "has_obsidian_bridge": (
            getattr(engine, "obsidian", None) is not None
            or getattr(engine, "obsidian_bridge", None) is not None
        ),
        "reason": None,
    }


@router.post("/mode", dependencies=[Depends(verify_api_key)])
def set_remediation_mode(
    req: ModeRequest,
    request: Request,
) -> Dict[str, Any]:
    """
    Change the remediation mode.

    Only modes in ALLOWED_MODES are accepted. This is enforced before the
    request reaches the engine, so a typo or unexpected value cannot weaken
    the safety layer.
    """
    agent = _get_agent(request)

    if agent is None:
        raise HTTPException(status_code=409, detail="agent unavailable")

    try:
        mode = _normalize_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    engine, engine_error = resolve_engine(agent, retries=5, delay=0.5)

    engine_ok = False
    if engine is not None:
        engine_ok = _set_engine_mode(engine, mode)

    agent_changes = _set_agent_mode(agent, mode, req.operator_present)

    _audit(
        agent,
        "REMEDIATION_MODE_CHANGED",
        {
            "mode": mode,
            "requested_mode": req.mode,
            "operator_present": req.operator_present,
            "engine_ok": engine_ok,
            "agent_changes": agent_changes,
            "engine_error": engine_error,
            "source": "remediation_api",
        },
    )

    if not engine_ok and not agent_changes:
        raise HTTPException(
            status_code=503,
            detail=(
                "remediation mode not accepted: "
                f"{engine_error or 'engine unavailable'}"
            ),
        )

    policy = getattr(engine, "policy", None) if engine is not None else None

    return {
        "ok": True,
        "mode": _policy_mode(policy) or _current_mode(agent, engine),
        "requested_mode": req.mode,
        "normalized_mode": mode,
        "engine_ok": engine_ok,
        "agent_changes": agent_changes,
        "engine_error": engine_error,
        "error": None,
    }


@router.get("/policy")
def remediation_policy(request: Request) -> Dict[str, Any]:
    """
    Return the current remediation policy document.

    Prefers the live engine's policy; falls back to loading it from disk.
    """
    agent = _get_agent(request)
    engine, _ = resolve_engine(agent, retries=0, delay=0.0) if agent is not None else (None, None)

    policy_obj = getattr(engine, "policy", None) if engine is not None else None

    if policy_obj is not None:
        raw = getattr(policy_obj, "raw", None)

        if raw is not None:
            return {
                "available": True,
                "source": "engine",
                "policy": raw,
            }

        return {
            "available": True,
            "source": "engine",
            "policy": {
                "mode": _policy_mode(policy_obj),
                "version": _policy_version(policy_obj),
                "allowed_actions": _policy_allowed_actions(policy_obj),
                "forbidden_actions": _policy_forbidden_actions(policy_obj),
            },
        }

    disk = _load_policy_from_disk()

    if disk.get("available"):
        return disk

    return {
        "available": False,
        "source": "none",
        "error": disk.get("error") or "Remediation module is not importable and no engine is attached.",
        "path": str(POLICY_PATH),
    }


@router.get("/history")
def remediation_history(
    request: Request,
    limit: int = 100,
    eq_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return recent remediation events from the agent audit log.

    This endpoint is read-only and must not emit audit events itself.
    """
    limit = max(1, min(int(limit or 100), 1000))
    eq_filter = str(eq_id or "").strip().upper()

    wanted = {
        "REMEDIATION_DECISION",
        "AUTO_REMEDIATION_EXECUTED",
        "REMEDIATION_NOT_EXECUTED",
        "REMEDIATION_HOOK_TRIGGERED",
        "REMEDIATION_HOOK_RECEIVED",
        "REMEDIATION_HOOK_COMPLETED",
        "REMEDIATION_HOOK_SKIPPED",
        "REMEDIATION_HOOK_ERROR",
        "REMEDIATION_ENGINE_READY",
        "REMEDIATION_MODE_CHANGED",
        "REMEDIATION_GUARD_COUNTERS_RESET",
        "REMEDIATION_DIRECT_PLC_CLIENT_OPENED",
        "REMEDIATION_OBSIDIAN_APPENDED",
        "REMEDIATION_OBSIDIAN_CREATED",
        "REMEDIATION_OBSIDIAN_APPEND_ERROR",
        "REMEDIATION_OBSIDIAN_CREATE_ERROR",
        "REMEDIATION_OBSIDIAN_ALREADY_PRESENT",
        "REMEDIATION_OBSIDIAN_APPEND_TIMEOUT",
    }

    records = _read_recent_audit_records(
        AUDIT_AGENT,
        max_records=max(limit * 10, 2000),
        max_bytes=4_000_000,
    )

    entries: List[Dict[str, Any]] = []

    for rec in reversed(records):
        event = rec.get("event")

        if event not in wanted:
            continue

        payload = rec.get("payload", {})
        rec_eq = _payload_eq_id(payload)

        if eq_filter and rec_eq != eq_filter:
            continue

        entries.append(
            {
                "ts": rec.get("ts"),
                "event": event,
                "eq_id": rec_eq,
                "payload": payload,
            }
        )

        if len(entries) >= limit:
            break

    entries.reverse()

    return {
        "available": AUDIT_AGENT.is_file(),
        "source": "agent_audit",
        "path": str(AUDIT_AGENT),
        "count": len(entries),
        "entries": entries,
    }


@router.post("/reset-guards", dependencies=[Depends(verify_api_key)])
def reset_remediation_guards(request: Request) -> Dict[str, Any]:
    """
    Reset consecutive-action guards in the remediation engine.

    This is intended for live verification and operator-triggered resets.
    It does not change policy, mode, or forbidden actions.
    """
    agent = _get_agent(request)

    if agent is None:
        raise HTTPException(status_code=409, detail="agent unavailable")

    engine, engine_error = resolve_engine(agent, retries=3, delay=0.25)

    if engine is None:
        return {
            "ok": False,
            "error": engine_error or "Remediation engine not initialized.",
        }

    fn = (
        getattr(engine, "reset_guard_counters", None)
        or getattr(engine, "reset_guards", None)
        or getattr(engine, "reset_consecutive_counters", None)
    )

    if not callable(fn):
        return {
            "ok": False,
            "error": "reset_guard_counters not available on engine.",
        }

    try:
        fn()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"reset failed: {exc}",
        }

    policy = getattr(engine, "policy", None)

    _audit(
        agent,
        "REMEDIATION_GUARD_COUNTERS_RESET",
        {
            "mode": _policy_mode(policy) or _current_mode(agent, engine),
            "source": "remediation_api",
        },
    )

    return {
        "ok": True,
        "mode": _policy_mode(policy) or _current_mode(agent, engine),
        "reset": "guard_counters",
    }


@router.post("/trigger", dependencies=[Depends(verify_api_key)])
def manual_remediation_trigger(
    req: TriggerRequest,
    request: Request,
) -> Dict[str, Any]:
    """
    Optional manual remediation trigger.

    Disabled by default. Enable only in test environments with:

        NEXUS_ENABLE_MANUAL_REMEDIATION=1

    This endpoint delegates to remediation_hook and does not emit
    remediation decision events itself.
    """
    if os.getenv("NEXUS_ENABLE_MANUAL_REMEDIATION", "0") != "1":
        raise HTTPException(
            status_code=403,
            detail="manual remediation trigger disabled",
        )

    agent = _get_agent(request)

    if agent is None:
        raise HTTPException(status_code=409, detail="agent unavailable")

    hook = _hook_module()

    if hook is None or not hasattr(hook, "run_remediation_after_finding"):
        raise HTTPException(status_code=503, detail="remediation hook unavailable")

    eq_id = str(req.eq_id or "").strip().upper()

    if not eq_id or eq_id == "SYSTEM":
        raise HTTPException(status_code=400, detail="valid eq_id required")

    evidence = dict(req.evidence or {})
    evidence.setdefault("primary_eq_id", eq_id)
    evidence.setdefault("target_eq_id", eq_id)
    evidence.setdefault("device_id", eq_id)
    evidence.setdefault("eq_id", eq_id)
    evidence.setdefault("trip_word", req.trip_word)
    evidence.setdefault("lockout_status", req.lockout_status)

    entry = {
        "eq_id": eq_id,
        "code": req.code,
        "severity": req.severity,
        "safety_level": req.safety_level,
        "message": f"{eq_id}: manual remediation trigger",
        "source": "remediation_api",
        "evidence": evidence,
        "timestamp": time.time(),
    }

    result = hook.run_remediation_after_finding(agent, entry)

    if result is None:
        return {
            "status": "skipped_or_failed",
            "eq_id": eq_id,
            "detail": "remediation hook returned no result",
        }

    if isinstance(result, tuple) and len(result) >= 2:
        dec, res = result[0], result[1]
    else:
        dec, res = result, {}

    return {
        "status": "ok",
        "eq_id": eq_id,
        "decision": dec,
        "result": res,
    }


__all__ = [
    "router",
    "resolve_engine",
    "set_agent_provider",
    "verify_api_key",
]