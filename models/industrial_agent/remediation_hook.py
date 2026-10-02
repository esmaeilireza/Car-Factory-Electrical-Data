from __future__ import annotations
import dataclasses, os, re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .remediation import RemediationEngine

# --- alarm-events sink (wired once by backend/api.py) ----------------------
_alarm_sink = None


_obsidian_handle = None


def set_obsidian_handle(handle) -> None:
    """Register the obsidian bridge so the engine can append the
    Autonomous Remediation section to the latest incident note."""
    global _obsidian_handle
    _obsidian_handle = handle


def set_alarm_sink(db_handle) -> None:
    """Register the backend DB handle so HIGH/CRITICAL findings are persisted
    into the alarm_events table (statistical memory)."""
    global _alarm_sink
    _alarm_sink = db_handle

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "configs" / "remediation_policy.json"
DEFAULT_EQ_IDS = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]


def _audit(a, event, payload):
    if a is None: return
    for fn in (lambda: a.log(event, payload), lambda: a.log(event=event, payload=payload),
               lambda: a.log({"event": event, **payload}), lambda: a.write(event, payload)):
        try: fn(); return
        except Exception: continue


def _get(a, *names):
    for n in names:
        v = getattr(a, n, None)
        if v is not None: return v
    return None


def _eq_ids(a) -> List[str]:
    v = _get(a, "equipment_ids")
    if isinstance(v, list) and v: return [str(x) for x in v]
    cfg = getattr(a, "config", None)
    if cfg is not None:
        for n in ("equipment_ids", "eq_ids", "devices", "machines"):
            v = getattr(cfg, n, None)
            if isinstance(v, list) and v: return [str(x) for x in v]
    return DEFAULT_EQ_IDS[:]


def _plain(o):
    if o is None: return {}
    if isinstance(o, dict): return dict(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        try: return dataclasses.asdict(o)
        except Exception: pass
    if hasattr(o, "to_dict"):
        try:
            v = o.to_dict()
            if isinstance(v, dict): return dict(v)
        except Exception: pass
    if hasattr(o, "__dict__"):
        return {k: val for k, val in vars(o).items() if not k.startswith("_")}
    return {}


def _flatten(o):
    d = _plain(o)
    pay = d.get("payload")
    if isinstance(pay, dict):
        m = dict(pay)
        for k, v in d.items():
            if k != "payload": m[k] = v
        d = m
    for nk in ("data", "detail", "finding", "diagnosis"):
        n = d.get(nk)
        if isinstance(n, dict):
            m = dict(n)
            for k, v in d.items():
                if k != nk: m[k] = v
            d = m; break
    return d


def _first(d, keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""): return d[k]
    return default


def _eq_id(d):
    v = _first(d, ("eq_id","equipment_id","device_id","machine_id","node_id","equipment","machine","device","node","tag"))
    if v is None: return None
    s = str(v).strip()
    m = re.search(r"\b([A-Z]{3}-\d{2})\b", s.upper())
    return m.group(1) if m else (s or None)


def _ansi(d):
    # 1) explicit numeric-ish code
    v = _first(d, ("ansi_code","ansi","code","fault_code","relay","protection_code","device_number"))
    # 2) evidence.ansi_name like "38 Over-Temperature"
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    name = str(ev.get("ansi_name", "") or "")
    for cand in (str(v or ""), name, str(d.get("message",""))):
        m = re.search(r"(?<!\d)(\d{2})(?!\d)", cand)
        if m: return m.group(1)
    return str(v or "")


def _severity(d):
    v = _first(d, ("severity","level","alarm_severity","priority_level"))
    if v is not None: return str(v).upper()
    return "HIGH" if str(d.get("event","")).upper() == "FINDING" else ""


def _auto_allowed(d):
    v = _first(d, ("auto_allowed","autonomous_allowed","allow_auto"))
    if v is None:
        ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
        v = ev.get("auto_allowed")
    if v is None: return None
    return bool(v)


def _eq_index(d, eq_id, a):
    v = _first(d, ("eq_index","index","offset","register_index","device_index"))
    if v is not None:
        try: return int(v)
        except Exception: pass
    ids = _eq_ids(a)
    try: return ids.index(str(eq_id))
    except Exception: return None


def _llm_rec(a, d):
    for k in ("remediation_recommendation","recommendation","proposed_action"):
        if isinstance(d.get(k), dict): return d[k]
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    if isinstance(ev.get("remediation_recommendation"), dict): return ev["remediation_recommendation"]
    for attr in ("last_diagnosis","last_llm_result","diagnosis","llm_result"):
        dd = _plain(_get(a, attr))
        for k in ("remediation_recommendation","recommendation"):
            if isinstance(dd.get(k), dict): return dd[k]
        p = dd.get("payload")
        if isinstance(p, dict):
            for k in ("remediation_recommendation","recommendation"):
                if isinstance(p.get(k), dict): return p[k]
    return None


def ensure_engine(a) -> RemediationEngine:
    e = getattr(a, "remediation_engine", None)
    if isinstance(e, RemediationEngine): return e
    e = RemediationEngine(
        modbus_client=_get(a, "modbus_client","client","modbus_adapter","plc_client","adapter","command_client","plc"),
        audit_logger=_get(a, "audit","audit_logger","logger","agent_audit"),
        obsidian_bridge=_get(a, "obsidian_bridge","vault_bridge","obsidian","knowledge_bridge"),
        equipment_ids=_eq_ids(a),
        policy_path=os.environ.get("NEXUS_REMEDIATION_POLICY", str(DEFAULT_POLICY)))
    mode = os.environ.get("NEXUS_REMEDIATION_MODE")
    if mode and mode in e.policy.raw.get("supported_modes", []): e.set_mode(mode)
    setattr(a, "remediation_engine", e)
    if _obsidian_handle is not None:
        try:
            e.obsidian = _obsidian_handle  # OBSIDIAN-HANDLE-WIRING
        except Exception as _obs_err:
            _audit(au, "OBSIDIAN_HANDLE_WIRING_FAILED", {"error": str(_obs_err)})
    return e


def run_remediation_after_finding(a, finding) -> Optional[Tuple[Any, Dict[str, Any]]]:
    au = _get(a, "audit","audit_logger","logger","agent_audit")
    try:
        d = _flatten(finding)
        _audit(au, "REMEDIATION_HOOK_TRIGGERED",
               {"raw_type": type(finding).__name__, "keys": sorted(d.keys())[:30]})
        eq = _eq_id(d)
        if not eq:
            _audit(au, "REMEDIATION_HOOK_SKIPPED", {"reason":"missing_eq_id","keys":sorted(d.keys())[:20]}); return None
        idx = _eq_index(d, eq, a)
        if idx is None:
            _audit(au, "REMEDIATION_HOOK_SKIPPED", {"reason":"missing_eq_index","eq_id":eq}); return None
        ansi, sev, aa, rec = _ansi(d), _severity(d) or "HIGH", _auto_allowed(d), _llm_rec(a, d)
        if _alarm_sink is not None and sev.upper() in ("HIGH", "CRITICAL"):
            try:
                _alarm_sink.save_alarm_event(
                    eq, str(d.get("code", "FINDING")), ansi,
                    str(d.get("message", ""))[:250], sev,
                )
                _audit(au, "ALARM_EVENT_RECORDED", {"eq_id": eq, "severity": sev})
            except Exception as _db_err:
                _audit(au, "ALARM_EVENT_RECORD_FAILED", {"error": str(_db_err)})
        eng = ensure_engine(a)
        _audit(au, "REMEDIATION_ENGINE_READY",
               {"eq_id":eq,"eq_index":idx,"ansi":ansi,"severity":sev,"auto_allowed":aa,
                "mode":eng.policy.mode,"has_client":eng.client is not None})
        dec = eng.evaluate(eq_id=eq, eq_index=idx, ansi_code=ansi, severity=sev,
                           llm_recommendation=rec, auto_allowed=aa)
        res = eng.execute(dec)
        if isinstance(finding, dict):
            try:
                finding["remediation_decision"] = dataclasses.asdict(dec)
                finding["remediation_result"] = res
            except Exception: pass
        _audit(au, "REMEDIATION_HOOK_COMPLETED",
               {"eq_id":eq,"approved":[x.type for x in dec.approved_actions],
                "rejected":len(dec.rejected_actions),"executed":bool(res.get("executed"))})
        return dec, res
    except Exception as e:
        _audit(au, "REMEDIATION_HOOK_ERROR", {"error":str(e),"error_type":type(e).__name__})
        return None
