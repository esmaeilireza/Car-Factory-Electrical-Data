"""
Local LLM reasoning layer.

The LLM is used for explanation and diagnosis only.
It must never bypass deterministic safety rules.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from .config import AgentConfig
from .models import EquipmentSnapshot, Finding, OperatingMode, Severity, SystemState


ALLOWED_LLM_ACTIONS = {
    "nominal",
    "manual_review",
    "inspect_process",
    "inspect_sensors",
    "check_supply",
    "check_capacitor_bank",
    "reduce_load",
    "stop_equipment",
    "raise_alarm",
    "reconnect_modbus",
    "flush_session_cache",
    "restart_poller",
    "request_reset",
}


class LLMReasoner:
    def __init__(self, ai_engine: Any, config: AgentConfig):
        self.engine = ai_engine
        self.config = config
        self.last_call_time = 0.0

    def available(self) -> bool:
        return (
            self.engine is not None
            and getattr(self.engine, "llm", None) is not None
        )

    def build_prompt(
        self,
        state: SystemState,
        mode: OperatingMode,
        findings: List[Finding],
        snapshots: List[EquipmentSnapshot],
        stats: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        finding_lines = []
        for f in findings[:12]:
            finding_lines.append(
                f"- {f.eq_id} | {f.code} | {f.severity.value} | {f.message}"
            )

        snapshot_lines = []
        for s in snapshots[:6]:
            snapshot_lines.append(
                f"{s.eq_id}: V={s.voltage:.1f}, I={s.current:.1f}, "
                f"T={s.temperature:.1f}, PF={s.power_factor:.3f}, "
                f"load={s.load:.0f}%, theta={s.theta_per_mille / 10.0:.1f}%, "
                f"motor={s.motor_state}, trip={s.trip_word}, "
                f"lockout={s.lockout_status}, hb={s.heartbeat}"
            )

        # Inject 24-hour statistical summary from SQLite
        stats_lines = []
        for eq_id, st in (stats or {}).items():
            if not st or st.get("sample_count", 0) == 0:
                stats_lines.append(f"{eq_id} 24h: no historical data")
                continue
            last_trip = st.get("minutes_since_last_trip", -1)
            last_trip_str = f"{last_trip}min ago" if last_trip >= 0 else "never"
            stats_lines.append(
                f"{eq_id} 24h: I_mean={st['current_mean']}A "
                f"(sigma={st['current_std']}), "
                f"theta_max={st['theta_max']}%, "
                f"alarms_1h={st['alarm_count_1h']}, "
                f"last_trip={last_trip_str}, n={st['sample_count']}"
            )

        prompt = f"""You are NEXUS SCADA Industrial Supervisor AI.
You assist operators and shift engineers in a car factory electrical system.

Current system state: {state.value}
Operating mode: {mode.value}

Recent equipment snapshots:
{chr(10).join(snapshot_lines) if snapshot_lines else "No snapshot data."}

24-hour statistical context (SQLite history):
{chr(10).join(stats_lines) if stats_lines else "No statistics available."}

Active findings:
{chr(10).join(finding_lines) if finding_lines else "No active findings."}

Rules:
1. Be factual. Do not invent faults.
2. Use the 24-hour statistics to distinguish sustained trends from transient spikes.
3. If everything is normal, say NOMINAL.
4. If abnormal, give one likely diagnosis.
5. Recommend one safe operator action.
6. Never recommend bypassing safety, clearing E-STOP automatically, or restarting locked equipment.
7. Respond ONLY with valid JSON:
{{
  "diagnosis": "short diagnosis",
  "severity": "low|medium|high|critical",
  "confidence": 0.0,
  "recommended_action": "action_name",
  "operator_message": "short message for HMI"
}}
"""
        return prompt

    def analyze(
        self,
        state: SystemState,
        mode: OperatingMode,
        findings: List[Finding],
        snapshots: List[EquipmentSnapshot],
        stats: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.available():
            return None

        now = time.time()
        if now - self.last_call_time < self.config.llm_min_interval_sec:
            return None

        self.last_call_time = now
        prompt = self.build_prompt(state, mode, findings, snapshots, stats=stats)

        try:
            llm = getattr(self.engine, "llm", None)
            if llm is None:
                return None

            if hasattr(llm, "create_chat_completion"):
                response = llm.create_chat_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a precise industrial automation diagnostician. "
                                "You output only valid JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self.config.llm_temperature,
                    top_p=0.95,
                    repeat_penalty=1.1,
                    max_tokens=self.config.llm_max_tokens,
                )
                raw = response["choices"][0]["message"]["content"]
            else:
                raw = llm(
                    prompt,
                    max_new_tokens=self.config.llm_max_tokens,
                    temperature=self.config.llm_temperature,
                    top_p=0.95,
                    repetition_penalty=1.1,
                )

            parsed = self._extract_json(raw)
            if not parsed:
                return None

            parsed = self._normalize_result(parsed)
            parsed["source"] = "llm"
            parsed["raw"] = raw
            return parsed

        except Exception as e:
            return {
                "source": "llm_error",
                "diagnosis": "LLM analysis unavailable",
                "severity": "low",
                "confidence": 0.0,
                "recommended_action": "manual_review",
                "operator_message": f"Local AI unavailable: {type(e).__name__}",
                "raw": "",
            }

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None

        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass

        return None

    @staticmethod
    def _normalize_result(result: Dict[str, Any]) -> Dict[str, Any]:
        severity = str(result.get("severity", "low")).lower()
        if severity not in {"low", "medium", "high", "critical"}:
            severity = "low"

        action = str(result.get("recommended_action", "manual_review")).strip().lower()
        action = action.replace(" ", "_").replace("-", "_").replace("()", "")

        if action not in ALLOWED_LLM_ACTIONS:
            action = "manual_review"

        try:
            confidence = float(result.get("confidence", 0.0))
        except Exception:
            confidence = 0.0

        confidence = max(0.0, min(1.0, confidence))

        if confidence < 0.45:
            action = "manual_review"

        result["severity"] = severity
        result["recommended_action"] = action
        result["confidence"] = confidence
        return result