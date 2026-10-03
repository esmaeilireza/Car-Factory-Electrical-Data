from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .policy import RemediationPolicy, load_policy
from .safety_guard import EquipmentState, GuardDecision, SafetyGuard


logger = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parents[3]
VAULT = ROOT / "data" / "scada_vault"

PLC_HOST, PLC_PORT = "127.0.0.1", 5020

DEFAULT_EQ_IDS = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]

LOAD_SETPOINT_BASE = 160
LOAD_SETPOINT_COUNT = 6


class _BlockedResult:
    """Minimal Modbus-like error result."""

    registers = None

    @staticmethod
    def isError() -> bool:
        return True


class RestrictedPLCClient:
    """
    Prototype-only direct PLC client.

    Safety contract:
      - Reads are allowed for telemetry/status registers.
      - Writes are allowed ONLY for load setpoint registers HR[160..165].
      - Coil writes, E-STOP, RESET, fault injection, safety arm, and ANSI bypass
        writes are forbidden.
    """

    def __init__(self, host: str = PLC_HOST, port: int = PLC_PORT):
        self.host = host
        self.port = port
        self._client: Any = None
        self._lock = threading.Lock()
        self._failed = False

    def _ensure(self) -> Any:
        with self._lock:
            if self._failed:
                return None

            if self._client is not None:
                return self._client

            try:
                from pymodbus.client import ModbusTcpClient

                c = ModbusTcpClient(self.host, port=self.port)
                if c.connect():
                    self._client = c
                    return self._client

                self._failed = True
                return None
            except Exception:
                self._failed = True
                return None

    def connected(self) -> bool:
        return self._ensure() is not None

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def read_holding_registers(self, address: int, count: int = 1, **kwargs: Any) -> Any:
        c = self._ensure()
        if c is None:
            return _BlockedResult()

        attempts = [
            lambda: c.read_holding_registers(int(address), count=int(count), **kwargs),
            lambda: c.read_holding_registers(int(address), int(count), **kwargs),
            lambda: c.read_holding_registers(int(address), count=int(count)),
            lambda: c.read_holding_registers(int(address), int(count)),
        ]

        for fn in attempts:
            try:
                return fn()
            except Exception:
                continue

        return _BlockedResult()

    def write_register(self, address: int, value: int, **kwargs: Any) -> Any:
        addr = int(address)

        if not (LOAD_SETPOINT_BASE <= addr < LOAD_SETPOINT_BASE + LOAD_SETPOINT_COUNT):
            raise PermissionError(
                f"RestrictedPLCClient refuses write to HR[{addr}]; "
                f"only HR[{LOAD_SETPOINT_BASE}..{LOAD_SETPOINT_BASE + LOAD_SETPOINT_COUNT - 1}] allowed."
            )

        c = self._ensure()
        if c is None:
            return _BlockedResult()

        attempts = [
            lambda: c.write_register(addr, int(value), **kwargs),
            lambda: c.write_register(addr, int(value)),
        ]

        for fn in attempts:
            try:
                return fn()
            except Exception:
                continue

        return _BlockedResult()

    def write_coil(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("RestrictedPLCClient refuses coil writes.")

    def write_coils(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("RestrictedPLCClient refuses coil writes.")


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
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )


class RemediationEngine:
    """
    Bounded autonomous remediation engine.

    Invariants:
      - LLM recommends.
      - Deterministic safety guard decides.
      - Engine executes only approved bounded non-safety actions.
      - If rule engine marks auto_allowed=False, no actuation is performed.
      - Global E-STOP, RESET, fault injection, ANSI bypass are never touched.
      - PLC writes are restricted to load setpoints HR[160..165].
      - Every call to execute() produces BOTH an audit entry AND an Obsidian
        attempt, regardless of whether actuation occurred. This is enforced
        structurally by routing all exit paths through _finalize().
    """

    LOAD_SETPOINT_BASE = LOAD_SETPOINT_BASE
    LOAD_SETPOINT_COUNT = LOAD_SETPOINT_COUNT

    MOTOR_STATE_OFFSET = 8
    ALARM_FLAG_OFFSET = 9
    TRIP_WORD_OFFSET = 10
    LOCKOUT_OFFSET = 14
    THERMAL_OFFSET = 15
    TRIP_COUNT_OFFSET = 16
    SYS_STATUS_ADDR = 120

    def __init__(
        self,
        modbus_client: Any = None,
        audit_logger: Any = None,
        policy_path: Path | str = "configs/remediation_policy.json",
        obsidian_bridge: Any = None,
        equipment_ids: Optional[List[str]] = None,
        vault_path: Optional[Path] = None,
    ):
        self.client = modbus_client
        self.audit = audit_logger
        self.obsidian = obsidian_bridge
        self.equipment_ids = list(equipment_ids or DEFAULT_EQ_IDS)
        self.vault = Path(vault_path) if vault_path else VAULT

        pp = Path(policy_path)
        if not pp.is_absolute():
            pp = ROOT / pp

        self.policy: RemediationPolicy = load_policy(pp)
        self.guard = SafetyGuard(self.policy)

        self._direct_client: Optional[RestrictedPLCClient] = None
        self._direct_failed = False
        self._direct_audited = False
        self._direct_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mode / reset / capability
    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> bool:
        raw = getattr(self.policy, "raw", {}) or {}
        supported = list(raw.get("supported_modes", [])) or ["advisory", "limited_autonomous"]

        if mode not in supported:
            return False

        self.policy.mode = mode
        raw["mode"] = mode
        return True

    def reset_guard_counters(self) -> None:
        """
        Reset in-memory safety-guard rate/consecutive counters.

        This is intended for test harnesses and operator-triggered resets.
        It does not change policy, mode, or forbidden actions.
        """
        self.guard = SafetyGuard(self.policy)
        self._audit(
            "REMEDIATION_GUARD_COUNTERS_RESET",
            {
                "mode": getattr(self.policy, "mode", "?"),
                "policy_version": getattr(self.policy, "version", "?"),
            },
        )

    def can_command_plc(self) -> bool:
        if self.client is not None:
            return True
        return self._get_direct_client() is not None

    # ------------------------------------------------------------------
    # Index resolution
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Direct restricted PLC fallback
    # ------------------------------------------------------------------
    def _direct_enabled(self) -> bool:
        return os.environ.get("NEXUS_REMEDIATION_DISABLE_DIRECT_PLC", "0") != "1"

    def _get_direct_client(self) -> Optional[RestrictedPLCClient]:
        if not self._direct_enabled():
            return None

        with self._direct_lock:
            if self._direct_failed:
                return None

            if self._direct_client is not None:
                return self._direct_client

            c = RestrictedPLCClient()
            if not c.connected():
                self._direct_failed = True
                return None

            self._direct_client = c

            if not self._direct_audited:
                self._direct_audited = True
                self._audit(
                    "REMEDIATION_DIRECT_PLC_CLIENT_OPENED",
                    {
                        "host": PLC_HOST,
                        "port": PLC_PORT,
                        "allowed_write_registers": [
                            LOAD_SETPOINT_BASE + i for i in range(LOAD_SETPOINT_COUNT)
                        ],
                        "note": "Restricted prototype fallback; writes only load setpoints.",
                    },
                )

            return self._direct_client

    # ------------------------------------------------------------------
    # Signature-tolerant PLC I/O
    # ------------------------------------------------------------------
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
        if client is None:
            return None

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
        if client is None:
            return False

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

    def _read_hr(self, addr: int) -> Optional[int]:
        val = self._call_read(self.client, addr) if self.client is not None else None
        if val is not None:
            return val

        dc = self._get_direct_client()
        if dc is not None:
            return self._call_read(dc, addr)

        return None

    def _write_hr(self, addr: int, value: int) -> bool:
        ok = self._call_write(self.client, addr, value) if self.client is not None else False
        if ok:
            return True

        dc = self._get_direct_client()
        if dc is not None:
            return self._call_write(dc, addr, value)

        return False

    # ------------------------------------------------------------------
    # State reading
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Policy helpers
    # ------------------------------------------------------------------
    def _action_cfg(self, action_type: str) -> Dict[str, Any]:
        raw = getattr(self.policy, "raw", {}) or {}
        allowed = raw.get("allowed_actions", {}) or {}
        cfg = allowed.get(action_type, {}) or {}
        return dict(cfg) if isinstance(cfg, dict) else {}

    # ------------------------------------------------------------------
    # Proposal generation
    # ------------------------------------------------------------------
    def propose_from_ansi(
        self,
        eq_id: str,
        ansi_code: str,
        severity: str,
        state: EquipmentState,
        auto_allowed: Optional[bool],
    ) -> List[RemediationAction]:
        actions: List[RemediationAction] = []
        a = "".join(ch for ch in str(ansi_code) if ch.isdigit())
        sev = str(severity).upper()
        aa = bool(auto_allowed) if auto_allowed is not None else False

        def add(t: str, reason: str, params: Optional[Dict[str, Any]] = None, prio: int = 100) -> None:
            actions.append(
                RemediationAction(
                    type=t,
                    eq_id=eq_id,
                    reason=reason,
                    params=params or {},
                    priority=prio,
                )
            )

        # Notification/hold are always safe to propose.
        add(
            "REQUEST_OPERATOR_ACK",
            f"Operator awareness for ANSI-{a or 'NA'} ({sev}).",
            prio=50,
        )

        if sev in {"HIGH", "CRITICAL"}:
            add(
                "HOLD_STATE",
                f"Hold state during {sev} ANSI-{a}.",
                prio=40,
            )

        # Actuation requires explicit auto_allowed from the rule engine.
        if not aa:
            return actions

        cfg = self._action_cfg("REDUCE_LOAD")
        max_step = int(cfg.get("max_step_percent") or 10)
        step = max(1, min(10, max_step))

        if a in {"49", "38"}:
            add(
                "REDUCE_LOAD",
                f"Thermal/overload ANSI-{a}; reduce load to arrest degradation.",
                {"step_percent": step},
                prio=10,
            )

        if a == "51":
            add(
                "REDUCE_LOAD",
                f"Time overcurrent ANSI-{a}; reduce load before trip escalates.",
                {"step_percent": step},
                prio=20,
            )

        if a == "59":
            add(
                "REDUCE_LOAD",
                f"Overvoltage ANSI-{a}; reduce load and monitor feeder.",
                {"step_percent": max(1, step // 2)},
                prio=30,
            )

        if a == "50":
            add(
                "REQUEST_OPERATOR_ACK",
                f"Instantaneous OC ANSI-{a}; automatic recovery forbidden.",
                prio=5,
            )

        return actions

    def incorporate_llm(
        self,
        llm_rec: Optional[Dict[str, Any]],
        state: EquipmentState,
        auto_allowed: Optional[bool],
    ) -> List[RemediationAction]:
        if not llm_rec or not auto_allowed:
            return []

        t = str(llm_rec.get("recommended_action", "")).upper()
        allowed = {
            "REDUCE_LOAD",
            "HOLD_STATE",
            "REQUEST_OPERATOR_ACK",
            "ISOLATE_NON_CRITICAL",
            "SAFE_STOP_NON_CRITICAL",
        }

        if t not in allowed:
            return []

        params: Dict[str, Any] = {}
        if t == "REDUCE_LOAD":
            params["step_percent"] = int(llm_rec.get("step_percent") or 10)

        return [
            RemediationAction(
                type=t,
                eq_id=state.eq_id,
                reason=str(llm_rec.get("rationale", "LLM recommendation.")),
                params=params,
                priority=int(llm_rec.get("priority") or 70),
            )
        ]

    # ------------------------------------------------------------------
    # Evaluate / execute
    # ------------------------------------------------------------------
    def evaluate(
        self,
        eq_id: str,
        eq_index: Optional[int],
        ansi_code: str,
        severity: str,
        llm_recommendation: Optional[Dict[str, Any]] = None,
        auto_allowed: Optional[bool] = None,
    ) -> RemediationDecision:
        state = self.read_equipment_state(eq_id, eq_index)
        aa = True if auto_allowed is None else bool(auto_allowed)

        proposed = self.propose_from_ansi(eq_id, ansi_code, severity, state, aa)
        proposed += self.incorporate_llm(llm_recommendation, state, aa)

        approved: List[RemediationAction] = []
        rejected: List[Dict[str, Any]] = []
        seen = set()

        for act in sorted(proposed, key=lambda x: x.priority):
            if act.type in seen:
                continue

            dec: GuardDecision = self.guard.validate(act.type, state, act.params)

            if dec.approved:
                if act.type == "REDUCE_LOAD" and "new_load" in dec.metadata:
                    act.params["new_load"] = dec.metadata["new_load"]
                approved.append(act)
                seen.add(act.type)
            else:
                rejected.append(
                    {
                        "action_type": act.type,
                        "reason": dec.reason,
                        "metadata": dec.metadata,
                    }
                )

        rd = RemediationDecision(
            eq_id=eq_id,
            ansi_code=str(ansi_code),
            severity=str(severity).upper(),
            auto_allowed=aa,
            approved_actions=approved,
            rejected_actions=rejected,
            llm_recommendation=llm_recommendation,
            policy_version=str(getattr(self.policy, "version", "")),
        )

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

        # Path 1: advisory mode, no actuation.
        if getattr(self.policy, "mode", "advisory") != "limited_autonomous":
            payload = {
                "executed": False,
                "mode": getattr(self.policy, "mode", "advisory"),
                "reason": "Advisory mode; no autonomous actuation.",
                "decision": asdict(decision),
            }
            return self._finalize(decision, payload)

        # Path 2: rule engine withheld autonomy.
        if decision.auto_allowed is False:
            payload = {
                "executed": False,
                "mode": getattr(self.policy, "mode", "?"),
                "reason": "Rule engine marked auto_allowed=False; actuation withheld.",
                "decision": asdict(decision),
            }
            return self._finalize(decision, payload)

        # Path 3: execute approved bounded actions.
        for act in decision.approved_actions:
            try:
                if act.type == "REDUCE_LOAD":
                    r = self._exec_reduce_load(act)
                elif act.type == "HOLD_STATE":
                    r = {
                        "success": True,
                        "action": act.type,
                        "eq_id": act.eq_id,
                        "reason": act.reason,
                        "note": "No PLC write performed; state held.",
                    }
                elif act.type == "REQUEST_OPERATOR_ACK":
                    r = {
                        "success": True,
                        "action": act.type,
                        "eq_id": act.eq_id,
                        "reason": act.reason,
                        "note": "Operator acknowledgment request emitted.",
                    }
                elif act.type == "ISOLATE_NON_CRITICAL":
                    cfg = self._action_cfg("REDUCE_LOAD")
                    min_load = int(cfg.get("min_load_percent") or 20)
                    r = self._exec_set_load(
                        act,
                        min_load,
                        note="non-critical isolation surrogate",
                    )
                elif act.type == "SAFE_STOP_NON_CRITICAL":
                    r = self._exec_set_load(
                        act,
                        0,
                        note="safe-stop surrogate: load to zero; no safety coil write",
                    )
                else:
                    r = {"success": False, "action": act.type, "reason": "Unknown action."}
            except Exception as e:
                r = {"success": False, "action": act.type, "reason": f"exception: {e}"}

            results.append(r)
            self._record_guard_result(act.eq_id, act.type, bool(r.get("success")))

        payload = {
            "executed": any(bool(x.get("success")) for x in results),
            "mode": getattr(self.policy, "mode", "?"),
            "results": results,
            "decision": asdict(decision),
        }

        return self._finalize(decision, payload)

    # ------------------------------------------------------------------
    # Single exit path
    # ------------------------------------------------------------------
    def _finalize(
        self,
        decision: RemediationDecision,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Single exit point for execute().

        Guarantees:
          * Exactly one audit entry per call.
          * Exactly one Obsidian append attempt per call, regardless of executed flag.
          * Never raises; a failure in the vault writer must not break the
            deterministic control path.
        """
        # 1. Audit.
        try:
            self._audit_execution(payload)
        except Exception as exc:
            logger.warning(
                "Remediation audit failed for eq_id=%r: %s",
                getattr(decision, "eq_id", "?"),
                exc,
                exc_info=True,
            )

        # 2. Obsidian incident note.
        # Called for BOTH executed=True and executed=False so the vault
        # is a complete record of every remediation decision, including
        # advisory-mode and auto_allowed=False cases.
        try:
            self._update_obsidian(decision, payload)
        except Exception as exc:
            logger.warning(
                "Obsidian update failed for eq_id=%r: %s",
                getattr(decision, "eq_id", "?"),
                exc,
                exc_info=True,
            )

        return payload

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------
    def _record_guard_result(self, eq_id: str, action_type: str, success: bool) -> None:
        try:
            if success:
                if hasattr(self.guard, "record_executed"):
                    self.guard.record_executed(eq_id, action_type)
            else:
                if hasattr(self.guard, "record_failed"):
                    self.guard.record_failed(eq_id, action_type)
        except Exception:
            pass

    def _exec_reduce_load(self, act: RemediationAction) -> Dict[str, Any]:
        idx = self._resolve_index(act.eq_id, None)
        if idx is None:
            return {"success": False, "action": act.type, "reason": "unknown equipment index"}

        addr = self.LOAD_SETPOINT_BASE + idx
        cur = self._read_hr(addr)
        if cur is None:
            return {"success": False, "action": act.type, "reason": "load setpoint unreadable"}

        cfg = self._action_cfg("REDUCE_LOAD")
        max_step = int(cfg.get("max_step_percent") or 10)
        min_load = int(cfg.get("min_load_percent") or 20)

        step = max(1, min(int(act.params.get("step_percent") or 10), max_step))
        target = cur - step

        if target < min_load:
            return {
                "success": False,
                "action": act.type,
                "reason": "requested reduction would breach policy minimum",
                "current_load": cur,
                "target_load": target,
                "min_load": min_load,
            }

        ok = self._write_hr(addr, target)
        return {
            "success": ok,
            "action": act.type,
            "eq_id": act.eq_id,
            "register": addr,
            "old_load": cur,
            "new_load": target,
            "reason": act.reason,
        }

    def _exec_set_load(self, act: RemediationAction, value: int, note: str = "") -> Dict[str, Any]:
        idx = self._resolve_index(act.eq_id, None)
        if idx is None:
            return {"success": False, "action": act.type, "reason": "unknown equipment index"}

        addr = self.LOAD_SETPOINT_BASE + idx
        cur = self._read_hr(addr)
        ok = self._write_hr(addr, int(value))

        return {
            "success": ok,
            "action": act.type,
            "eq_id": act.eq_id,
            "register": addr,
            "old_load": cur,
            "new_load": int(value),
            "reason": act.reason,
            "note": note,
        }

    # ------------------------------------------------------------------
    # Obsidian integration
    # ------------------------------------------------------------------
    def _update_obsidian(self, decision: RemediationDecision, result: Dict[str, Any]) -> None:
        """
        Append a remediation section to the equipment's latest incident note.

        Fire-and-forget by contract: any failure here is logged and swallowed
        so that a broken vault writer cannot break the deterministic control
        path.
        """
        result = result or {}

        try:
            logger.debug(
                "_update_obsidian: eq_id=%r executed=%r",
                getattr(decision, "eq_id", None),
                result.get("executed"),
            )

            eq_id = getattr(decision, "eq_id", "")
            if not eq_id:
                logger.warning("_update_obsidian: eq_id is empty — skipping Obsidian append")
                return

            decision_dict = asdict(decision)

            # 1. Try the configured Obsidian bridge first.
            if self.obsidian is not None and hasattr(self.obsidian, "append_remediation_section"):
                try:
                    self.obsidian.append_remediation_section(
                        eq_id=decision.eq_id,
                        decision=decision_dict,
                        execution_result=result,
                    )
                except Exception as exc:
                    logger.warning(
                        "append_remediation_section failed for eq_id=%r: %s",
                        decision.eq_id,
                        exc,
                        exc_info=True,
                    )

            # 2. Ensure a vault record exists even if the bridge is missing or failed.
            #    The worker avoids duplicate sections when possible.
            self._schedule_obsidian_direct_append(decision_dict, result)

        except Exception as exc:
            logger.warning(
                "Unexpected _update_obsidian failure for eq_id=%r: %s",
                getattr(decision, "eq_id", "?"),
                exc,
                exc_info=True,
            )

    def _schedule_obsidian_direct_append(
        self,
        decision_dict: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        if not self.vault:
            return

        t = threading.Thread(
            target=self._obsidian_direct_append_worker,
            args=(
                str(decision_dict.get("eq_id", "")),
                str(decision_dict.get("ansi_code", "")),
                decision_dict,
                result,
                time.time(),
            ),
            daemon=True,
        )
        t.start()

    def _obsidian_direct_append_worker(
        self,
        eq_id: str,
        ansi_code: str,
        decision_dict: Dict[str, Any],
        result: Dict[str, Any],
        start_ts: float,
    ) -> None:
        try:
            inc_dir = self.vault / "Incidents"
            inc_dir.mkdir(parents=True, exist_ok=True)

            section = self._format_remediation_section(decision_dict, result)
            deadline = time.time() + 120.0

            while time.time() < deadline:
                try:
                    files = list(inc_dir.glob("*.md"))
                except Exception:
                    files = []

                candidates = []

                for p in files:
                    try:
                        st = p.stat()
                        if st.st_mtime < start_ts - 300:
                            continue
                        body = p.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue

                    if eq_id and eq_id in body:
                        candidates.append((st.st_mtime, p, body))

                candidates.sort(key=lambda item: item[0], reverse=True)

                for _, p, body in candidates:
                    if "Autonomous Remediation" not in body:
                        try:
                            with p.open("a", encoding="utf-8") as f:
                                f.write(section)

                            self._audit(
                                "REMEDIATION_OBSIDIAN_APPENDED",
                                {
                                    "eq_id": eq_id,
                                    "incident": p.name,
                                    "mode": result.get("mode"),
                                    "executed": result.get("executed"),
                                },
                            )
                        except Exception as exc:
                            logger.warning(
                                "Obsidian append failed for eq_id=%r incident=%r: %s",
                                eq_id,
                                p.name,
                                exc,
                                exc_info=True,
                            )
                            self._audit(
                                "REMEDIATION_OBSIDIAN_APPEND_ERROR",
                                {
                                    "eq_id": eq_id,
                                    "incident": p.name,
                                    "error": str(exc),
                                },
                            )
                        return

                    self._audit(
                        "REMEDIATION_OBSIDIAN_ALREADY_PRESENT",
                        {
                            "eq_id": eq_id,
                            "incident": p.name,
                            "mode": result.get("mode"),
                            "executed": result.get("executed"),
                        },
                    )
                    return

                # No existing incident note found. Create one.
                safe_eq = "".join(
                    ch if ch.isalnum() or ch in "-_" else "_"
                    for ch in str(eq_id or "UNKNOWN")
                )

                p = inc_dir / f"Incident_{int(start_ts * 1000)}_{safe_eq}.md"

                status = "REMEDIATED" if result.get("executed") else "WITHHELD"
                severity = decision_dict.get("severity", "")

                initial = (
                    "---\n"
                    f"equipment_id: {eq_id}\n"
                    f"status: {status}\n"
                    f"severity: {severity}\n"
                    f"ansi_code: {ansi_code}\n"
                    f"created: {datetime.now().isoformat(timespec='seconds')}\n"
                    "---\n\n"
                    f"# Incident {eq_id}\n\n"
                    "## Detection\n\n"
                    "- Source: NEXUS remediation engine\n"
                    f"- ANSI code: {ansi_code}\n"
                    f"- Severity: {severity}\n"
                    f"- Auto allowed: {decision_dict.get('auto_allowed')}\n"
                )

                try:
                    p.write_text(initial + section, encoding="utf-8")
                    self._audit(
                        "REMEDIATION_OBSIDIAN_CREATED",
                        {
                            "eq_id": eq_id,
                            "incident": p.name,
                            "mode": result.get("mode"),
                            "executed": result.get("executed"),
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "Obsidian create failed for eq_id=%r incident=%r: %s",
                        eq_id,
                        p.name,
                        exc,
                        exc_info=True,
                    )
                    self._audit(
                        "REMEDIATION_OBSIDIAN_CREATE_ERROR",
                        {
                            "eq_id": eq_id,
                            "incident": p.name,
                            "error": str(exc),
                        },
                    )

                return

            self._audit(
                "REMEDIATION_OBSIDIAN_APPEND_TIMEOUT",
                {
                    "eq_id": eq_id,
                    "ansi_code": ansi_code,
                    "vault": str(self.vault),
                },
            )

        except Exception as exc:
            logger.warning(
                "Obsidian worker failed for eq_id=%r: %s",
                eq_id,
                exc,
                exc_info=True,
            )
            self._audit(
                "REMEDIATION_OBSIDIAN_WORKER_ERROR",
                {
                    "eq_id": eq_id,
                    "ansi_code": ansi_code,
                    "error": str(exc),
                },
            )

    def _format_remediation_section(
        self,
        decision_dict: Dict[str, Any],
        result: Dict[str, Any],
    ) -> str:
        approved = []
        for a in decision_dict.get("approved_actions", []) or []:
            if isinstance(a, dict):
                approved.append(str(a.get("type", "")))
            else:
                approved.append(str(a))

        rejected = []
        for r in decision_dict.get("rejected_actions", []) or []:
            if isinstance(r, dict):
                rejected.append(str(r.get("action_type", "")))
            else:
                rejected.append(str(r))

        executed = bool(result.get("executed"))
        mode = result.get("mode")
        reason = result.get("reason", "")

        load_lines = []
        for r in result.get("results", []) or []:
            if not isinstance(r, dict):
                continue
            if r.get("old_load") is not None and r.get("new_load") is not None:
                load_lines.append(
                    f"- `{r.get('action')}` on `{r.get('eq_id', '')}`: "
                    f"load `{r.get('old_load')}` -> `{r.get('new_load')}`"
                )

        lines = [
            "",
            "## Autonomous Remediation",
            "",
            f"- Timestamp: `{datetime.now().isoformat(timespec='seconds')}`",
            f"- ANSI code: `{decision_dict.get('ansi_code', '')}`",
            f"- Severity: `{decision_dict.get('severity', '')}`",
            f"- Auto allowed: `{decision_dict.get('auto_allowed')}`",
            f"- Policy version: `{decision_dict.get('policy_version', '')}`",
            f"- Mode: `{mode}`",
            f"- Executed: `{executed}`",
            f"- Reason: `{reason or 'n/a'}`",
            f"- Approved actions: `{', '.join(x for x in approved if x) or 'none'}`",
            f"- Rejected actions: `{', '.join(x for x in rejected if x) or 'none'}`",
            "",
        ]

        if load_lines:
            lines.append("### Load Setpoint Changes")
            lines.append("")
            lines.extend(load_lines)
            lines.append("")

        llm = decision_dict.get("llm_recommendation")
        if isinstance(llm, dict) and llm:
            lines.append("### LLM Recommendation")
            lines.append("")
            lines.append(f"- Recommended action: `{llm.get('recommended_action', '')}`")
            lines.append(f"- Step percent: `{llm.get('step_percent', '')}`")
            lines.append(f"- Priority: `{llm.get('priority', '')}`")
            lines.append(f"- Rationale: {llm.get('rationale', '')}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Audit helpers
    # ------------------------------------------------------------------
    def _audit(self, event: str, payload: Dict[str, Any]) -> None:
        if not self.audit:
            return

        attempts = [
            lambda: self.audit.log(event, payload),
            lambda: self.audit.log(event=event, payload=payload),
            lambda: self.audit.log({"event": event, **payload}),
            lambda: self.audit.write(event, payload),
            lambda: self.audit.write({"event": event, **payload}),
        ]

        for fn in attempts:
            try:
                fn()
                return
            except Exception:
                continue

    def _audit_decision(self, d: RemediationDecision) -> None:
        self._audit(
            "REMEDIATION_DECISION",
            {
                "eq_id": d.eq_id,
                "ansi_code": d.ansi_code,
                "severity": d.severity,
                "auto_allowed": d.auto_allowed,
                "approved_actions": [a.type for a in d.approved_actions],
                "rejected_actions": d.rejected_actions,
                "llm_recommendation": d.llm_recommendation,
                "policy_version": d.policy_version,
                "mode": getattr(self.policy, "mode", "?"),
                "ts": time.time(),
            },
        )

    def _audit_execution(self, r: Dict[str, Any]) -> None:
        event = "AUTO_REMEDIATION_EXECUTED" if r.get("executed") else "REMEDIATION_NOT_EXECUTED"
        payload = dict(r)
        payload["ts"] = time.time()
        self._audit(event, payload)