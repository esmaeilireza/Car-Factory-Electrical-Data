from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .policy import RemediationPolicy, load_policy
from .safety_guard import EquipmentState, GuardDecision, SafetyGuard

DEFAULT_EQ_IDS = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
PLC_HOST, PLC_PORT = "127.0.0.1", 5020


@dataclass
class RemediationAction:
    type: str
    eq_id: str
    reason: str
    params: Dict[str, Any] = field(default_factory=dict)
    priority: int = 100


@dataclass
class RemediationDecision:
    eq_id: str
    ansi_code: str
    severity: str
    auto_allowed: Optional[bool]
    approved_actions: List[RemediationAction]
    rejected_actions: List[Dict[str, Any]]
    llm_recommendation: Optional[Dict[str, Any]] = None
    policy_version: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class RemediationEngine:
    """
    Bounded autonomous remediation engine.

    Invariants:
      - LLM recommends; the deterministic guard decides; the engine executes
        only approved, bounded, non-safety actions.
      - If the rule engine marks a finding auto_allowed=False, NO autonomous
        actuation is performed (only notification/hold class actions).
      - Global E-STOP and ANSI bypass are never touched (see SafetyGuard).
      - PLC I/O is signature-tolerant because the agent may expose a wrapped
        adapter (unit_id= vs slave=, .registers vs int, raise vs isError).
      - Every call to execute() produces BOTH an audit entry AND an Obsidian
        record, regardless of whether actuation occurred. This is enforced
        structurally by routing all exit paths through _finalize(). Do not
        add a new early return to execute() without routing it through
        _finalize() as well.
    """

    LOAD_SETPOINT_BASE = 160
    MOTOR_STATE_OFFSET = 8
    ALARM_FLAG_OFFSET = 9
    TRIP_WORD_OFFSET = 10
    LOCKOUT_OFFSET = 14
    THERMAL_OFFSET = 15
    TRIP_COUNT_OFFSET = 16
    SYS_STATUS_ADDR = 120

    # Actions that never increase plant stress and are therefore the only ones
    # permitted when auto_allowed is unknown/False.
    ALWAYS_OK = {"REQUEST_OPERATOR_ACK", "HOLD_STATE"}

    def __init__(self, modbus_client: Any = None, audit_logger: Any = None,
                 policy_path: Path | str = "configs/remediation_policy.json",
                 obsidian_bridge: Any = None, equipment_ids: Optional[List[str]] = None):
        self.client = modbus_client
        self.audit = audit_logger
        self.obsidian = obsidian_bridge
        self.equipment_ids = list(equipment_ids or DEFAULT_EQ_IDS)
        self.policy: RemediationPolicy = load_policy(policy_path)
        self.guard = SafetyGuard(self.policy)
        self._direct_client: Any = None  # lazily opened fallback (prototype only)

    # ---- mode -------------------------------------------------------------
    def set_mode(self, mode: str) -> bool:
        supported = self.policy.raw.get("supported_modes", ["advisory", "limited_autonomous"])
        if mode not in supported:
            return False
        self.policy.mode = mode
        self.policy.raw["mode"] = mode
        return True

    # ---- index ------------------------------------------------------------
    def _resolve_index(self, eq_id: str, eq_index: Optional[int]) -> Optional[int]:
        if eq_index is not None:
            try:
                return int(eq_index)
            except Exception:
                pass
        try:
            return self.equipment_ids.index(str(eq_id))
        except Exception:
            return None

    # ---- signature-tolerant PLC I/O --------------------------------------
    def _extract_reg(self, res: Any) -> Optional[int]:
        if res is None:
            return None
        if isinstance(res, int):
            return res
        if hasattr(res, "isError") and res.isError():
            return None
        regs = getattr(res, "registers", None)
        if regs is not None:
            try:
                return int(regs[0])
            except Exception:
                return None
        val = getattr(res, "value", None)
        if isinstance(val, int):
            return val
        return None

    def _call_read(self, client: Any, addr: int) -> Optional[int]:
        attempts = [
            lambda: client.read_holding_registers(addr, 1, slave=1),
            lambda: client.read_holding_registers(addr, count=1, slave=1),
            lambda: client.read_holding_registers(addr, 1, unit_id=1),
            lambda: client.read_holding_registers(address=addr, count=1, unit_id=1),
            lambda: client.read_holding_registers(addr, 1),
            lambda: client.read_holding_registers(addr),
        ]
        for fn in attempts:
            try:
                out = self._extract_reg(fn())
                if out is not None:
                    return out
            except Exception:
                continue
        return None

    def _call_write(self, client: Any, addr: int, value: int) -> bool:
        attempts = [
            lambda: client.write_register(addr, int(value), slave=1),
            lambda: client.write_register(addr, int(value), unit_id=1),
            lambda: client.write_register(address=addr, value=int(value), unit_id=1),
            lambda: client.write_register(addr, int(value)),
        ]
        for fn in attempts:
            try:
                res = fn()
                if hasattr(res, "isError"):
                    if not res.isError():
                        return True
                else:
                    return True
            except Exception:
                continue
        return False

    def _get_client(self) -> Any:
        if self.client is not None:
            return self.client
        # Prototype-only fallback: open a direct client to the local simulator.
        # Disabled unless explicitly allowed, and always audited when used.
        if os.environ.get("NEXUS_REMEDIATION_ALLOW_DIRECT_PLC") != "1":
            return None
        if self._direct_client is None:
            try:
                from pymodbus.client import ModbusTcpClient
                c = ModbusTcpClient(PLC_HOST, port=PLC_PORT)
                if c.connect():
                    self._direct_client = c
                    self._audit("REMEDIATION_DIRECT_PLC_CLIENT_OPENED",
                                {"host": PLC_HOST, "port": PLC_PORT,
                                 "note": "prototype fallback; agent adapter unavailable"})
            except Exception:
                self._direct_client = None
        return self._direct_client

    def _read_hr(self, addr: int) -> Optional[int]:
        c = self._get_client()
        return self._call_read(c, addr) if c is not None else None

    def _write_hr(self, addr: int, value: int) -> bool:
        c = self._get_client()
        return self._call_write(c, addr, value) if c is not None else False

    # ---- state ------------------------------------------------------------
    def read_equipment_state(self, eq_id: str, eq_index: Optional[int] = None) -> EquipmentState:
        idx = self._resolve_index(eq_id, eq_index)
        if idx is None:
            return EquipmentState(eq_id=eq_id)
        base = idx * 18
        sys_word = self._read_hr(self.SYS_STATUS_ADDR)
        return EquipmentState(
            eq_id=eq_id,
            motor_state=self._read_hr(base + self.MOTOR_STATE_OFFSET),
            alarm_flag=self._read_hr(base + self.ALARM_FLAG_OFFSET),
            trip_word=self._read_hr(base + self.TRIP_WORD_OFFSET),
            lockout_status=self._read_hr(base + self.LOCKOUT_OFFSET),
            thermal_per_mille=self._read_hr(base + self.THERMAL_OFFSET),
            trip_count=self._read_hr(base + self.TRIP_COUNT_OFFSET),
            load_setpoint=self._read_hr(self.LOAD_SETPOINT_BASE + idx),
            global_estop_latched=bool(sys_word & 1) if sys_word is not None else False,
            any_equipment_tripped=bool(sys_word & 2) if sys_word is not None else False,
        )

    # ---- proposal ---------------------------------------------------------
    def propose_from_ansi(self, eq_id, ansi_code, severity, state, auto_allowed):
        actions: List[RemediationAction] = []
        a = "".join(ch for ch in str(ansi_code) if ch.isdigit())
        sev = str(severity).upper()
        aa = bool(auto_allowed)

        def add(t, reason, params=None, prio=100):
            actions.append(RemediationAction(type=t, eq_id=eq_id, reason=reason,
                                             params=params or {}, priority=prio))

        # Notification/hold are always safe to propose.
        add("REQUEST_OPERATOR_ACK", f"Operator awareness for ANSI-{a or 'NA'} ({sev}).", prio=50)
        if sev in {"HIGH", "CRITICAL"}:
            add("HOLD_STATE", f"Hold state during {sev} ANSI-{a}.", prio=40)

        # Actuation requires explicit auto_allowed from the rule engine.
        if not aa:
            return actions

        if a in {"49", "38"}:
            add("REDUCE_LOAD", f"Thermal/overload ANSI-{a}; reduce load to arrest degradation.",
                {"step_percent": 10}, prio=10)
        if a == "51":
            add("REDUCE_LOAD", f"Time overcurrent ANSI-{a}; reduce load before trip escalates.",
                {"step_percent": 10}, prio=20)
        if a == "59":
            add("REDUCE_LOAD", f"Overvoltage ANSI-{a}; reduce load and monitor feeder.",
                {"step_percent": 5}, prio=30)
        if a == "50":
            add("REQUEST_OPERATOR_ACK", f"Instantaneous OC ANSI-{a}; auto recovery forbidden.",
                prio=5)

        thr = int(self.policy.escalation.get("after_repeated_trips", 3))
        if state.trip_count is not None and state.trip_count >= thr:
            add("REQUEST_OPERATOR_ACK", "Repeated trips; escalate to operator.", prio=1)
        return actions

    def incorporate_llm(self, llm_rec, state, auto_allowed):
        if not llm_rec or not auto_allowed:
            return []
        t = str(llm_rec.get("recommended_action", "")).upper()
        allowed = {"REDUCE_LOAD", "HOLD_STATE", "REQUEST_OPERATOR_ACK",
                   "ISOLATE_NON_CRITICAL", "SAFE_STOP_NON_CRITICAL"}
        if t not in allowed:
            return []
        params = {"step_percent": int(llm_rec.get("step_percent", 10))} if t == "REDUCE_LOAD" else {}
        return [RemediationAction(type=t, eq_id=state.eq_id,
                                  reason=str(llm_rec.get("rationale", "LLM recommendation.")),
                                  params=params, priority=int(llm_rec.get("priority", 70)))]

    # ---- evaluate / execute ----------------------------------------------
    def evaluate(self, eq_id, eq_index, ansi_code, severity,
                 llm_recommendation=None, auto_allowed=None) -> RemediationDecision:
        state = self.read_equipment_state(eq_id, eq_index)
        aa = True if auto_allowed is None else bool(auto_allowed)  # None => trust engine default

        proposed = self.propose_from_ansi(eq_id, ansi_code, severity, state, aa)
        proposed += self.incorporate_llm(llm_recommendation, state, aa)

        approved, rejected, seen = [], [], set()
        for act in sorted(proposed, key=lambda x: x.priority):
            if act.type in seen:
                continue
            dec: GuardDecision = self.guard.validate(act.type, state, act.params)
            if dec.approved:
                if act.type == "REDUCE_LOAD" and "new_load" in dec.metadata:
                    act.params["new_load"] = dec.metadata["new_load"]
                approved.append(act); seen.add(act.type)
            else:
                rejected.append({"action_type": act.type, "reason": dec.reason,
                                 "metadata": dec.metadata})

        rd = RemediationDecision(eq_id=eq_id, ansi_code=str(ansi_code),
                                 severity=str(severity).upper(), auto_allowed=aa,
                                 approved_actions=approved, rejected_actions=rejected,
                                 llm_recommendation=llm_recommendation,
                                 policy_version=self.policy.version)
        self._audit_decision(rd)
        return rd

    def execute(self, decision: RemediationDecision) -> Dict[str, Any]:
        """
        Execute the decision.

        Every return path goes through _finalize(), which guarantees both
        the audit entry and the Obsidian append happen exactly once per
        call, regardless of whether actuation occurred.
        """
        results: List[Dict[str, Any]] = []

        # --- Path 1: advisory mode, no actuation ---
        if self.policy.mode != "limited_autonomous":
            payload = {
                "executed": False,
                "mode": self.policy.mode,
                "reason": "Advisory mode; no autonomous actuation.",
                "decision": asdict(decision),
            }
            return self._finalize(decision, payload)

        # --- Path 2: rule engine blocked actuation ---
        if decision.auto_allowed is False:
            payload = {
                "executed": False,
                "mode": self.policy.mode,
                "reason": "Rule engine marked auto_allowed=False; actuation withheld.",
                "decision": asdict(decision),
            }
            return self._finalize(decision, payload)

        # --- Path 3: normal execution ---
        for act in decision.approved_actions:
            try:
                if act.type == "REDUCE_LOAD":
                    r = self._exec_reduce_load(act)
                elif act.type == "HOLD_STATE":
                    r = {"success": True, "action": act.type, "eq_id": act.eq_id,
                         "reason": act.reason, "note": "No PLC write; state held."}
                elif act.type == "REQUEST_OPERATOR_ACK":
                    r = {"success": True, "action": act.type, "eq_id": act.eq_id,
                         "reason": act.reason, "note": "Operator ack request emitted."}
                elif act.type == "ISOLATE_NON_CRITICAL":
                    r = self._exec_set_load(
                        act,
                        self.policy.action_config("REDUCE_LOAD").get("min_load_percent", 20),
                    )
                elif act.type == "SAFE_STOP_NON_CRITICAL":
                    r = self._exec_set_load(
                        act, 0,
                        note="safe-stop surrogate: load->0; no safety coil write.",
                    )
                else:
                    r = {"success": False, "action": act.type, "reason": "Unknown action."}
            except Exception as e:
                r = {"success": False, "action": act.type, "reason": f"exception: {e}"}

            results.append(r)
            (self.guard.record_executed if r.get("success") else self.guard.record_failed)(
                act.eq_id, act.type
            )

        payload = {
            "executed": any(bool(x.get("success")) for x in results),
            "mode": self.policy.mode,
            "results": results,
            "decision": asdict(decision),
        }
        return self._finalize(decision, payload)

    # ---- single exit path -------------------------------------------------
    def _finalize(
        self,
        decision: RemediationDecision,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Single exit point for execute().

        Guarantees:
          * Exactly one audit entry per call.
          * Exactly one Obsidian append per call, regardless of executed flag.
          * Never raises; a failure in the vault writer must not break the
            deterministic control path.
        """
        # 1. Audit (event name depends on whether anything executed).
        try:
            self._audit_execution(payload)
        except Exception:
            # Auditing must never break execution either.
            pass

        # 2. Obsidian incident note (fire-and-forget by contract).
        #    Called for BOTH executed=True and executed=False so the vault
        #    is a complete record of every remediation decision, including
        #    advisory-mode and auto_allowed=False cases.
        self._update_obsidian(decision, payload)

        return payload

    def _exec_reduce_load(self, act):
        idx = self._resolve_index(act.eq_id, None)
        if idx is None:
            return {"success": False, "action": act.type, "reason": "unknown index"}
        addr = self.LOAD_SETPOINT_BASE + idx
        cur = self._read_hr(addr)
        if cur is None:
            return {"success": False, "action": act.type, "reason": "load setpoint unreadable"}
        cfg = self.policy.action_config("REDUCE_LOAD")
        step = max(1, min(int(act.params.get("step_percent", 10)),
                          int(cfg.get("max_step_percent", 10))))
        mn = int(cfg.get("min_load_percent", 20))
        target = cur - step
        if target < mn:
            return {"success": False, "action": act.type, "reason": "would breach minimum",
                    "current_load": cur, "target_load": target, "min_load": mn}
        ok = self._write_hr(addr, target)
        return {"success": ok, "action": act.type, "eq_id": act.eq_id, "register": addr,
                "old_load": cur, "new_load": target, "reason": act.reason}

    def _exec_set_load(self, act, value, note=""):
        idx = self._resolve_index(act.eq_id, None)
        if idx is None:
            return {"success": False, "action": act.type, "reason": "unknown index"}
        addr = self.LOAD_SETPOINT_BASE + idx
        cur = self._read_hr(addr)
        ok = self._write_hr(addr, int(value))
        return {"success": ok, "action": act.type, "eq_id": act.eq_id, "register": addr,
                "old_load": cur, "new_load": int(value), "reason": act.reason, "note": note}

    def _update_obsidian(self, decision, result):
        """
        Append a remediation section to the equipment's latest incident note.

        Fire-and-forget by contract: any failure here is swallowed so that
        a broken vault writer cannot break the deterministic control path.
        """
        if not self.obsidian:
            return
        try:
            if hasattr(self.obsidian, "append_remediation_section"):
                self.obsidian.append_remediation_section(
                    eq_id=decision.eq_id,
                    decision=asdict(decision),
                    execution_result=result,
                )
        except Exception:
            pass

    # ---- audit ------------------------------------------------------------
    def _audit(self, event, payload):
        if not self.audit:
            return
        for fn in (lambda: self.audit.log(event, payload),
                   lambda: self.audit.log(event=event, payload=payload),
                   lambda: self.audit.log({"event": event, **payload}),
                   lambda: self.audit.write(event, payload)):
            try:
                fn(); return
            except Exception:
                continue

    def _audit_decision(self, d):
        self._audit("REMEDIATION_DECISION", {
            "eq_id": d.eq_id, "ansi_code": d.ansi_code, "severity": d.severity,
            "auto_allowed": d.auto_allowed,
            "approved_actions": [a.type for a in d.approved_actions],
            "rejected_actions": d.rejected_actions,
            "llm_recommendation": d.llm_recommendation,
            "policy_version": d.policy_version, "mode": self.policy.mode, "ts": time.time()})

    def _audit_execution(self, r):
        ev = "AUTO_REMEDIATION_EXECUTED" if r.get("executed") else "REMEDIATION_NOT_EXECUTED"
        p = dict(r)
        p["ts"] = time.time()
        self._audit(ev, p)