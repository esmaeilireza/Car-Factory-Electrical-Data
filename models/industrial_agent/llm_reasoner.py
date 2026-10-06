"""
LLM reasoner for NEXUS SCADA industrial cognitive agent.

Responsibilities:
  - Build a bounded, evidence-grounded prompt from deterministic findings.
  - Call the local LLM engine if available.
  - Parse structured JSON output safely.
  - Sanitize remediation recommendations against a strict allow-list.
  - Expose episodic-memory metadata so the verifier can prove prior vault
    incidents were included in the prompt/result path.
  - Enforce focus-equipment attribution so the LLM does not bleed context
    across devices during rapid fault-injection sweeps.
  - Add an evidence-consistency guard: if the LLM says "no active protection
    events" while the deterministic rule engine just emitted a HIGH/CRITICAL
    finding, the LLM output is flagged and the operator message is reinforced
    from deterministic evidence.

Safety contract:
  - The LLM may recommend.
  - The deterministic guard decides.
  - Forbidden actions are downgraded to NONE.
  - Unknown/unstructured output yields a safe manual-review default.
  - This module must never raise into the agent loop.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional


# ---------------------------------------------------------------------------
# Action policy constants
# ---------------------------------------------------------------------------

ALLOWED_REMEDIATION_ACTIONS = {
    "REDUCE_LOAD",
    "HOLD_STATE",
    "REQUEST_OPERATOR_ACK",
    "ISOLATE_NON_CRITICAL",
    "SAFE_STOP_NON_CRITICAL",
    "NONE",
}

FORBIDDEN_REMEDIATION_ACTIONS = {
    "RESET_GLOBAL_ESTOP",
    "RESET_ESTOP",
    "CLEAR_ESTOP",
    "BYPASS_SAFETY",
    "BYPASS_INTERLOCK",
    "ANSI_BYPASS",
    "OPEN_COIL",
    "CLOSE_COIL",
    "WRITE_COIL",
    "WRITE_COILS",
    "FORCE_MOTOR_START",
    "FORCE_MOTOR_STOP",
    "MODIFY_SAFETY_SETPOINT",
    "CHANGE_PROTECTION_SETTINGS",
    "TRIP_BREAKER",
    "RESET_PROTECTION",
    "DISABLE_TRIP",
    "DISABLE_ALARM",
    "IGNORE_ESTOP",
    "OVERRIDE_SAFETY",
}

_ALLOWED_TOP_LEVEL_ACTIONS = {
    "manual_review",
    "reduce_load",
    "hold_state",
    "request_operator_ack",
    "isolate_non_critical",
    "safe_stop_non_critical",
    "none",
}

_TOP_ACTION_ALIASES = {
    "reduce load": "reduce_load",
    "reduce-load": "reduce_load",
    "reduce_load": "reduce_load",
    "reduceload": "reduce_load",
    "hold state": "hold_state",
    "hold-state": "hold_state",
    "hold_state": "hold_state",
    "holdstate": "hold_state",
    "request operator ack": "request_operator_ack",
    "request-operator-ack": "request_operator_ack",
    "request_operator_ack": "request_operator_ack",
    "requestoperatorack": "request_operator_ack",
    "operator_ack": "request_operator_ack",
    "acknowledge": "request_operator_ack",
    "isolate non critical": "isolate_non_critical",
    "isolate-non-critical": "isolate_non_critical",
    "isolate_non_critical": "isolate_non_critical",
    "isolatenoncritical": "isolate_non_critical",
    "safe stop non critical": "safe_stop_non_critical",
    "safe-stop-non-critical": "safe_stop_non_critical",
    "safe_stop_non_critical": "safe_stop_non_critical",
    "safestopnoncritical": "safe_stop_non_critical",
    "manual review": "manual_review",
    "manual-review": "manual_review",
    "manual_review": "manual_review",
    "manualreview": "manual_review",
    "none": "none",
    "no_action": "none",
    "no action": "none",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""

    inner = _enum_value(value)
    text = str(inner).strip()

    if text.upper().startswith("SEVERITY."):
        text = text.split(".", 1)[1]

    return text


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        v = float(value)
    except Exception:
        return default

    if v < low:
        return low
    if v > high:
        return high
    return v


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        v = int(value)
    except Exception:
        return default

    if v < low:
        return low
    if v > high:
        return high
    return v


def _normalize_severity(value: Any) -> str:
    text = _normalize_text(value).lower()

    if text in {"crit", "critical"}:
        return "critical"
    if text in {"high", "urgent"}:
        return "high"
    if text in {"med", "medium", "warning"}:
        return "medium"
    if text in {"low", "info", "informational"}:
        return "low"

    return "low"


def _normalize_top_action(value: Any) -> str:
    raw = _normalize_text(value).lower().strip()
    if not raw:
        return "manual_review"

    mapped = _TOP_ACTION_ALIASES.get(raw, raw)
    mapped = mapped.replace(" ", "_").replace("-", "_")

    if mapped in _ALLOWED_TOP_LEVEL_ACTIONS:
        return mapped

    return "manual_review"


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    # 1. Fenced code block.
    fence = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass

    # 2. Balanced first object.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False

        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            break

    # 3. Greedy fallback.
    greedy = re.search(r"\{.*\}", text, re.DOTALL)
    if greedy:
        try:
            return json.loads(greedy.group(0))
        except Exception:
            return None

    return None


def _safe_default(
    raw: str = "",
    diagnosis: str = "",
    message: str = "",
    rationale: str = "",
) -> Dict[str, Any]:
    raw_snippet = (raw or "").strip()[:400]

    return {
        "diagnosis": diagnosis or raw_snippet or "LLM output could not be parsed.",
        "operator_message": message or "Manual review recommended.",
        "severity": "low",
        "confidence": 0.0,
        "recommended_action": "manual_review",
        "step_percent": 0,
        "priority": 100,
        "rationale": rationale or "Structured LLM output was unavailable; defaulting to manual review.",
        "remediation_recommendation": {
            "recommended_action": "NONE",
            "step_percent": 0,
            "priority": 100,
            "rationale": "No safe autonomous action could be parsed.",
        },
    }


def _sanitize_remediation(
    value: Any,
    primary_eq_id: str = "",
) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    action_value: Any = None

    if isinstance(value, dict):
        obj = value
        action_value = (
            obj.get("recommended_action")
            or obj.get("action")
            or obj.get("type")
        )
    elif isinstance(value, str):
        action_value = value
    else:
        action_value = None

    action = _normalize_text(action_value).upper().strip()
    action = action.replace(" ", "_").replace("-", "_")

    if not action:
        action = "NONE"

    if action in FORBIDDEN_REMEDIATION_ACTIONS:
        action = "NONE"

    if action not in ALLOWED_REMEDIATION_ACTIONS:
        action = "NONE"

    step_percent = 0
    if action == "REDUCE_LOAD":
        step_percent = _clamp_int(
            obj.get("step_percent")
            or obj.get("step")
            or obj.get("percent")
            or 10,
            low=1,
            high=100,
            default=10,
        )
    else:
        step_percent = 0

    priority = _clamp_int(
        obj.get("priority") or 100,
        low=1,
        high=1000,
        default=100,
    )

    rationale = _normalize_text(
        obj.get("rationale")
        or obj.get("reason")
        or obj.get("explanation")
        or ""
    )

    if not rationale:
        if action == "NONE":
            rationale = "No bounded autonomous action was recommended by the LLM."
        else:
            rationale = "LLM recommendation accepted for deterministic guard evaluation."

    if primary_eq_id and rationale:
        if primary_eq_id.upper() not in rationale.upper():
            rationale = f"{primary_eq_id}: {rationale}"

    return {
        "recommended_action": action,
        "step_percent": step_percent,
        "priority": priority,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# LLMReasoner
# ---------------------------------------------------------------------------

class LLMReasoner:
    """
    Local-LLM explanation/recommendation layer.

    This class is intentionally conservative:
      - It only speaks if an engine is available.
      - It grounds prompts in deterministic findings/snapshots.
      - It sanitizes all remediation output.
      - It exposes episodic-memory metadata for verification.
      - It enforces focus-equipment attribution.
      - It adds an evidence-consistency guard.
    """

    def __init__(self, engine: Any = None, config: Any = None):
        self.engine = engine
        self.config = config
        self._last_raw: str = ""
        self._last_error: str = ""

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def available(self) -> bool:
        engine = self.engine
        if engine is None:
            return False

        llm = getattr(engine, "llm", None)
        if llm is not None:
            return True

        if hasattr(engine, "create_chat_completion"):
            return True

        if callable(engine):
            return True

        return False

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    def analyze(
        self,
        state: Any,
        mode: Any,
        findings: Iterable[Any],
        snapshots: Iterable[Any],
        prior_incidents: Optional[List[Dict[str, Any]]] = None,
        focus_eq_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Produce a bounded LLM diagnosis/recommendation.

        Returns None if LLM is unavailable or inference fails.
        Never raises.
        """
        if not self.available():
            return None

        try:
            # Resolve primary equipment: prefer explicit focus_eq_id.
            primary_eq_id = str(focus_eq_id or "").strip().upper()

            if not primary_eq_id or primary_eq_id == "SYSTEM":
                primary_eq_id = self._infer_primary_eq_id(findings)

            prompt = self._build_prompt(
                state=state,
                mode=mode,
                findings=findings,
                snapshots=snapshots,
                prior_incidents=prior_incidents,
                focus_eq_id=primary_eq_id,
            )

            raw = self._call_llm(prompt)

            if raw is None:
                return None

            self._last_raw = raw

            parsed = _extract_json_from_text(raw)

            if not isinstance(parsed, dict):
                parsed = _safe_default(
                    raw=raw,
                    rationale="LLM returned unstructured output; defaulted to manual review.",
                )
            else:
                parsed.setdefault("diagnosis", "")
                parsed.setdefault("operator_message", "")
                parsed.setdefault("severity", "low")
                parsed.setdefault("confidence", 0.0)
                parsed.setdefault("recommended_action", "manual_review")
                parsed.setdefault("step_percent", 0)
                parsed.setdefault("priority", 100)
                parsed.setdefault("rationale", "")
                parsed.setdefault("remediation_recommendation", {})

            # Normalize severity/confidence.
            parsed["severity"] = _normalize_severity(parsed.get("severity"))
            parsed["confidence"] = _clamp_float(
                parsed.get("confidence"),
                low=0.0,
                high=1.0,
                default=0.0,
            )

            # ------------------------------------------------------------------
            # EVIDENCE-CONSISTENCY GUARD
            #
            # If the LLM said "no active protection events" or assigned
            # low/medium severity, but the deterministic rule engine just
            # emitted a HIGH/CRITICAL finding, flag the inconsistency and
            # reinforce the operator message from deterministic evidence.
            # ------------------------------------------------------------------
            has_active_serious = False
            top_serious_message = ""
            top_serious_eq = ""

            for f in list(findings or []):
                sev = _normalize_severity(getattr(f, "severity", "low"))
                eq = _normalize_text(getattr(f, "eq_id", "")).upper()

                if sev in {"high", "critical"} and eq and eq != "SYSTEM":
                    has_active_serious = True
                    if not top_serious_message:
                        top_serious_message = _normalize_text(
                            getattr(f, "message", "")
                        )
                        top_serious_eq = eq

            parsed_severity = _normalize_severity(parsed.get("severity"))
            parsed_diag = _normalize_text(parsed.get("diagnosis", "")).lower()
            parsed_msg = _normalize_text(
                parsed.get("operator_message", "")
            ).lower()

            inconsistent = (
                has_active_serious
                and (
                    parsed_severity in {"low", "medium"}
                    or "no active" in parsed_diag
                    or "no protection" in parsed_diag
                    or "no protection" in parsed_msg
                    or "normal" in parsed_diag
                    or "operating normally" in parsed_diag
                )
            )

            if inconsistent:
                parsed["llm_consistency_warning"] = (
                    "LLM output was inconsistent with deterministic HIGH/CRITICAL "
                    "findings. Operator message was reinforced from deterministic "
                    "evidence."
                )

                if top_serious_eq:
                    primary_eq_id = top_serious_eq
                elif primary_eq_id and primary_eq_id != "SYSTEM":
                    top_serious_eq = primary_eq_id

                parsed["primary_eq_id"] = primary_eq_id
                parsed["affected_equipment"] = [primary_eq_id] if primary_eq_id else []

                if top_serious_message:
                    parsed["operator_message"] = (
                        f"{top_serious_eq}: {top_serious_message}"
                        if top_serious_eq
                        else top_serious_message
                    )
                    parsed["diagnosis"] = top_serious_message
                elif primary_eq_id:
                    parsed["operator_message"] = (
                        f"{primary_eq_id}: Protection trip detected. "
                        "Deterministic rule engine confirms active fault; "
                        "LLM output was inconsistent and has been overridden."
                    )
                    parsed["diagnosis"] = parsed["operator_message"]

                parsed["severity"] = "high"
                parsed["confidence"] = min(
                    float(parsed.get("confidence", 0.0) or 0.0),
                    0.25,
                )

            # ------------------------------------------------------------------
            # ATTRIBUTION REINFORCEMENT
            # ------------------------------------------------------------------
            if primary_eq_id:
                parsed["primary_eq_id"] = primary_eq_id
                parsed["affected_equipment"] = [primary_eq_id]

                prefix = f"{primary_eq_id}:"
                for field in ("diagnosis", "operator_message"):
                    val = _normalize_text(parsed.get(field))
                    if val and not val.upper().startswith(primary_eq_id.upper()):
                        parsed[field] = f"{prefix} {val}"
                    elif not val:
                        parsed[field] = (
                            f"{prefix} Review indicated by deterministic findings."
                        )

            # Sanitize remediation recommendation.
            remediation = _sanitize_remediation(
                parsed.get("remediation_recommendation") or parsed,
                primary_eq_id=primary_eq_id,
            )

            parsed["remediation_recommendation"] = remediation
            parsed["recommended_action"] = _normalize_top_action(
                parsed.get("recommended_action")
                or remediation.get("recommended_action")
            )
            parsed["step_percent"] = remediation.get("step_percent", 0)
            parsed["priority"] = remediation.get("priority", 100)
            parsed["rationale"] = remediation.get("rationale") or _normalize_text(
                parsed.get("rationale")
            )

            # Ensure operator-facing message exists.
            if not _normalize_text(parsed.get("operator_message")):
                parsed["operator_message"] = (
                    parsed.get("diagnosis")
                    or "Review recommended."
                )

            # ------------------------------------------------------------------
            # EPISODIC-MEMORY METADATA
            # ------------------------------------------------------------------
            if prior_incidents is not None:
                parsed["prior_incidents_count"] = len(prior_incidents)
                parsed["prior_incidents"] = [
                    {
                        "file": _normalize_text(p.get("file", "")),
                        "stem": _normalize_text(p.get("stem", "")),
                        "severity": _normalize_text(p.get("severity", "")),
                        "equipment": _normalize_text(p.get("equipment", "")),
                        "primary_equipment": _normalize_text(
                            p.get("primary_equipment", "")
                        ),
                    }
                    for p in prior_incidents[:5]
                    if isinstance(p, dict)
                ]
            else:
                parsed["prior_incidents_count"] = 0
                parsed["prior_incidents"] = []

            parsed["source"] = "llm"
            parsed["raw"] = raw

            return parsed

        except Exception as exc:
            self._last_error = str(exc)
            print(f"[LLM_REASONER] analyze failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _infer_primary_eq_id(self, findings: Iterable[Any]) -> str:
        if findings is None:
            return ""

        items = list(findings)

        # First pass: high/critical.
        for f in items:
            eq = _normalize_text(getattr(f, "eq_id", "")).upper()
            sev = _normalize_severity(getattr(f, "severity", "low"))

            if eq and eq != "SYSTEM" and sev in {"high", "critical"}:
                return eq

        # Second pass: any non-system.
        for f in items:
            eq = _normalize_text(getattr(f, "eq_id", "")).upper()
            if eq and eq != "SYSTEM":
                return eq

        return ""

    def _build_prompt(
        self,
        state: Any,
        mode: Any,
        findings: Iterable[Any],
        snapshots: Iterable[Any],
        prior_incidents: Optional[List[Dict[str, Any]]] = None,
        focus_eq_id: Optional[str] = None,
    ) -> str:
        state_text = _normalize_text(state) or "UNKNOWN"
        mode_text = _normalize_text(mode) or "ADVISORY"

        finding_items = list(findings or [])[:12]

        finding_lines: List[str] = []

        for f in finding_items:
            eq = _normalize_text(getattr(f, "eq_id", "")) or "UNKNOWN"
            code = _normalize_text(getattr(f, "code", "")) or "UNKNOWN"
            sev = _normalize_severity(
                getattr(f, "severity", "low")
            ).upper()
            msg = _normalize_text(getattr(f, "message", "")) or ""
            source = _normalize_text(getattr(f, "source", "")) or "rule"
            auto_allowed = getattr(f, "auto_allowed", None)

            evidence = getattr(f, "evidence", {}) or {}
            if not isinstance(evidence, dict):
                evidence = {}

            ansi = (
                evidence.get("ansi_code")
                or evidence.get("ansi_name")
                or ""
            )
            trip = evidence.get("trip_word", "")
            lockout = evidence.get("lockout_status", "")

            finding_lines.append(
                f"- {eq} | {code} | {sev} | source={source} | "
                f"ansi={ansi} | trip={trip} | lockout={lockout} | "
                f"auto_allowed={auto_allowed} | {msg}"
            )

        if not finding_lines:
            finding_lines.append("- No active deterministic findings.")

        # Latest snapshot per equipment.
        latest: Dict[str, Any] = {}

        for s in snapshots or []:
            eq = _normalize_text(getattr(s, "eq_id", ""))
            if eq:
                latest[eq] = s

        snapshot_lines: List[str] = []

        for eq in sorted(latest.keys()):
            s = latest[eq]

            voltage = _normalize_text(getattr(s, "voltage", ""))
            current = _normalize_text(getattr(s, "current", ""))
            temperature = _normalize_text(getattr(s, "temperature", ""))
            load = _normalize_text(getattr(s, "load", ""))
            frequency = _normalize_text(getattr(s, "frequency", ""))
            power_factor = _normalize_text(getattr(s, "power_factor", ""))
            theta = _normalize_text(getattr(s, "theta_per_mille", ""))
            trip = _normalize_text(getattr(s, "trip_word", ""))
            alarm = _normalize_text(getattr(s, "alarm_word", ""))
            lockout = _normalize_text(getattr(s, "lockout_status", ""))
            motor = _normalize_text(getattr(s, "motor_state", ""))

            snapshot_lines.append(
                f"- {eq}: V={voltage}, I={current}, T={temperature}, "
                f"load={load}, f={frequency}, pf={power_factor}, "
                f"theta_permille={theta}, trip={trip}, alarm={alarm}, "
                f"lockout={lockout}, motor_state={motor}"
            )

        if not snapshot_lines:
            snapshot_lines.append("- No recent telemetry snapshots available.")

        prompt_parts: List[str] = []

        prompt_parts.append(
            "You are a cautious industrial AI supervisor for a SCADA plant.\n"
            "Your role is bounded cognitive supervision:\n"
            "  - deterministic rules detect protection events;\n"
            "  - you explain and recommend only;\n"
            "  - you must not command safety systems;\n"
            "  - you must not invent faults unsupported by evidence;\n"
            "  - autonomous actuation is decided later by a deterministic guard.\n"
        )

        # ------------------------------------------------------------------
        # FOCUS-EQUIPMENT INSTRUCTION
        # ------------------------------------------------------------------
        primary_eq_id = str(focus_eq_id or "").strip().upper()

        if primary_eq_id and primary_eq_id != "SYSTEM":
            prompt_parts.append(
                "\n## PRIMARY EQUIPMENT UNDER ANALYSIS\n"
                f"{primary_eq_id}\n\n"
                "The deterministic rule engine has selected this equipment as the "
                "current primary subject. Your diagnosis and operator message must "
                "focus on this equipment unless the evidence clearly identifies "
                "another equipment as the root cause.\n"
                "Do NOT say 'no active protection events detected' if active "
                "HIGH/CRITICAL findings are present in the evidence.\n"
                "Do NOT attribute the fault to a different equipment unless the "
                "evidence explicitly shows a cross-equipment causal chain.\n"
            )

        prompt_parts.append(f"System state: {state_text}")
        prompt_parts.append(f"Operating mode: {mode_text}")

        prompt_parts.append("\n## Active deterministic findings\n")
        prompt_parts.extend(finding_lines)

        prompt_parts.append("\n## Latest telemetry snapshots\n")
        prompt_parts.extend(snapshot_lines)

        if prior_incidents:
            hist_lines: List[str] = [
                "\n## Recent episodic memory (prior Obsidian incidents)\n"
            ]

            for inc in prior_incidents[:5]:
                if not isinstance(inc, dict):
                    continue

                fname = _normalize_text(inc.get("file", "unknown.md"))
                sev = _normalize_text(inc.get("severity", "")).upper() or "UNKNOWN"
                eq = _normalize_text(inc.get("primary_equipment", "")) or _normalize_text(
                    inc.get("equipment", "")
                )
                diag = _normalize_text(inc.get("diagnosis", ""))[:180]

                eq_label = f" [{eq}]" if eq else ""
                hist_lines.append(f"- [{sev}]{eq_label} {fname}: {diag}")

            hist_lines.append(
                "\nInstruction: Reference these past incidents when relevant "
                "to identify recurring patterns.\n"
                "Do NOT repeat them verbatim in your output JSON.\n"
                "Do NOT let a past incident override the current primary "
                "equipment attribution."
            )

            prompt_parts.extend(hist_lines)

        prompt_parts.append(
            "\n## Task\n"
            "Based only on the evidence above, produce a concise diagnosis and "
            "a bounded remediation recommendation.\n"
            "If no anomaly is supported, use severity=low and "
            "recommended_action=manual_review.\n"
        )

        prompt_parts.append(
            "\n## Required JSON schema\n"
            "Respond ONLY with valid JSON matching this shape:\n"
            "{\n"
            '  "diagnosis": "short factual diagnosis",\n'
            '  "operator_message": "short operator-facing message",\n'
            '  "severity": "low|medium|high|critical",\n'
            '  "confidence": 0.0,\n'
            '  "recommended_action": "manual_review|reduce_load|hold_state|request_operator_ack|isolate_non_critical|safe_stop_non_critical|none",\n'
            '  "step_percent": 0,\n'
            '  "priority": 100,\n'
            '  "rationale": "brief evidence-based rationale",\n'
            '  "primary_eq_id": "optional equipment id",\n'
            '  "remediation_recommendation": {\n'
            '    "recommended_action": "REDUCE_LOAD|HOLD_STATE|REQUEST_OPERATOR_ACK|ISOLATE_NON_CRITICAL|SAFE_STOP_NON_CRITICAL|NONE",\n'
            '    "step_percent": 0,\n'
            '    "priority": 100,\n'
            '    "rationale": "brief rationale for autonomous guard evaluation"\n'
            "  }\n"
            "}\n"
        )

        prompt_parts.append(
            "\nSafety constraints:\n"
            "  - Never recommend resetting E-STOP.\n"
            "  - Never recommend bypassing safety interlocks.\n"
            "  - Never recommend coil writes or protection setting changes.\n"
            "  - Only recommend bounded non-safety actions such as load reduction, "
            "hold state, operator acknowledgment, non-critical isolation, or "
            "non-critical safe stop surrogate.\n"
        )

        return "\n".join(prompt_parts)

    def _call_llm(self, prompt: str) -> Optional[str]:
        try:
            engine = self.engine
            if engine is None:
                return None

            llm = getattr(engine, "llm", None)

            # Preferred: llama-cpp-python style chat completion.
            if llm is not None and hasattr(llm, "create_chat_completion"):
                response = llm.create_chat_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a cautious industrial AI supervisor. "
                                "Respond only with valid JSON. Do not command "
                                "safety systems. Ground every statement in the "
                                "provided evidence. Focus on the PRIMARY "
                                "EQUIPMENT UNDER ANALYSIS section."
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                    temperature=0.1,
                    top_p=0.95,
                    max_tokens=512,
                    repeat_penalty=1.1,
                )

                try:
                    content = response["choices"][0]["message"]["content"]
                    if isinstance(content, list):
                        return " ".join(str(x) for x in content)
                    return str(content)
                except Exception:
                    if isinstance(response, str):
                        return response
                    return json.dumps(response, default=str)

            # Fallback: engine itself exposes create_chat_completion.
            if hasattr(engine, "create_chat_completion"):
                response = engine.create_chat_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Respond only with valid JSON. "
                                "Focus on the PRIMARY EQUIPMENT UNDER ANALYSIS."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=512,
                )

                try:
                    content = response["choices"][0]["message"]["content"]
                    if isinstance(content, list):
                        return " ".join(str(x) for x in content)
                    return str(content)
                except Exception:
                    if isinstance(response, str):
                        return response
                    return json.dumps(response, default=str)

            # Fallback: callable engine.
            if callable(engine):
                try:
                    out = engine(
                        prompt,
                        max_new_tokens=512,
                        temperature=0.1,
                        top_p=0.95,
                        repetition_penalty=1.1,
                    )
                except TypeError:
                    out = engine(prompt)

                if isinstance(out, str):
                    return out

                return json.dumps(out, default=str)

            return None

        except Exception as exc:
            self._last_error = str(exc)
            print(f"[LLM_REASONER] inference failed: {exc}")
            return None