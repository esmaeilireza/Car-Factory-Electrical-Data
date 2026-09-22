"""
AI Diagnosis Engine for NEXUS SCADA
Local LLM fault detection and auto-recovery via llama-cpp-python
(with ctransformers as legacy fallback).

[DEBUG BUILD]: Full import/load diagnostics enabled.
Set SCADA_AI_VERBOSE=1 to see llama.cpp internal logs.
"""
import json
import os
import sys
import time
import hashlib
import traceback
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional

import pandas as pd

# ==============================================================================
# Library detection — WITH FULL DIAGNOSTICS
# ==============================================================================

CTRANSFORMERS_AVAILABLE = False
LLAMA_CPP_AVAILABLE = False
Llama = None
AutoModelForCausalLM = None


def _debug_env() -> None:
    """Print which interpreter and paths are in play (debug aid)."""
    print(f"[AI DEBUG] interpreter : {sys.executable}")
    print(f"[AI DEBUG] venv prefix : {getattr(sys, 'prefix', '?')}")
    sp = [p for p in sys.path if 'site-packages' in p.lower()]
    print(f"[AI DEBUG] site-packages on path: {sp}")


_debug_env()

# --- Attempt 1: llama-cpp-python (CORRECT choice for Qwen2) ---
try:
    import llama_cpp
    from llama_cpp import Llama as _Llama
    Llama = _Llama
    LLAMA_CPP_AVAILABLE = True
    print(f"[AI DEBUG] llama_cpp imported OK")
    print(f"[AI DEBUG] llama_cpp version  : {llama_cpp.__version__}")
    print(f"[AI DEBUG] llama_cpp location : {llama_cpp.__file__}")
except ImportError as e:
    # [DEBUG] Print the REAL reason instead of swallowing it
    print(f"[AI DEBUG] llama-cpp-python import FAILED. Reason: {e!r}")
    traceback.print_exc()
    print("[AI] llama-cpp-python not available, trying ctransformers...")

    # --- Attempt 2: ctransformers (legacy fallback) ---
    try:
        from ctransformers import AutoModelForCausalLM as _AutoModel
        AutoModelForCausalLM = _AutoModel
        CTRANSFORMERS_AVAILABLE = True
        print("[AI] ctransformers available (NOTE: cannot load Qwen2 architecture!)")
    except ImportError as e2:
        print("=" * 60)
        print("ERROR: Neither llama-cpp-python nor ctransformers installed")
        print(f"[AI DEBUG] ctransformers import failed. Reason: {e2!r}")
        print("Install:  pip install llama-cpp-python")
        print("=" * 60)


class AIDiagnosisEngine:
    """Local AI engine for SCADA fault detection and auto-recovery."""

    SAFETY_LEVELS = {
        "monitoring": 1,
        "alerting": 2,
        "safe_recovery": 3,
        "critical": 4,
    }

    SAFE_ACTION_REGISTRY = {
        "reconnect_modbus": "Reconnect Modbus TCP connection",
        "flush_session_cache": "Flush session cache and resync data",
        "restart_poller": "Restart the data polling loop",
        "reset_protection": "Reset protection relays and clear trips",
    }

    def __init__(self, model_path: Optional[str] = None, n_ctx: int = 4096, n_threads: int = 4):
        """
        Initialize the AI diagnosis engine.

        Args:
            model_path: Path to qwen2.5-coder-1.5b-instruct-q6_k.gguf
            n_ctx: Context window size (4096 recommended for 1.5B model)
            n_threads: CPU threads for inference
        """
        if not LLAMA_CPP_AVAILABLE and not CTRANSFORMERS_AVAILABLE:
            raise ImportError(
                "No AI library available.\n"
                "Install one of these:\n"
                "  pip install llama-cpp-python\n"
                "  pip install ctransformers"
            )

        # ------------------------------------------------------------
        # Anchor path to file, not CWD + environment override
        # ------------------------------------------------------------
        PROJECT_ROOT = Path(__file__).resolve().parent.parent
        DEFAULT_MODEL = PROJECT_ROOT / "models" / "qwen2.5-coder-1.5b-instruct-q6_k.gguf"
        MODEL_PATH = Path(os.getenv("SCADA_MODEL_PATH", str(DEFAULT_MODEL)))

        if model_path is not None:
            MODEL_PATH = Path(model_path)

        if not MODEL_PATH.is_file():
            raise FileNotFoundError(
                f"Model file not found at: {MODEL_PATH}\n"
                f"Project root resolved to: {PROJECT_ROOT}\n"
                f"Expected location: {DEFAULT_MODEL}\n"
                "You can override with environment variable SCADA_MODEL_PATH.\n"
                "Download from: https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF\n"
                "File: qwen2.5-coder-1.5b-instruct-q6_k.gguf"
            )

        size_mb = MODEL_PATH.stat().st_size / (1024 * 1024)
        print(f"[AI] Loading model from {MODEL_PATH} ({size_mb:.0f} MB)...")
        start_time = time.time()

        self.backend = None
        self.llm = None

        # ------------------------------------------------------------
        # Attempt 1: llama-cpp-python (supports Qwen2 natively)
        # ------------------------------------------------------------
        if LLAMA_CPP_AVAILABLE:
            # [DEBUG] verbose can be enabled via env var
            verbose = os.getenv("SCADA_AI_VERBOSE", "0") == "1"
            if verbose:
                print("[AI DEBUG] SCADA_AI_VERBOSE=1 -> llama.cpp internal logs ON")

            try:
                print("[AI] Trying llama-cpp-python (CPU-only mode)... this may take 15-40s")
                self.llm = Llama(
                    model_path=str(MODEL_PATH),
                    n_ctx=n_ctx,
                    n_threads=n_threads,
                    n_gpu_layers=0,     # CPU only
                    verbose=verbose,    # [DEBUG] internal llama.cpp logs
                )
                self.backend = "llama-cpp"
                print("[AI] llama-cpp-python loaded successfully (CPU-only)")
            except Exception as e:
                # [DEBUG] full traceback, not one line
                print(f"[AI] WARNING: llama-cpp-python failed to load: {e}")
                traceback.print_exc()
                print("[AI] Falling back to ctransformers...")
                self.llm = None
                self.backend = None

        # ------------------------------------------------------------
        # Attempt 2: ctransformers (legacy fallback)
        # WARNING: ctransformers does NOT support the Qwen2 architecture.
        # This fallback exists only for older GGUF models.
        # ------------------------------------------------------------
        if self.llm is None and CTRANSFORMERS_AVAILABLE:
            model_types_to_try = ['mistral', 'llama', 'qwen', 'gpt2', 'gptj']
            last_error = None

            for model_type in model_types_to_try:
                try:
                    print(f"[AI] Trying ctransformers with model_type='{model_type}'...")
                    self.llm = AutoModelForCausalLM.from_pretrained(
                        str(MODEL_PATH.parent),
                        model_file=MODEL_PATH.name,
                        model_type=model_type,
                        gpu_layers=0,
                        context_length=n_ctx,
                        threads=n_threads,
                    )
                    self.backend = f"ctransformers-{model_type}"
                    print(f"[AI] ctransformers loaded successfully with model_type='{model_type}'")
                    break
                except Exception as e:
                    print(f"[AI] model_type='{model_type}' failed: {e}")
                    last_error = e
                    self.llm = None
                    continue

            if self.llm is None:
                print(f"[AI] ERROR: All ctransformers model_types failed. Last error: {last_error}")
                raise RuntimeError(
                    f"Failed to load model with ctransformers.\n"
                    f"Tried model_types: {model_types_to_try}\n"
                    f"Last error: {last_error}\n"
                    f"\nNOTE: ctransformers cannot load Qwen2 architecture models.\n"
                    f"Fix llama-cpp-python instead: pip install llama-cpp-python"
                )

        if self.llm is None:
            raise RuntimeError(
                "Failed to load model with both llama-cpp and ctransformers.\n"
                "Run with SCADA_AI_VERBOSE=1 and check the [AI DEBUG] lines above."
            )

        load_time = time.time() - start_time
        print(f"[AI] Model loaded in {load_time:.2f}s using {self.backend}")

        # ------------------------------------------------------------
        # Prompt files
        # ------------------------------------------------------------
        self.prompt_dir = Path(__file__).parent / "prompt"

        self.system_prompt = self._load_prompt("SYSTEM_PROMPT.md")
        self.failure_modes = self._load_prompt("FAILURE_MODES.md")

        self._safety_protocols = self._load_prompt("SAFETY_PROTOCOLS.md")
        self._action_hierarchy = self._load_prompt("ACTION_HIERARCHY.md")
        self._consent_logic = self._load_prompt("CONSENT_GATE_LOGIC.md")
        self._audit_schema = self._load_prompt("AUDIT_LOGGING.md")

        # Audit log path
        self.audit_log_path = Path(__file__).parent.parent / "data" / "audit_log.jsonl"
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

        print("[AI] AI Diagnosis Engine initialized successfully")

    def _load_prompt(self, filename: str) -> str:
        """Load a prompt file from the prompts directory."""
        prompt_path = self.prompt_dir / filename
        if prompt_path.exists():
            content = prompt_path.read_text(encoding="utf-8")
            print(f"[AI] Loaded prompt: {filename}")
            return content
        else:
            print(f"[AI] Warning: {filename} not found at {prompt_path}")
            return ""

    # ============================================================
    # Rule-based pre-check for fast, deterministic responses
    # ============================================================
    def _rule_based_precheck(self, session_state: Dict, traffic_log: List,
                             modbus_errors: List, recent_data: pd.DataFrame) -> Optional[Dict]:
        """Fast rule-based checks that don't need AI. Handles 80% of common cases in <1ms."""
        if not session_state.get('connected', True):
            return {
                "diagnosis": "Modbus TCP connection lost",
                "root_cause": "TCP socket disconnected - server may be offline or network issue",
                "severity": "high", "safety_level": 3,
                "recommended_action": "reconnect_modbus", "confidence": 1.0,
                "human_approval_required": False, "affected_equipment": [],
                "iec_reference": "IEC 62443-3-3 SR 1.1", "source": "rule_based",
            }

        if session_state.get('estop_active', False):
            return {
                "diagnosis": "E-STOP emergency stop is ACTIVE",
                "root_cause": "Emergency stop button was pressed or safety interlock triggered",
                "severity": "critical", "safety_level": 4,
                "recommended_action": "Manual reset required via physical RESET button (CO[7])",
                "confidence": 1.0, "human_approval_required": True,
                "affected_equipment": ["ALL"], "iec_reference": "IEC 60255-1",
                "source": "rule_based",
            }

        if session_state.get('any_trip_active', False):
            return {
                "diagnosis": "Protection relay trip detected",
                "root_cause": "One or more equipment protection relays have tripped (ANSI 49/50/51)",
                "severity": "high", "safety_level": 3,
                "recommended_action": "reset_protection", "confidence": 1.0,
                "human_approval_required": True, "affected_equipment": [],
                "iec_reference": "ANSI C37.90", "source": "rule_based",
            }

        if recent_data is not None and not recent_data.empty:
            try:
                last_update = pd.to_datetime(recent_data['timestamp']).max()
                age_seconds = (pd.Timestamp.now() - last_update).total_seconds()
                if age_seconds > 10:
                    return {
                        "diagnosis": f"Stale data detected - {age_seconds:.1f}s old",
                        "root_cause": "Data feed is not updating - possible server CPU starvation",
                        "severity": "medium", "safety_level": 2,
                        "recommended_action": "flush_session_cache", "confidence": 1.0,
                        "human_approval_required": False, "affected_equipment": [],
                        "iec_reference": "IEC 61131-2", "source": "rule_based",
                    }
            except Exception:
                pass

        if modbus_errors and len(modbus_errors) >= 3:
            error_count = len([e for e in modbus_errors[-5:]])
            if error_count >= 3:
                return {
                    "diagnosis": f"Multiple Modbus communication failures ({error_count} in last 5 reads)",
                    "root_cause": "Server starvation or network instability - blocking HTTP calls in async loop",
                    "severity": "high", "safety_level": 3,
                    "recommended_action": "reconnect_modbus", "confidence": 1.0,
                    "human_approval_required": False,
                    "affected_equipment": list(set(e.get('eq', 'UNKNOWN') for e in modbus_errors[-5:])),
                    "iec_reference": "IEC 62443-3-3 SR 1.1", "source": "rule_based",
                }

        return None

    def analyze_system_state(self, session_state: Dict, traffic_log: List,
                             modbus_errors: List, recent_data: pd.DataFrame) -> Dict:
        """Analyze current system state and identify issues."""
        rule_result = self._rule_based_precheck(session_state, traffic_log,
                                                modbus_errors, recent_data)
        if rule_result is not None:
            print(f"[AI] Rule-based diagnosis: {rule_result['diagnosis']}")
            self._log_audit("RULE_BASED_DIAGNOSIS", rule_result)
            return rule_result

        context = self._build_context(session_state, traffic_log,
                                      modbus_errors, recent_data)

        full_prompt = f"""{self.system_prompt}

{self.failure_modes}

---

## CURRENT SYSTEM CONTEXT

{context}

---

## YOUR TASK

Based ONLY on the facts in CURRENT SYSTEM CONTEXT above, provide a diagnosis.
If no anomaly is detected, report "System operating normally" with severity "low".
Do NOT invent problems that are not supported by the context data.

Respond ONLY with valid JSON matching the schema defined in SYSTEM_PROMPT.
"""

        try:
            t0 = time.time()
            if self.backend == "llama-cpp":
                response = self.llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": full_prompt},
                        {"role": "user", "content": "Analyze system state. Be factual. Report only issues supported by evidence."}
                    ],
                    temperature=0.05,
                    top_p=0.95,
                    repeat_penalty=1.1,
                    max_tokens=512,
                )
                ai_response = response['choices'][0]['message']['content']
            else:
                ai_response = self.llm(
                    full_prompt,
                    max_new_tokens=512,
                    temperature=0.05,
                    top_k=40,
                    top_p=0.95,
                    repetition_penalty=1.1,
                )
            print(f"[AI] Inference completed in {time.time() - t0:.2f}s")

            try:
                diagnosis = self._extract_json(ai_response)
                diagnosis['source'] = 'ai'

                if diagnosis.get('confidence', 0) < 0.5:
                    print(f"[AI] WARNING: Low confidence diagnosis ({diagnosis.get('confidence'):.2f}), treating as monitoring only")
                    diagnosis['safety_level'] = 1
                    diagnosis['human_approval_required'] = False

                self._log_audit("AI_DIAGNOSIS", diagnosis)
                return diagnosis
            except Exception as e:
                error_diagnosis = {
                    "diagnosis": "AI analysis failed", "root_cause": str(e),
                    "severity": "low", "safety_level": 1,
                    "recommended_action": "Manual inspection required",
                    "confidence": 0.0, "human_approval_required": False,
                    "affected_equipment": [], "iec_reference": "N/A", "source": "error",
                }
                self._log_audit("AI_ERROR", error_diagnosis)
                return error_diagnosis

        except Exception as e:
            traceback.print_exc()
            error_diagnosis = {
                "diagnosis": "AI inference failed", "root_cause": str(e),
                "severity": "low", "safety_level": 1,
                "recommended_action": "Manual inspection required",
                "confidence": 0.0, "human_approval_required": False,
                "affected_equipment": [], "iec_reference": "N/A", "source": "error",
            }
            self._log_audit("AI_ERROR", error_diagnosis)
            return error_diagnosis

    def _build_context(self, session_state: Dict, traffic_log: List,
                       modbus_errors: List, recent_data: pd.DataFrame) -> str:
        """Build context string for AI analysis."""
        context = []
        context.append(f"Connection Status: {'Connected' if session_state.get('connected') else 'Disconnected'}")

        if modbus_errors:
            error_summary = "\n".join([
                f"  - [{e.get('time', 'N/A')}] {e.get('eq', 'N/A')}: {e.get('error', 'Unknown error')}"
                for e in modbus_errors[-5:]
            ])
            context.append(f"Recent Modbus Errors:\n{error_summary}")

        if traffic_log:
            recent_traffic = traffic_log[-10:]
            context.append(f"Recent Traffic: {len(recent_traffic)} transactions in last window")

        if recent_data is not None and not recent_data.empty:
            try:
                last_update = pd.to_datetime(recent_data['timestamp']).max()
                age_seconds = (pd.Timestamp.now() - last_update).total_seconds()
                context.append(f"Data Age: {age_seconds:.1f} seconds")

                if 'heartbeat' in recent_data.columns and len(recent_data) > 1:
                    hb_variance = recent_data['heartbeat'].std()
                    if pd.notna(hb_variance) and hb_variance > 10:
                        context.append(f"WARNING: Heartbeat variance: {hb_variance:.1f} (abnormal)")
            except Exception:
                pass

        if session_state.get('estop_active'):
            context.append("E-STOP ACTIVE")
        if session_state.get('any_trip_active'):
            context.append("Equipment trip detected")

        return "\n".join(context)

    def _extract_json(self, text: str) -> Dict:
        """Extract JSON from AI response."""
        import re
        code_block = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if code_block:
            try:
                return json.loads(code_block.group(1))
            except Exception:
                pass

        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except Exception:
                pass

        return {
            "diagnosis": "Unable to parse AI response",
            "root_cause": "JSON parsing failed",
            "severity": "low", "safety_level": 1,
            "recommended_action": "Manual review required",
            "confidence": 0.0, "human_approval_required": False,
            "affected_equipment": [], "iec_reference": "N/A",
        }

    def _log_audit(self, event: str, diagnosis: Dict):
        """Log AI decision to audit log."""
        log_entry = {
            "ts": datetime.now().isoformat(),
            "source": diagnosis.get('source', 'ai_engine'),
            "event": event,
            "safety_level": diagnosis.get("safety_level", 1),
            "equipment": diagnosis.get("affected_equipment", []),
            "diagnosis": diagnosis.get("diagnosis", ""),
            "diagnosis_hash": hashlib.sha256(
                json.dumps(diagnosis, sort_keys=True).encode()
            ).hexdigest(),
            "recommended_action": diagnosis.get("recommended_action", ""),
            "executed": False,
            "confidence": diagnosis.get("confidence", 0.0),
            "operator_id": None,
            "iec_reference": diagnosis.get("iec_reference", "N/A"),
        }
        with open(self.audit_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

    def execute_safe_action(self, action: str, safety_level: int) -> Tuple[bool, str]:
        """Validate and approve a recommended action if it's safe for auto-execution."""
        if not action or not action.strip():
            return False, "No action provided to execute."

        if safety_level >= 4:
            return False, "Critical action requires human approval (Safety Level >= 4)."

        normalized_action = action.strip().lower().replace("()", "").replace(" ", "_")

        is_safe = (
            normalized_action in self.SAFE_ACTION_REGISTRY or
            any(keyword in normalized_action for keyword in ["reconnect", "flush", "reset", "restart", "clear"])
        )

        if is_safe:
            return True, f"Action '{action}' validated and approved for execution (Safety Level {safety_level})."

        return False, f"Action '{action}' is not recognized as a safe auto-execution command."


# ==============================================================================
# Module-level compatibility shims for streamlit_app.py
# ==============================================================================
_engine_instance: Optional[AIDiagnosisEngine] = None


def get_ai_engine(model_path: Optional[str] = None) -> AIDiagnosisEngine:
    """Get or create the singleton AI Diagnosis Engine instance."""
    global _engine_instance
    if _engine_instance is None:
        print("[AI] Initializing singleton AIDiagnosisEngine...")
        _engine_instance = AIDiagnosisEngine(model_path=model_path)
    return _engine_instance


def load_model(model_path: Optional[str] = None) -> AIDiagnosisEngine:
    """Legacy compatibility function for streamlit_app.py imports."""
    return get_ai_engine(model_path)