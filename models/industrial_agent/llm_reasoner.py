"""
Local LLM reasoning layer.

The LLM is used for explanation and diagnosis only.
It must never bypass deterministic safety rules.

Safety contract
---------------
* The LLM emits a single structured JSON object (see ``build_prompt``).
* ``parse_llm_output`` validates every field and *only* forwards a
  ``recommended_action`` that appears in ``ALLOWED_REMEDIATION_ACTIONS``.
* Anything unknown, ambiguous, or explicitly forbidden (e.g.
  ``RESET_GLOBAL_ESTOP``) is downgraded to ``NONE`` with a rationale
  explaining the rejection. Even a perfect-looking JSON payload is
  rejected if it asks for something the deterministic layer does not
  permit.
* The LLM output is a *recommendation*, never a command. The
  downstream safety layer still owns execution.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from .config import AgentConfig
from .models import EquipmentSnapshot, Finding, OperatingMode, Severity, SystemState


# ---------------------------------------------------------------------------
# Safety whitelists
# ---------------------------------------------------------------------------

# The only remediation actions the LLM may *recommend*.
# Anything else is rejected by ``parse_llm_output`` and downgraded to NONE.
ALLOWED_REMEDIATION_ACTIONS = frozenset({
    "REDUCE_LOAD",
    "HOLD_STATE",
    "REQUEST_OPERATOR_ACK",
    "ISOLATE_NON_CRITICAL",
    "SAFE_STOP_NON_CRITICAL",
    "NONE",
})

# Actions that are never allowed via the LLM path. Listed explicitly so
# rejection messages can be specific instead of just "unknown action".
# The whitelist above is the enforcement mechanism; this set only
# improves diagnostics/logging.
EXPLICITLY_FORBIDDEN_ACTIONS = frozenset({
    "RESET_GLOBAL_ESTOP",
    "RESET_ESTOP",
    "CLEAR_ESTOP",
    "BYPASS_SAFETY",
    "BYPASS_INTERLOCK",
    "RESTART_LOCKED_EQUIPMENT",
    "FORCE_OUTPUT",
    "OVERRIDE_TRIP",
    "DISABLE_PROTECTION",
    "CLOSE_BREAKER",
    "ENERGIZE",
})

ALLOWED_SEVERITIES = frozenset({"low", "medium", "high", "critical"})

# Below this confidence, no active autonomous recommendation is allowed.
MIN_USEFUL_CONFIDENCE = 0.45


# ---------------------------------------------------------------------------
# System prompt for the chat-style LLM call
# ---------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = (
    "You are a precise industrial automation diagnostician. "
    "You output only a single valid JSON object and nothing else."
)


# ---------------------------------------------------------------------------
# Parsing / validation helpers (module level, testable in isolation)
# ---------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a single JSON object from LLM output."""
    if not text:
        return None

    # Preferred: fenced ```json { ... } ``` block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Fallback: first {...} blob
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Last resort: the whole string is JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _is_prompt_echo(raw: str) -> bool:
    """Detect small-model prompt echoing.

    Small local models sometimes regurgitate the schema example from
    the prompt verbatim. Those placeholder strings only exist in the
    prompt, never in a real diagnosis, so seeing them means the model
    did not actually reason about the data.
    """
    if not raw:
        return False
    lowered = raw.lower()
    markers = (
        "short diagnosis",
        "one-line diagnosis",
        "short root cause",
        "one-line likely root cause",
        "short hmi message",
        "short message for hmi",
        "why this action is appropriate",
        "why this action",
    )
    return any(marker in lowered for marker in markers)


def _safe_default(rationale: str, diagnosis: str = "") -> Dict[str, Any]:
    """Canonical safe (no-action) result."""
    return {
        "diagnosis": diagnosis or "No diagnosis available",
        "root_cause": "Unknown",
        "severity": "low",
        "safety_level": 1,
        "confidence": 0.0,
        "operator_message": rationale,
        "remediation_recommendation": {
            "recommended_action": "NONE",
            "step_percent": 0,
            "rationale": rationale,
            "priority": 50,
        },
    }


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_llm_output(raw: str) -> Dict[str, Any]:
    """Parse and validate the LLM's structured recommendation.

    Safety contract
    ---------------
    * Only actions present in ``ALLOWED_REMEDIATION_ACTIONS`` survive.
    * Explicitly forbidden actions (e.g. ``RESET_GLOBAL_ESTOP``) are
      detected and downgraded to ``NONE``.
    * Unknown / unstructured output also yields ``NONE``.
    * The function never raises on malformed LLM output -- it degrades
      safely.
    """
    data = _extract_json_from_text(raw)
    if data is None or not isinstance(data, dict):
        return _safe_default(
            "LLM output was not structured; no autonomous action recommended.",
            diagnosis=(raw or "").strip()[:400],
        )

    # --- Top-level scalars ---------------------------------------------
    diagnosis = str(data.get("diagnosis", "")).strip() or "No diagnosis provided"
    root_cause = str(data.get("root_cause", "")).strip() or "Unknown"

    severity = str(data.get("severity", "low")).strip().lower()
    if severity not in ALLOWED_SEVERITIES:
        severity = "low"

    safety_level = _coerce_int(data.get("safety_level", 1), 1)
    safety_level = max(1, min(5, safety_level))

    confidence = _coerce_float(data.get("confidence", 0.0), 0.0)
    confidence = max(0.0, min(1.0, confidence))

    operator_message = (
        str(data.get("operator_message", "")).strip()
        or "Manual review recommended"
    )

    # --- Remediation block (safety-critical) ---------------------------
    rec_in = data.get("remediation_recommendation") or {}
    if not isinstance(rec_in, dict):
        rec_in = {}

    raw_action = str(rec_in.get("recommended_action", "NONE")).strip().upper()
    raw_action = re.sub(r"[\s\-]+", "_", raw_action)

    if raw_action in ALLOWED_REMEDIATION_ACTIONS:
        action = raw_action
        rationale = (
            str(rec_in.get("rationale", "")).strip()
            or "Action approved by schema validation."
        )
    elif raw_action in EXPLICITLY_FORBIDDEN_ACTIONS:
        action = "NONE"
        rationale = (
            f"REJECTED explicitly forbidden LLM action '{raw_action}'. "
            "This action can never be taken via the LLM path; "
            "operator acknowledgement is required."
        )
    else:
        action = "NONE"
        rationale = (
            f"Rejected unknown/unstructured LLM action '{raw_action}'. "
            "No autonomous action recommended."
        )

    # step_percent is only meaningful for REDUCE_LOAD.
    step_percent = _coerce_int(rec_in.get("step_percent", 0), 0)
    if action == "REDUCE_LOAD":
        step_percent = max(1, min(50, step_percent or 10))
    else:
        step_percent = 0

    priority = _coerce_int(rec_in.get("priority", 50), 50)
    priority = max(0, min(100, priority))

    # Low confidence => human in the loop, never an active action.
    if confidence < MIN_USEFUL_CONFIDENCE and action not in (
        "NONE",
        "REQUEST_OPERATOR_ACK",
    ):
        rationale = (
            f"Confidence {confidence:.2f} below threshold "
            f"({MIN_USEFUL_CONFIDENCE}); downgraded '{action}' to "
            f"REQUEST_OPERATOR_ACK. Original rationale: {rationale}"
        )
        action = "REQUEST_OPERATOR_ACK"
        step_percent = 0

    return {
        "diagnosis": diagnosis,
        "root_cause": root_cause,
        "severity": severity,
        "safety_level": safety_level,
        "confidence": confidence,
        "operator_message": operator_message,
        "remediation_recommendation": {
            "recommended_action": action,
            "step_percent": step_percent,
            "rationale": rationale,
            "priority": priority,
        },
    }


def _llm_error_result(exc: Exception) -> Dict[str, Any]:
    return {
        "source": "llm_error",
        "diagnosis": "LLM analysis unavailable",
        "root_cause": "Unknown",
        "severity": "low",
        "safety_level": 1,
        "confidence": 0.0,
        "operator_message": f"Local AI unavailable: {type(exc).__name__}",
        "remediation_recommendation": {
            "recommended_action": "NONE",
            "step_percent": 0,
            "rationale": "LLM call failed; no autonomous action recommended.",
            "priority": 50,
        },
        "raw": "",
    }


# ---------------------------------------------------------------------------
# Reasoner
# ---------------------------------------------------------------------------

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

    # -- prompt construction --------------------------------------------

    def build_prompt(
        self,
        state: SystemState,
        mode: OperatingMode,
        findings: List[Finding],
        snapshots: List[EquipmentSnapshot],
        stats: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        finding_lines = [
            f"- {f.eq_id} | {f.code} | {f.severity.value} | {f.message}"
            for f in findings[:12]
        ]

        snapshot_lines = [
            f"{s.eq_id}: V={s.voltage:.1f}, I={s.current:.1f}, "
            f"T={s.temperature:.1f}, PF={s.power_factor:.3f}, "
            f"load={s.load:.0f}%, theta={s.theta_per_mille / 10.0:.1f}%, "
            f"motor={s.motor_state}, trip={s.trip_word}, "
            f"lockout={s.lockout_status}, hb={s.heartbeat}"
            for s in snapshots[:6]
        ]

        stats_lines: List[str] = []
        for eq_id, st in (stats or {}).items():
            if not st or st.get("sample_count", 0) == 0:
                stats_lines.append(f"{eq_id} 24h: no historical data")
                continue
            last_trip = st.get("minutes_since_last_trip", -1)
            last_trip_str = (
                f"{last_trip}min ago" if last_trip >= 0 else "never"
            )
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

Return ONLY one valid JSON object. No prose, no markdown, no code fences.

Schema:
{{
  "diagnosis": "one-line diagnosis",
  "root_cause": "one-line likely root cause",
  "severity": "low|medium|high|critical",
  "safety_level": 1,
  "confidence": 0.0,
  "operator_message": "short HMI message",
  "remediation_recommendation": {{
    "recommended_action": "REDUCE_LOAD | HOLD_STATE | REQUEST_OPERATOR_ACK | ISOLATE_NON_CRITICAL | SAFE_STOP_NON_CRITICAL | NONE",
    "step_percent": 10,
    "rationale": "why this action is appropriate",
    "priority": 50
  }}
}}

Allowed remediation actions (choose exactly one):
- REDUCE_LOAD              (lower load; set step_percent 1..50)
- HOLD_STATE               (keep current state, no change)
- REQUEST_OPERATOR_ACK     (ask the human operator to acknowledge)
- ISOLATE_NON_CRITICAL     (isolate only non-critical equipment)
- SAFE_STOP_NON_CRITICAL   (safe-stop only non-critical equipment)
- NONE                     (no autonomous recommendation)

Hard rules:
1. Be factual. Do not invent faults that are not visible in the data.
2. Use the 24-hour statistics to distinguish sustained trends from
   transient spikes.
3. If everything is normal, use recommended_action = "NONE" and
   severity = "low".
4. NEVER recommend safety bypasses, E-STOP resets, breaker closing,
   interlock bypass, or restarting locked equipment. Any such output
   will be rejected by the safety layer.
5. If you are not confident, use "REQUEST_OPERATOR_ACK" or "NONE".
6. Output JSON only. No explanations outside the JSON.
"""
        return prompt

    # -- main entry point -----------------------------------------------

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
                        {"role": "system", "content": LLM_SYSTEM_PROMPT},
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

            # Canary: reject verbatim schema echoes from small models.
            if _is_prompt_echo(raw):
                return None

            parsed = parse_llm_output(raw)
            parsed["source"] = "llm"
            parsed["raw"] = raw
            return parsed

        except Exception as exc:  # noqa: BLE001 - degrade safely
            return _llm_error_result(exc)