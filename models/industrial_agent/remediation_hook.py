from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import the engine class directly to avoid circular imports if possible,
# or rely on lazy loading inside ensure_engine if necessary.
# Assuming standard project structure:
try:
    from .remediation import RemediationEngine
except ImportError:
    # Fallback for different path structures
    try:
        from models.industrial_agent.remediation.engine import RemediationEngine
    except ImportError:
        RemediationEngine = None  # Will fail gracefully at runtime if missing


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "configs" / "remediation_policy.json"
DEFAULT_VAULT = ROOT / "data" / "scada_vault"
DEFAULT_EQ_IDS = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]

# Global handles for external dependencies that might be registered later
_alarm_sink: Any = None
_obsidian_handle: Any = None
_engine_instance: Optional[Any] = None  # Cached singleton instance


def set_obsidian_handle(handle) -> None:
    """Register the obsidian bridge so the engine can append the
    Autonomous Remediation section to the latest incident note."""
    global _obsidian_handle
    _obsidian_handle = handle
    
    # If engine already exists, propagate immediately
    if _engine_instance is not None and hasattr(_engine_instance, 'obsidian'):
        if _engine_instance.obsidian is None:
            _engine_instance.obsidian = handle


def set_alarm_sink(db_handle) -> None:
    """Register the backend DB handle so HIGH/CRITICAL findings are persisted
    into the alarm_events table (statistical memory)."""
    global _alarm_sink
    _alarm_sink = db_handle
    
    # If engine already exists, propagate immediately if supported
    if _engine_instance is not None and hasattr(_engine_instance, 'alarm_sink'):
        if getattr(_engine_instance, 'alarm_sink', None) is None:
            _engine_instance.alarm_sink = db_handle


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _audit(a: Any, event: str, payload: Dict[str, Any]) -> None:
    """Safely log an audit event, trying multiple common logger signatures."""
    if a is None:
        return

    attempts = [
        lambda: a.log(event, payload),
        lambda: a.log(event=event, payload=payload),
        lambda: a.log({"event": event, **payload}),
        lambda: a.write(event, payload),
        lambda: a.write({"event": event, **payload}),
    ]

    for fn in attempts:
        try:
            fn()
            return
        except Exception:
            continue


def _get(a: Any, *names: str) -> Any:
    """Get attribute by name, returning first non-None match."""
    for n in names:
        v = getattr(a, n, None)
        if v is not None:
            return v
    return None


def _eq_ids(a: Any) -> List[str]:
    """Extract equipment IDs from agent config or defaults."""
    v = _get(a, "equipment_ids")
    if isinstance(v, list) and v:
        return [str(x) for x in v]

    cfg = getattr(a, "config", None)
    if cfg is not None:
        for n in ("equipment_ids", "eq_ids", "devices", "machines"):
            v = getattr(cfg, n, None)
            if isinstance(v, list) and v:
                return [str(x) for x in v]

    return DEFAULT_EQ_IDS[:]


def _plain(o: Any) -> Dict[str, Any]:
    """Convert object to dict safely."""
    if o is None:
        return {}

    if isinstance(o, dict):
        return dict(o)

    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        try:
            return dataclasses.asdict(o)
        except Exception:
            pass

    if hasattr(o, "to_dict"):
        try:
            v = o.to_dict()
            if isinstance(v, dict):
                return dict(v)
        except Exception:
            pass

    if hasattr(o, "__dict__"):
        return {k: val for k, val in vars(o).items() if not k.startswith("_")}

    return {}


def _flatten(o: Any) -> Dict[str, Any]:
    """Flatten nested dicts/payloads into a single dictionary."""
    d = _plain(o)

    pay = d.get("payload")
    if isinstance(pay, dict):
        m = dict(pay)
        for k, v in d.items():
            if k != "payload":
                m[k] = v
        d = m

    for nk in ("data", "detail", "finding", "diagnosis"):
        n = d.get(nk)
        if isinstance(n, dict):
            m = dict(n)
            for k, v in d.items():
                if k != nk:
                    m[k] = v
            d = m
            break

    return d


def _first(d: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    """Return first non-empty value from keys."""
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _eq_id(d: Dict[str, Any]) -> Optional[str]:
    """Extract equipment ID string."""
    v = _first(
        d,
        (
            "eq_id",
            "equipment_id",
            "device_id",
            "machine_id",
            "node_id",
            "equipment",
            "machine",
            "device",
            "node",
            "tag",
        ),
    )

    if v is None:
        return None

    s = str(v).strip()
    if not s:
        return None

    # Try to extract standard format like XXX-YY
    m = re.search(r"\b([A-Z]{3}-\d{2})\b", s.upper())
    if m:
        return m.group(1)

    return s


def _ansi(d: Dict[str, Any]) -> str:
    """Extract ANSI code."""
    v = _first(
        d,
        (
            "ansi_code",
            "ansi",
            "code",
            "fault_code",
            "relay",
            "protection_code",
            "device_number",
        ),
    )

    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    name = str(ev.get("ansi_name", "") or "")

    for cand in (str(v or ""), name, str(d.get("message", ""))):
        # Look for 2-digit number not surrounded by other digits
        m = re.search(r"(?<!\d)(\d{2})(?!\d)", cand)
        if m:
            return m.group(1)

    return str(v or "")


def _severity(d: Dict[str, Any]) -> str:
    """Extract severity level."""
    v = _first(d, ("severity", "level", "alarm_severity", "priority_level"))
    if v is not None:
        return str(v).upper()

    # Default based on event type
    if str(d.get("event", "")).upper() == "FINDING":
        return "HIGH"
    
    return ""


def _auto_allowed(d: Dict[str, Any]) -> Optional[bool]:
    """Extract auto_allowed flag."""
    v = _first(d, ("auto_allowed", "autonomous_allowed", "allow_auto"))

    if v is None:
        ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
        v = ev.get("auto_allowed")

    if v is None:
        return None

    return bool(v)


def _eq_index(d: Dict[str, Any], eq_id: Optional[str], a: Any) -> Optional[int]:
    """Resolve equipment index."""
    v = _first(d, ("eq_index", "index", "offset", "register_index", "device_index"))
    if v is not None:
        try:
            return int(v)
        except Exception:
            pass

    ids = _eq_ids(a)
    try:
        return ids.index(str(eq_id))
    except Exception:
        return None


def _llm_rec(a: Any, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract LLM recommendation if present."""
    for k in ("remediation_recommendation", "recommendation", "proposed_action"):
        if isinstance(d.get(k), dict):
            return d[k]

    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    if isinstance(ev.get("remediation_recommendation"), dict):
        return ev["remediation_recommendation"]

    for attr in ("last_diagnosis", "last_llm_result", "diagnosis", "llm_result"):
        dd = _plain(_get(a, attr))
        for k in ("remediation_recommendation", "recommendation"):
            if isinstance(dd.get(k), dict):
                return dd[k]

        p = dd.get("payload")
        if isinstance(p, dict):
            for k in ("remediation_recommendation", "recommendation"):
                if isinstance(p.get(k), dict):
                    return p[k]

    return None


# ---------------------------------------------------------------------------
# Engine Management
# ---------------------------------------------------------------------------

def ensure_engine(a: Any) -> RemediationEngine:
    """
    Get or create the singleton RemediationEngine.
    
    CRITICAL FIX:
    If the engine already exists, we must check if _obsidian_handle or 
    _alarm_sink were registered AFTER the engine was created. If so, 
    propagate them now before returning. This fixes the race condition 
    where hooks fire before the main app registers bridges.
    """
    global _engine_instance, _obsidian_handle, _alarm_sink

    # Case 1: Engine already exists
    if _engine_instance is not None:
        # Propagate Obsidian handle if missing
        if _obsidian_handle is not None and hasattr(_engine_instance, 'obsidian'):
            if getattr(_engine_instance, 'obsidian', None) is None:
                _engine_instance.obsidian = _obsidian_handle
        
        # Propagate Alarm sink if missing (if supported by engine)
        if _alarm_sink is not None and hasattr(_engine_instance, 'alarm_sink'):
            if getattr(_engine_instance, 'alarm_sink', None) is None:
                _engine_instance.alarm_sink = _alarm_sink
                
        return _engine_instance

    # Case 2: Create new engine
    if RemediationEngine is None:
        raise RuntimeError("RemediationEngine class not available.")

    e = RemediationEngine(
        modbus_client=_get(
            a,
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
        ),
        audit_logger=_get(a, "audit", "audit_logger", "logger", "agent_audit"),
        obsidian_bridge=_obsidian_handle,  # Pass current global handle
        equipment_ids=_eq_ids(a),
        policy_path=os.environ.get("NEXUS_REMEDIATION_POLICY", str(DEFAULT_POLICY)),
        vault_path=DEFAULT_VAULT,
    )

    # Apply mode from env if specified
    mode = os.environ.get("NEXUS_REMEDIATION_MODE")
    if mode and mode in e.policy.raw.get("supported_modes", []):
        e.set_mode(mode)

    # Cache the instance
    _engine_instance = e
    
    # Ensure any late-registered handles are attached just in case
    if _obsidian_handle is not None:
        e.obsidian = _obsidian_handle
    if _alarm_sink is not None and hasattr(e, 'alarm_sink'):
        e.alarm_sink = _alarm_sink

    return e


# ---------------------------------------------------------------------------
# Main Hook Entry Point
# ---------------------------------------------------------------------------

def run_remediation_after_finding(a: Any, finding: Any) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """
    Execute the remediation chain after a finding is generated.
    
    Args:
        a: The Agent instance (contains config, clients, loggers).
        finding: The Finding object/dict generated by Rule Engine or LLM.
        
    Returns:
        Tuple of (RemediationDecision, ExecutionResult) if successful, else None.
    """
    au = _get(a, "audit", "audit_logger", "logger", "agent_audit")

    try:
        # 1. Flatten and Extract Metadata
        d = _flatten(finding)

        _audit(
            au,
            "REMEDIATION_HOOK_TRIGGERED",
            {
                "raw_type": type(finding).__name__,
                "keys": sorted(d.keys())[:30],
            },
        )

        eq = _eq_id(d)
        if not eq:
            _audit(
                au,
                "REMEDIATION_HOOK_SKIPPED",
                {"reason": "missing_eq_id", "keys": sorted(d.keys())[:20]},
            )
            return None

        idx = _eq_index(d, eq, a)
        if idx is None:
            _audit(
                au,
                "REMEDIATION_HOOK_SKIPPED",
                {"reason": "missing_eq_index", "eq_id": eq},
            )
            return None

        ansi = _ansi(d)
        sev = _severity(d) or "HIGH"
        aa = _auto_allowed(d)
        rec = _llm_rec(a, d)

        # 2. Ensure Engine is Ready and Wired
        eng = ensure_engine(a)

        _audit(
            au,
            "REMEDIATION_ENGINE_READY",
            {
                "eq_id": eq,
                "eq_index": idx,
                "ansi": ansi,
                "severity": sev,
                "auto_allowed": aa,
                "mode": eng.policy.mode,
                "has_primary_client": eng.client is not None,
                "has_obsidian_bridge": eng.obsidian is not None,
                "direct_fallback_enabled": getattr(eng, "_direct_enabled", lambda: False)(),
            },
        )

        # 3. Evaluate Decision
        dec = eng.evaluate(
            eq_id=eq,
            eq_index=idx,
            ansi_code=ansi,
            severity=sev,
            llm_recommendation=rec,
            auto_allowed=aa,
        )

        # 4. Execute Action
        res = eng.execute(dec)

        # 5. Attach Results back to Finding if mutable
        if isinstance(finding, dict):
            try:
                finding["remediation_decision"] = dataclasses.asdict(dec)
                finding["remediation_result"] = res
            except Exception:
                pass

        _audit(
            au,
            "REMEDIATION_HOOK_COMPLETED",
            {
                "eq_id": eq,
                "approved": [x.type for x in dec.approved_actions],
                "rejected": len(dec.rejected_actions),
                "executed": bool(res.get("executed")),
            },
        )

        return dec, res

    except Exception as e:
        _audit(
            au,
            "REMEDIATION_HOOK_ERROR",
            {"error": str(e), "error_type": type(e).__name__},
        )
        return None