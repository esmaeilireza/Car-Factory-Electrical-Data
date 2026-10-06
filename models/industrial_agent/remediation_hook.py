"""
NEXUS SCADA remediation hook.

Bridge between:

    deterministic agent finding
        -> remediation engine decision/execution
        -> audit trail
        -> Obsidian incident append

Ownership rules:
  - agent.py owns REMEDIATION_HOOK_TRIGGERED.
  - This hook owns REMEDIATION_HOOK_RECEIVED / SKIPPED / COMPLETED / ERROR.
  - remediation/engine.py owns REMEDIATION_DECISION and execution events.
  - This hook must never emit REMEDIATION_DECISION.
  - This hook must never emit REMEDIATION_HOOK_TRIGGERED.
  - This hook must resolve a real equipment id before calling the engine.
  - This hook should only run for active protective HIGH/CRITICAL findings.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_POLICY = ROOT / "configs" / "remediation_policy.json"
DEFAULT_VAULT = ROOT / "data" / "scada_vault"

_KNOWN_EQ_IDS = (
    "STP-01",
    "WLD-01",
    "PNT-01",
    "ASM-01",
    "UTI-01",
    "UTI-02",
)

_EQ_INDEX = {
    "STP-01": 0,
    "WLD-01": 1,
    "PNT-01": 2,
    "ASM-01": 3,
    "UTI-01": 4,
    "UTI-02": 5,
}

_PROTECTIVE_CODE_EXACT = {
    "PROTECTION_TRIP",
    "ANSI_TRIP",
    "TRIP",
    "OVERCURRENT",
    "OVERVOLTAGE",
    "OVERTEMPERATURE",
    "THERMAL_OVERLOAD",
    "INSTANTANEOUS_OVERCURRENT",
    "TIME_OVERCURRENT",
    "DIFFERENTIAL",
    "EARTH_FAULT",
    "GROUND_FAULT",
}

_PROTECTIVE_CODE_PREFIXES = (
    "PROTECTION_TRIP",
    "ANSI_TRIP",
    "TRIP",
    "OVERCURRENT",
    "OVERVOLTAGE",
    "OVERTEMPERATURE",
    "THERMAL_OVERLOAD",
    "INSTANTANEOUS_OVERCURRENT",
    "TIME_OVERCURRENT",
    "DIFFERENTIAL",
    "EARTH_FAULT",
    "GROUND_FAULT",
)

_engine_lock = threading.RLock()
_engine: Optional[Any] = None

_alarm_sink: Optional[Any] = None
_obsidian_handle: Optional[Any] = None

# Lazy-loaded engine class.
RemediationEngine: Optional[Any] = None


# ---------------------------------------------------------------------------
# Backend wiring hooks
# ---------------------------------------------------------------------------

def set_alarm_sink(db_handle: Any) -> None:
    """
    Register the backend DB handle.

    Kept for compatibility with backend/api_legacy.py startup wiring.
    The agent now persists HIGH/CRITICAL alarm events directly, but this
    setter remains harmless and may be used by future engine paths.
    """
    global _alarm_sink
    _alarm_sink = db_handle


def set_obsidian_handle(handle: Any) -> None:
    """
    Register the Obsidian bridge module.

    The remediation engine uses this to append:
        ## Autonomous Remediation
    to the latest incident note for the equipment.
    """
    global _obsidian_handle
    _obsidian_handle = handle


# ---------------------------------------------------------------------------
# Small normalization helpers
# ---------------------------------------------------------------------------

def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(str(value).strip())
        except Exception:
            return default


def _normalize_eq(value: Any) -> str:
    text = _enum_text(value).upper()
    text = text.strip("[](){}'\"<>")
    text = text.strip(" \t\r\n.,;:!?")

    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]

    return text.strip()


def _is_real_eq(text: str) -> bool:
    if not text:
        return False

    bad = {
        "",
        "SYSTEM",
        "ALL",
        "EVERYTHING",
        "PLANT",
        "NONE",
        "NULL",
        "UNKNOWN",
        "NA",
        "N/A",
    }

    return text.upper() not in bad


def _normalize_severity(value: Any) -> str:
    text = _enum_text(value).upper()

    if text.startswith("SEVERITY."):
        text = text.split(".", 1)[1]

    text = text.strip("'\"<>")

    if text in {"CRIT", "CRITICAL"}:
        return "CRITICAL"
    if text in {"HIGH", "URGENT"}:
        return "HIGH"
    if text in {"MED", "MEDIUM", "WARNING"}:
        return "MEDIUM"
    if text in {"LOW", "INFO", "INFORMATIONAL"}:
        return "LOW"

    return text


def _get_attr_or_key(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)


def _first(mapping: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _flatten(finding: Any) -> Dict[str, Any]:
    """
    Convert Finding/dataclass/dict/object into a plain dict.
    """
    if finding is None:
        return {}

    if isinstance(finding, dict):
        return dict(finding)

    if dataclasses.is_dataclass(finding) and not isinstance(finding, type):
        try:
            return dataclasses.asdict(finding)
        except Exception:
            pass

    try:
        return dict(vars(finding))
    except Exception:
        return {"value": str(finding)}


def _evidence(d: Dict[str, Any]) -> Dict[str, Any]:
    ev = d.get("evidence") or {}
    if not isinstance(ev, dict):
        ev = {}
    return ev


# ---------------------------------------------------------------------------
# Attribution helpers
# ---------------------------------------------------------------------------

def _resolve_eq_id(d: Dict[str, Any]) -> str:
    ev = _evidence(d)

    candidates = (
        d.get("eq_id"),
        d.get("equipment_id"),
        d.get("device_id"),
        d.get("machine_id"),
        d.get("asset_id"),
        ev.get("primary_eq_id"),
        ev.get("target_eq_id"),
        ev.get("device_id"),
        ev.get("eq_id"),
        ev.get("equipment_id"),
    )

    for value in candidates:
        eq = _normalize_eq(value)
        if _is_real_eq(eq):
            return eq

    # Last fallback: mine known equipment ids from text.
    haystack = " ".join(
        str(d.get(k, "") or "")
        for k in ("message", "diagnosis", "code", "alarm_type")
    ).upper()

    for eq in _KNOWN_EQ_IDS:
        if eq in haystack:
            return eq

    return ""


def _eq_index(d: Dict[str, Any], eq_id: str) -> int:
    ev = _evidence(d)

    for key in ("eq_index", "index", "equipment_index", "device_index"):
        value = d.get(key)
        if value is None:
            value = ev.get(key)

        if value is not None:
            idx = _safe_int(value, -1)
            if idx >= 0:
                return idx

    return _EQ_INDEX.get(eq_id, -1)


def _ansi_code(d: Dict[str, Any]) -> str:
    ev = _evidence(d)

    for key in (
        "ansi_code",
        "ansi",
        "device_number",
        "relay_code",
        "protection_code",
    ):
        text = _enum_text(d.get(key) or ev.get(key))
        if text:
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                return digits

    blob = " ".join(
        str(d.get(k, "") or "")
        for k in ("code", "alarm_type", "iec_reference", "message", "diagnosis")
    )

    m = re.search(r"ANSI[-_ ]?(\d{2,3})", blob, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r"(?<!\d)(\d{2})(?!\d)", blob)
    if m:
        return m.group(1)

    return ""


def _severity(d: Dict[str, Any]) -> str:
    ev = _evidence(d)
    return _normalize_severity(
        d.get("severity")
        or ev.get("severity")
        or "HIGH"
    )


def _auto_allowed(d: Dict[str, Any]) -> Optional[bool]:
    ev = _evidence(d)

    for source in (d, ev):
        for key in ("auto_allowed", "autonomous_allowed", "allow_auto"):
            value = source.get(key)
            if value is None:
                continue

            if isinstance(value, bool):
                return value

            text = str(value).strip().lower()

            if text in {"true", "1", "yes", "y"}:
                return True
            if text in {"false", "0", "no", "n"}:
                return False

    return None


def _llm_recommendation(agent: Any, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ev = _evidence(d)

    for key in ("remediation_recommendation", "recommendation", "llm_recommendation"):
        value = d.get(key) or ev.get(key)
        if isinstance(value, dict):
            return value

    last = getattr(agent, "last_llm_result", None)

    if isinstance(last, dict):
        primary = _normalize_eq(
            last.get("primary_eq_id")
            or last.get("target_eq_id")
            or last.get("device_id")
            or ""
        )

        eq = _resolve_eq_id(d)

        if not eq or not primary or primary == eq:
            rec = (
                last.get("remediation_recommendation")
                or last.get("recommendation")
            )
            if isinstance(rec, dict):
                return rec

    return None


def _is_protective_code(code: Any) -> bool:
    text = _enum_text(code).upper()

    if not text:
        return False

    if text in _PROTECTIVE_CODE_EXACT:
        return True

    return text.startswith(_PROTECTIVE_CODE_PREFIXES)


def _is_protective_high(d: Dict[str, Any]) -> bool:
    """
    Double guard.

    The agent should already only call this hook for active protective
    HIGH/CRITICAL findings. This guard prevents weak ANSI_ALARM_5/MEDIUM
    findings from reaching the remediation engine if some other path calls
    the hook directly.
    """
    ev = _evidence(d)

    sev = _normalize_severity(d.get("severity") or ev.get("severity"))

    if sev not in {"HIGH", "CRITICAL"}:
        return False

    code = d.get("code") or d.get("alarm_type") or ev.get("code") or ""

    trip_word = _safe_int(
        ev.get("trip_word")
        or d.get("trip_word")
        or 0,
        0,
    )

    lockout = bool(
        ev.get("lockout_status")
        or ev.get("lockout")
        or d.get("lockout_status")
        or d.get("lockout")
    )

    motor_state = _safe_int(
        ev.get("motor_state")
        or d.get("motor_state")
        or 0,
        0,
    )

    if motor_state == 3:
        lockout = True

    return _is_protective_code(code) or trip_word > 0 or lockout


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _get_audit(agent: Any) -> Any:
    audit = getattr(agent, "audit", None)

    if audit is not None:
        return audit

    try:
        from .audit import AuditLogger
        audit = AuditLogger()
        setattr(agent, "audit", audit)
        return audit
    except Exception as exc:
        logger.warning("remediation_hook: unable to create fallback AuditLogger: %s", exc)
        return None


def _audit(agent: Any, event: str, payload: Dict[str, Any]) -> None:
    """
    Write a remediation hook audit event.

    This function must never emit REMEDIATION_HOOK_TRIGGERED.
    """
    if event == "REMEDIATION_HOOK_TRIGGERED":
        # Defensive guard: ownership belongs to agent.py.
        event = "REMEDIATION_HOOK_RECEIVED"

    au = _get_audit(agent)

    if au is None:
        return

    payload = payload if isinstance(payload, dict) else {"value": payload}
    payload = dict(payload)

    eq_id = _resolve_eq_id(payload)
    if eq_id:
        payload.setdefault("eq_id", eq_id)

    attempts = (
        lambda: au.log(event, payload),
        lambda: au.log(event=event, payload=payload),
        lambda: au.write(event, payload),
        lambda: au.write({"event": event, **payload}),
    )

    last_exc: Optional[Exception] = None

    for attempt in attempts:
        try:
            attempt()
            return
        except Exception as exc:
            last_exc = exc

    logger.warning(
        "remediation_hook: audit write failed event=%s eq_id=%s error=%s",
        event,
        payload.get("eq_id"),
        last_exc,
        exc_info=True,
    )


# ---------------------------------------------------------------------------
# Obsidian / engine dependency helpers
# ---------------------------------------------------------------------------

def _obsidian_module() -> Any:
    if _obsidian_handle is not None:
        return _obsidian_handle

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


def _eq_ids(agent: Any) -> List[str]:
    windows = getattr(agent, "windows", None)

    if isinstance(windows, dict):
        ids = [
            _normalize_eq(k)
            for k in windows.keys()
            if _is_real_eq(_normalize_eq(k))
        ]
        if ids:
            return ids

    return list(_KNOWN_EQ_IDS)


def _modbus_client(agent: Any) -> Any:
    for attr in (
        "modbus_client",
        "client",
        "modbus_adapter",
        "plc_client",
        "adapter",
        "command_client",
        "plc",
        "modbus",
        "scada_client",
        "data_client",
    ):
        value = getattr(agent, attr, None)
        if value is not None:
            return value

    return None


def _engine_class() -> Optional[Any]:
    """
    Resolve RemediationEngine lazily and defensively.
    """
    global RemediationEngine

    if RemediationEngine is not None:
        return RemediationEngine

    try:
        from .remediation.engine import RemediationEngine as cls  # type: ignore
        RemediationEngine = cls
        return cls
    except Exception:
        pass

    try:
        from models.industrial_agent.remediation.engine import RemediationEngine as cls  # type: ignore
        RemediationEngine = cls
        return cls
    except Exception:
        pass

    for name in (
        "models.industrial_agent.remediation.engine",
        "industrial_agent.remediation.engine",
    ):
        try:
            mod = importlib.import_module(name)
            cls = getattr(mod, "RemediationEngine", None)
            if cls is not None:
                RemediationEngine = cls
                return cls
        except Exception:
            continue

    return None


def _filter_constructor_kwargs(cls: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Filter constructor kwargs to the signature when possible.
    """
    try:
        sig = inspect.signature(cls)
        params = sig.parameters

        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )

        if has_var_kw:
            return dict(kwargs)

        return {
            k: v
            for k, v in kwargs.items()
            if k in params
            and params[k].kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        }

    except Exception:
        return dict(kwargs)


def _attach_engine_deps(
    engine: Any,
    agent: Any,
    audit: Any,
    obsidian: Any,
) -> None:
    """
    Attach runtime dependencies to an already-constructed engine.
    """
    if engine is None or agent is None:
        return

    pairs = (
        ("agent", agent),
        ("audit", audit),
        ("audit_logger", audit),
        ("obsidian", obsidian),
        ("obsidian_bridge", obsidian),
        ("db", getattr(agent, "db", None)),
        ("config", getattr(agent, "config", None)),
        ("command_callback", getattr(agent, "command_callback", None)),
        ("vault", DEFAULT_VAULT),
        ("vault_path", DEFAULT_VAULT),
    )

    for attr, value in pairs:
        if value is None:
            continue

        current = getattr(engine, attr, None)

        if current is None:
            try:
                setattr(engine, attr, value)
            except Exception:
                pass


def _construct_engine(agent: Any, audit: Any, obsidian: Any) -> Optional[Any]:
    cls = _engine_class()

    if cls is None:
        logger.warning("remediation_hook: RemediationEngine class unavailable")
        return None

    base_kwargs: Dict[str, Any] = {
        "agent": agent,
        "audit": audit,
        "audit_logger": audit,
        "obsidian": obsidian,
        "obsidian_bridge": obsidian,
        "db": getattr(agent, "db", None),
        "config": getattr(agent, "config", None),
        "command_callback": getattr(agent, "command_callback", None),
        "modbus_client": _modbus_client(agent),
        "equipment_ids": _eq_ids(agent),
        "policy_path": os.environ.get(
            "NEXUS_REMEDIATION_POLICY",
            str(DEFAULT_POLICY),
        ),
        "vault": DEFAULT_VAULT,
        "vault_path": DEFAULT_VAULT,
    }

    filtered = _filter_constructor_kwargs(cls, base_kwargs)

    canonical_keys = (
        "modbus_client",
        "audit_logger",
        "obsidian_bridge",
        "equipment_ids",
        "policy_path",
    )

    canonical = {
        k: filtered[k]
        for k in canonical_keys
        if k in filtered
    }

    attempts = (
        lambda: cls(**filtered),
        lambda: cls(**canonical),
        lambda: cls(agent),
        lambda: cls(),
    )

    last_exc: Optional[Exception] = None

    for attempt in attempts:
        try:
            engine = attempt()
            if engine is not None:
                return engine
        except Exception as exc:
            last_exc = exc

    logger.warning(
        "remediation_hook: unable to construct RemediationEngine: %s",
        last_exc,
        exc_info=last_exc is not None,
    )

    return None


def ensure_engine(agent: Any) -> Optional[Any]:
    """
    Get or create the singleton RemediationEngine attached to the agent.
    """
    global _engine

    if agent is None:
        return None

    existing = getattr(agent, "remediation_engine", None)

    if existing is not None:
        audit = _get_audit(agent)
        obsidian = _obsidian_module()
        _attach_engine_deps(existing, agent, audit, obsidian)
        return existing

    with _engine_lock:
        # Re-check under lock.
        existing = getattr(agent, "remediation_engine", None)

        if existing is not None:
            audit = _get_audit(agent)
            obsidian = _obsidian_module()
            _attach_engine_deps(existing, agent, audit, obsidian)
            return existing

        if _engine is not None:
            audit = _get_audit(agent)
            obsidian = _obsidian_module()
            _attach_engine_deps(_engine, agent, audit, obsidian)
            try:
                setattr(agent, "remediation_engine", _engine)
            except Exception:
                pass
            return _engine

        audit = _get_audit(agent)
        obsidian = _obsidian_module()

        engine = _construct_engine(agent, audit, obsidian)

        if engine is None:
            return None

        _attach_engine_deps(engine, agent, audit, obsidian)

        try:
            setattr(agent, "remediation_engine", engine)
        except Exception:
            pass

        _engine = engine
        return engine


# Public alias for backend/remediation_api.py and legacy api.py.
ensure_remediation_engine = ensure_engine


# ---------------------------------------------------------------------------
# Engine invocation helpers
# ---------------------------------------------------------------------------

def _try_calls(calls: Tuple[Any, ...]) -> Any:
    last_exc: Optional[Exception] = None

    for call in calls:
        try:
            return call()
        except TypeError as exc:
            last_exc = exc
            continue
        except Exception:
            raise

    if last_exc is not None:
        raise last_exc

    raise TypeError("no compatible call signature succeeded")


def _decision_to_dict(dec: Any) -> Dict[str, Any]:
    if dec is None:
        return {}

    if isinstance(dec, dict):
        return dict(dec)

    if dataclasses.is_dataclass(dec) and not isinstance(dec, type):
        try:
            return dataclasses.asdict(dec)
        except Exception:
            pass

    try:
        return dict(vars(dec))
    except Exception:
        return {"value": str(dec)}


def _result_to_dict(res: Any) -> Dict[str, Any]:
    if res is None:
        return {}

    if isinstance(res, dict):
        return dict(res)

    if dataclasses.is_dataclass(res) and not isinstance(res, type):
        try:
            return dataclasses.asdict(res)
        except Exception:
            pass

    try:
        return dict(vars(res))
    except Exception:
        return {"value": str(res)}


def _action_types(dec_dict: Dict[str, Any]) -> List[str]:
    raw = dec_dict.get("approved_actions") or []
    types: List[str] = []

    for item in raw:
        if isinstance(item, dict):
            value = item.get("type") or item.get("action") or item.get("name")
        else:
            value = (
                getattr(item, "type", None)
                or getattr(item, "action", None)
                or getattr(item, "name", None)
                or str(item)
            )

        if value:
            types.append(str(value))

    return types


def _rejected_count(dec_dict: Dict[str, Any]) -> int:
    raw = dec_dict.get("rejected_actions") or []
    return len(raw) if isinstance(raw, list) else 0


def _call_evaluate(
    engine: Any,
    agent: Any,
    entry: Dict[str, Any],
    eq: str,
    idx: int,
    ansi: str,
    sev: str,
    rec: Optional[Dict[str, Any]],
    aa: Optional[bool],
) -> Any:
    fn = (
        getattr(engine, "evaluate", None)
        or getattr(engine, "decide", None)
        or getattr(engine, "make_decision", None)
        or getattr(engine, "generate_decision", None)
        or getattr(engine, "assess", None)
    )

    if not callable(fn):
        raise AttributeError("remediation engine has no evaluate/decide method")

    state = getattr(agent, "state", None)
    mode = getattr(agent, "mode", None)

    calls = (
        lambda: fn(
            eq_id=eq,
            eq_index=idx,
            ansi_code=ansi,
            severity=sev,
            llm_recommendation=rec,
            auto_allowed=aa,
            state=state,
            mode=mode,
        ),
        lambda: fn(
            eq_id=eq,
            eq_index=idx,
            ansi_code=ansi,
            severity=sev,
            llm_recommendation=rec,
            auto_allowed=aa,
        ),
        lambda: fn(
            eq,
            idx,
            ansi,
            sev,
            rec,
            aa,
        ),
        lambda: fn(
            eq_id=eq,
            ansi_code=ansi,
            severity=sev,
            llm_recommendation=rec,
            auto_allowed=aa,
        ),
        lambda: fn(
            eq,
            ansi,
            sev,
            rec,
            aa,
        ),
        lambda: fn(
            eq_id=eq,
            severity=sev,
            llm_recommendation=rec,
            auto_allowed=aa,
        ),
        lambda: fn(entry),
        lambda: fn(eq, entry),
    )

    return _try_calls(calls)


def _call_execute(engine: Any, dec: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
    fn = (
        getattr(engine, "execute", None)
        or getattr(engine, "apply", None)
        or getattr(engine, "run", None)
        or getattr(engine, "perform", None)
        or getattr(engine, "execute_decision", None)
        or getattr(engine, "apply_decision", None)
    )

    if not callable(fn):
        return {
            "executed": False,
            "mode": "limited_autonomous",
            "results": [],
            "reason": "no_execute_method_found",
        }

    calls = (
        lambda: fn(dec),
        lambda: fn(decision=dec),
        lambda: fn(dec, entry),
        lambda: fn(dec, finding=entry),
        lambda: fn(entry),
    )

    try:
        res = _try_calls(calls)
        return _result_to_dict(res)
    except Exception as exc:
        return {
            "executed": False,
            "mode": "limited_autonomous",
            "results": [],
            "reason": f"execute_failed:{type(exc).__name__}:{exc}",
        }


# ---------------------------------------------------------------------------
# Public hook
# ---------------------------------------------------------------------------

def run_remediation_after_finding(
    agent: Any,
    finding: Any,
) -> Optional[Tuple[Any, Any]]:
    """
    Run bounded remediation for a deterministic protective finding.

    This function is called by agent.py after a HIGH/CRITICAL protective
    finding has been logged.

    It must not be used for LLM_DIAGNOSIS findings.
    """
    if agent is None:
        return None

    d = _flatten(finding)

    eq = _resolve_eq_id(d)

    if not eq:
        return None

    # Build a normalized entry with explicit attribution.
    entry = dict(d)
    entry["eq_id"] = eq

    ev = entry.get("evidence") or {}
    if not isinstance(ev, dict):
        ev = {}

    ev.setdefault("primary_eq_id", eq)
    ev.setdefault("target_eq_id", eq)
    ev.setdefault("device_id", eq)
    ev.setdefault("eq_id", eq)

    entry["evidence"] = ev

    sev = _severity(entry)
    code = _enum_text(entry.get("code") or entry.get("alarm_type") or "")
    trip_word = _safe_int(ev.get("trip_word") or entry.get("trip_word") or 0, 0)
    lockout_status = bool(
        ev.get("lockout_status")
        or ev.get("lockout")
        or entry.get("lockout_status")
        or entry.get("lockout")
    )

    # Double guard: only active protective HIGH/CRITICAL findings.
    if not _is_protective_high(entry):
        _audit(
            agent,
            "REMEDIATION_HOOK_SKIPPED",
            {
                "eq_id": eq,
                "code": code,
                "severity": sev,
                "trip_word": trip_word,
                "lockout_status": lockout_status,
                "reason": "not_active_protective_high",
            },
        )
        return None

    _audit(
        agent,
        "REMEDIATION_HOOK_RECEIVED",
        {
            "eq_id": eq,
            "code": code,
            "severity": sev,
            "safety_level": entry.get("safety_level", 3),
            "trip_word": trip_word,
            "lockout_status": lockout_status,
            "primary_eq_id": ev.get("primary_eq_id", eq),
            "target_eq_id": ev.get("target_eq_id", eq),
            "device_id": ev.get("device_id", eq),
        },
    )

    engine = ensure_engine(agent)

    if engine is None:
        _audit(
            agent,
            "REMEDIATION_HOOK_ERROR",
            {
                "eq_id": eq,
                "code": code,
                "severity": sev,
                "trip_word": trip_word,
                "lockout_status": lockout_status,
                "error": "remediation_engine_unavailable",
            },
        )
        return None

    idx = _eq_index(entry, eq)
    ansi = _ansi_code(entry)
    aa = _auto_allowed(entry)
    rec = _llm_recommendation(agent, entry)

    policy = getattr(engine, "policy", None)
    policy_mode = _enum_text(getattr(policy, "mode", "")) or "unknown"

    _audit(
        agent,
        "REMEDIATION_ENGINE_READY",
        {
            "eq_id": eq,
            "eq_index": idx,
            "ansi": ansi,
            "severity": sev,
            "auto_allowed": aa,
            "mode": policy_mode,
            "has_primary_client": getattr(engine, "client", None) is not None,
            "has_obsidian_bridge": (
                getattr(engine, "obsidian", None) is not None
                or getattr(engine, "obsidian_bridge", None) is not None
            ),
            "direct_fallback_enabled": bool(
                getattr(engine, "_direct_client", None) is not None
                or hasattr(engine, "can_command_plc")
            ),
        },
    )

    try:
        dec = _call_evaluate(
            engine,
            agent,
            entry,
            eq,
            idx,
            ansi,
            sev,
            rec,
            aa,
        )

        res = _call_execute(engine, dec, entry)

        dec_dict = _decision_to_dict(dec)
        res_dict = _result_to_dict(res)

        # Force attribution into decision/result payloads.
        if eq:
            dec_dict.setdefault("eq_id", eq)
            res_dict.setdefault("eq_id", eq)

            dec_ev = dec_dict.get("evidence") or {}
            if not isinstance(dec_ev, dict):
                dec_ev = {}

            dec_ev.setdefault("primary_eq_id", eq)
            dec_ev.setdefault("target_eq_id", eq)
            dec_ev.setdefault("device_id", eq)
            dec_ev.setdefault("eq_id", eq)

            dec_dict["evidence"] = dec_ev

        nested_dec = res_dict.get("decision")
        if isinstance(nested_dec, dict):
            nested_dec.setdefault("eq_id", eq)

        # Mutate caller finding if it is a dict, for dashboard/debugging.
        if isinstance(finding, dict):
            try:
                finding["remediation_decision"] = dec_dict
                finding["remediation_result"] = res_dict
            except Exception:
                pass

        _audit(
            agent,
            "REMEDIATION_HOOK_COMPLETED",
            {
                "eq_id": eq,
                "code": code,
                "severity": sev,
                "approved": _action_types(dec_dict),
                "rejected": _rejected_count(dec_dict),
                "executed": bool(res_dict.get("executed", False)),
                "mode": (
                    res_dict.get("mode")
                    or dec_dict.get("mode")
                    or policy_mode
                    or "limited_autonomous"
                ),
            },
        )

        return dec, res

    except Exception as exc:
        _audit(
            agent,
            "REMEDIATION_HOOK_ERROR",
            {
                "eq_id": eq,
                "code": code,
                "severity": sev,
                "trip_word": trip_word,
                "lockout_status": lockout_status,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )

        logger.warning(
            "remediation_hook: failed for eq_id=%s: %s",
            eq,
            exc,
            exc_info=True,
        )

        return None


__all__ = [
    "ensure_engine",
    "ensure_remediation_engine",
    "run_remediation_after_finding",
    "set_alarm_sink",
    "set_obsidian_handle",
]