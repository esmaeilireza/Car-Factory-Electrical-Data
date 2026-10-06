"""
NEXUS SCADA — Tamper-Evident Audit Logger

Thread-safe version for local prototype / single-backend writer.

Design:
  - module-level threading lock for concurrent threads inside one process
  - before every append, re-read the real last hash from disk
  - append exactly one canonical JSON line
  - flush after write
  - Obsidian promotion is fire-and-forget and runs outside the audit lock
  - explicit equipment attribution is preferred over LLM text mining

Attribution hardening:
  - extracts primary/target/device/eq ids from payload and evidence
  - forces payload-level eq_id to be first when it is a real equipment id
  - passes explicit target_eq_id / primary_eq_id / device_id / ansi_code
    into obsidian_bridge.log_incident
  - avoids SYSTEM pollution when a real equipment id is recoverable
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from obsidian_bridge import log_incident as _obsidian_log_incident

    _OBSIDIAN_AVAILABLE = True
except Exception:
    _OBSIDIAN_AVAILABLE = False
    _obsidian_log_incident = None


# ---------------------------------------------------------------------------
# Module-level shared state and lock
# ---------------------------------------------------------------------------

_audit_lock = threading.Lock()
_last_hash_state: Dict[str, Optional[str]] = {"hash": None}
_chain_initialized = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    """
    Normalize enum-like or scalar text values.
    """
    if value is None:
        return ""

    inner = getattr(value, "value", value)
    text = str(inner).strip()

    # Defensive: strip enum qualified names such as Severity.HIGH.
    if text.upper().startswith("SEVERITY."):
        text = text.split(".", 1)[1]

    return text


def _normalize_eq(value: Any) -> str:
    """
    Normalize an equipment id candidate.
    """
    text = _text(value).upper()

    # Remove common accidental punctuation/wiki-link wrappers.
    text = text.strip("[](){}'\"<>")
    text = text.strip(" \t\r\n.,;:!?")

    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]

    return text.strip()


def _is_real_eq(text: str) -> bool:
    """
    Return True when text is a usable equipment id.
    """
    if not text:
        return False

    bad = {
        "SYSTEM",
        "ALL",
        "EVERYTHING",
        "PLANT",
        "NONE",
        "NULL",
        "UNKNOWN",
        "NA",
        "N/A",
        "",
    }

    return text.upper() not in bad


# ---------------------------------------------------------------------------
# Tail reader: find the real last hash on disk
# ---------------------------------------------------------------------------

def _read_last_hash_from_disk(
    path: Path,
    max_tail_bytes: int = 1_000_000,
) -> Optional[str]:
    """
    Read the last valid JSONL record's hash without loading the whole file.

    Conservative behavior:
      - ignores blank lines
      - ignores malformed lines
      - returns None if no valid hash is found
    """
    try:
        if not path.exists():
            return None

        size = path.stat().st_size
        if size == 0:
            return None

        read_size = min(size, max_tail_bytes)

        with open(path, "rb") as f:
            f.seek(size - read_size)
            data = f.read(read_size)

        lines = data.splitlines()

        # If we did not read from the beginning, the first line may be partial.
        if size > read_size and lines:
            lines = lines[1:]

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

            h = rec.get("hash")
            if isinstance(h, str) and h:
                return h

        return None

    except Exception:
        return None


def _initialize_chain(path: Path) -> str:
    """
    Initialize shared chain state from disk.

    Every later write still re-reads the disk head under the lock, so this is
    only a bootstrap.
    """
    global _chain_initialized

    with _audit_lock:
        if _chain_initialized and _last_hash_state["hash"] is not None:
            return str(_last_hash_state["hash"])

        head = _read_last_hash_from_disk(path) or "GENESIS"
        _last_hash_state["hash"] = head
        _chain_initialized = True
        return head


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------

class AuditLogger:
    """
    Thread-safe, tamper-evident JSONL audit logger.
    """

    def __init__(self, path: Optional[str] = None):
        if path is None:
            project_root = Path(__file__).resolve().parents[2]
            self.path = project_root / "data" / "agent_audit.jsonl"
        else:
            self.path = Path(path)

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Bootstrap local view. Actual writes still re-read disk head.
        self.prev_hash = _initialize_chain(self.path)

    def log(self, event: str, payload: Dict[str, Any]) -> None:
        """
        Append one tamper-evident audit record.

        Protocol:
          1. Take thread lock.
          2. Read actual last hash from disk.
          3. Build record with prev_hash = actual disk head.
          4. Hash canonical body.
          5. Append one line.
          6. Flush.
          7. Update shared state.
          8. Outside lock, optionally promote to Obsidian.
        """
        payload = payload if isinstance(payload, dict) else {"value": payload}

        try:
            with _audit_lock:
                disk_head = _read_last_hash_from_disk(self.path) or "GENESIS"

                record: Dict[str, Any] = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "event": event,
                    "payload": payload,
                    "prev_hash": disk_head,
                }

                body = {k: v for k, v in record.items() if k != "hash"}
                serialized_body = json.dumps(
                    body,
                    sort_keys=True,
                    default=str,
                )

                record_hash = hashlib.sha256(
                    serialized_body.encode("utf-8")
                ).hexdigest()

                record["hash"] = record_hash

                line = json.dumps(record, default=str) + "\n"

                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()

                _last_hash_state["hash"] = record_hash
                self.prev_hash = record_hash

        except Exception as exc:
            print(f"[AUDIT ERROR] write failed for event '{event}': {exc}")
            return

        # Fire-and-forget Obsidian promotion.
        # This is deliberately outside the audit lock so vault I/O cannot block
        # or corrupt the deterministic audit chain.
        try:
            self._promote_to_obsidian(event, payload)
        except Exception:
            pass

    def _promote_to_obsidian(self, event: str, payload: Dict[str, Any]) -> None:
        """
        Promote high-severity findings into the Obsidian vault.

        Attribution priority:
          1. payload-level eq_id forced first when real
          2. explicit device_id / target_eq_id / primary_eq_id / eq_id
          3. evidence-level primary_eq_id / target_eq_id / device_id / eq_id
          4. affected_equipment / equipment list
          5. obsidian_bridge text mining fallback

        This prevents UTI-01 LLM text mentioning STP-01 from creating a
        STP-01-named incident.
        """
        if not _OBSIDIAN_AVAILABLE or _obsidian_log_incident is None:
            return

        severity = _text(payload.get("severity")).lower()

        try:
            safety_level = int(payload.get("safety_level", 1) or 1)
        except Exception:
            safety_level = 1

        if severity not in ("high", "critical") and safety_level < 3:
            return

        evidence = payload.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}

        preferred: List[str] = []

        def add_preferred(value: Any) -> None:
            if value is None:
                return

            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add_preferred(item)
                return

            if isinstance(value, dict):
                # Prefer common equipment-bearing keys if present.
                for key in (
                    "eq_id",
                    "equipment_id",
                    "device_id",
                    "machine_id",
                    "asset_id",
                    "primary_eq_id",
                    "target_eq_id",
                ):
                    if key in value:
                        add_preferred(value[key])
                return

            text = _normalize_eq(value)

            if not _is_real_eq(text):
                return

            if text not in preferred:
                preferred.append(text)

        # Strong explicit attribution fields.
        for key in (
            "device_id",
            "target_eq_id",
            "primary_eq_id",
            "eq_id",
            "equipment_id",
            "machine_id",
            "asset_id",
        ):
            add_preferred(payload.get(key))
            add_preferred(evidence.get(key))

        # Secondary equipment list.
        equipment_ids_raw = (
            payload.get("affected_equipment")
            or payload.get("equipment")
            or []
        )

        secondary: List[str] = []

        def add_secondary(value: Any) -> None:
            if value is None:
                return

            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add_secondary(item)
                return

            if isinstance(value, dict):
                for key in (
                    "eq_id",
                    "equipment_id",
                    "device_id",
                    "machine_id",
                    "asset_id",
                    "primary_eq_id",
                    "target_eq_id",
                ):
                    if key in value:
                        add_secondary(value[key])
                return

            text = _normalize_eq(value)

            if not _is_real_eq(text):
                return

            if text not in secondary:
                secondary.append(text)

        add_secondary(equipment_ids_raw)

        ordered: List[str] = []

        for item in preferred + secondary:
            text = str(item).strip().upper()

            if not _is_real_eq(text):
                continue

            if text not in ordered:
                ordered.append(text)

        # Force payload-level eq_id to be first when it is a real equipment id.
        payload_eq = _normalize_eq(payload.get("eq_id"))

        if _is_real_eq(payload_eq):
            if payload_eq in ordered:
                ordered.remove(payload_eq)
            ordered.insert(0, payload_eq)

        equipment_ids = ordered

        # Extract ANSI code from explicit metadata first.
        ansi_raw = ""

        for source in (evidence, payload):
            if not isinstance(source, dict):
                continue

            for key in (
                "ansi_code",
                "ansi",
                "device_number",
                "relay_code",
                "protection_code",
            ):
                value = source.get(key)
                text = _text(value)
                digits = "".join(ch for ch in text if ch.isdigit())

                if digits:
                    ansi_raw = digits
                    break

            if ansi_raw:
                break

        # Fallback: parse ANSI-xx from iec_reference / ansi_name / code.
        if not ansi_raw:
            blob = " ".join(
                [
                    _text(payload.get("iec_reference")),
                    _text(evidence.get("ansi_name")),
                    _text(payload.get("code")),
                ]
            )

            m = re.search(r"ANSI[-_ ]?(\d{2,3})", blob, re.IGNORECASE)
            if m:
                ansi_raw = m.group(1)

        if ansi_raw:
            fault_type = f"ANSI-{ansi_raw}"
        else:
            fault_type = (
                _text(payload.get("iec_reference"))
                or _text(evidence.get("ansi_name"))
                or _text(payload.get("code"))
                or "Unclassified"
            )

        diagnosis = (
            _text(payload.get("diagnosis"))
            or _text(payload.get("message"))
            or event
        )

        # Reinforce attribution in the diagnosis text so downstream fallbacks
        # have a better chance of selecting the correct equipment.
        if equipment_ids and diagnosis:
            first = equipment_ids[0]
            if not diagnosis.upper().startswith(first.upper()):
                diagnosis = f"{first}: {diagnosis}"

        recommended_action = (
            _text(payload.get("recommended_action"))
            or _text(payload.get("suggested_action"))
            or _text(payload.get("action"))
            or "manual_review"
        )

        obsidian_kwargs: Dict[str, Any] = {}

        if equipment_ids:
            obsidian_kwargs["target_eq_id"] = equipment_ids[0]
            obsidian_kwargs["primary_eq_id"] = equipment_ids[0]
            obsidian_kwargs["device_id"] = equipment_ids[0]

        if ansi_raw:
            obsidian_kwargs["ansi_code"] = ansi_raw

        try:
            _obsidian_log_incident(
                equipment_ids=equipment_ids,
                fault_type=str(fault_type),
                diagnosis=str(diagnosis),
                recommended_action=str(recommended_action),
                severity=severity.upper(),
                source=str(payload.get("source", "agent_audit")),
                **obsidian_kwargs,
            )
        except Exception:
            pass