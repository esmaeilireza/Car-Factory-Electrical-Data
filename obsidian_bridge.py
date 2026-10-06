"""
Obsidian Bridge - file-based incident knowledge base for NEXUS SCADA.

Every high-severity diagnosis can be persisted as a markdown file in a
local Obsidian vault, giving operators a human-readable, linkable,
fully-offline incident history (IEC 62443-friendly: no cloud, no DB).

Two responsibilities:

  1. ``log_incident(...)``
       Write a *new* incident note when the agent produces a finding.

  2. ``append_remediation_section(...)``
       When the remediation engine later acts on that finding, append a
       structured "Autonomous Remediation" section to the *same* note so
       the file becomes a complete outcome record (diagnosis -> decision
       -> execution result).

Design rules:
- fire-and-forget: any failure inside must NEVER propagate to the agent
- rotation: Incidents/ is capped, oldest files archive automatically
- idempotent: appending the same remediation twice does not duplicate
- no dependencies beyond the standard library
- LINKED: every incident note always contains at least one [[wiki-link]]
- ATTRIBUTED: explicit equipment ids are preferred over LLM text mining
- STRICT LOOKUP: remediation append must not attach to a wrong-device note
  merely because the LLM prose mentions that device
- SELF-HEALING: if remediation needs an incident for UTI-01 but none exists,
  create a correctly attributed UTI-01 incident before appending
- EPISODIC: new incidents reference recent prior incidents when available
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VAULT_DIR = Path(__file__).resolve().parent / "data" / "scada_vault"
INCIDENTS_DIR = VAULT_DIR / "Incidents"
ARCHIVE_DIR = INCIDENTS_DIR / "archive"
ROTATION_LIMIT = 500

# How many files to scan when falling back to strict content search.
# Filename glob is the primary lookup path; this is a bounded fallback.
_CONTENT_SCAN_LIMIT = 50

# Marker used to prevent duplicate remediation sections.
_REMEDIATION_HEADER = "## Autonomous Remediation"

# Known equipment ids. SYSTEM-level findings carry no machine attribution,
# so the diagnosis text is mined for these ids to keep the knowledge graph
# connected (every incident ends up with at least one resolvable link).
KNOWN_EQ_IDS = [
    "STP-01",
    "WLD-01",
    "PNT-01",
    "ASM-01",
    "UTI-01",
    "UTI-02",
]

# Serialize vault writes to reduce duplicate/race-created incident files
# during rapid fault-injection sweeps.
_INCIDENT_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _enum_value(value: Any) -> Any:
    """
    Return the underlying value for enums or simple scalars.
    """
    return getattr(value, "value", value)


def _normalize_text(value: Any) -> str:
    """
    Normalize enum-like or scalar text values.
    """
    if value is None:
        return ""

    inner = _enum_value(value)
    text = str(inner).strip()

    # Defensive: strip enum qualified names such as Severity.HIGH.
    if text.upper().startswith("SEVERITY."):
        text = text.split(".", 1)[1]

    return text


def _normalize_eq(value: Any) -> str:
    """
    Normalize an equipment id candidate.
    """
    text = _normalize_text(value).upper()

    # Remove common accidental punctuation.
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


def _iter_values(value: Any) -> Iterable[Any]:
    """
    Flatten lists/tuples/sets/dicts into individual values.
    """
    if value is None:
        return

    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_values(item)
        return

    if isinstance(value, dict):
        # Prefer common equipment-bearing keys if present.
        preferred_keys = (
            "eq_id",
            "equipment_id",
            "device_id",
            "machine_id",
            "asset_id",
            "primary_eq_id",
            "target_eq_id",
        )

        yielded = False
        for key in preferred_keys:
            if key in value:
                yielded = True
                yield from _iter_values(value[key])

        if not yielded:
            for item in value.values():
                yield from _iter_values(item)

        return

    yield value


def _safe_mtime(p: Path) -> float:
    """
    Mtime that survives files vanishing mid-iteration.
    """
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Attribution helpers
# ---------------------------------------------------------------------------

def _collect_explicit_ids(
    equipment_ids: Any,
    kwargs: Dict[str, Any],
) -> List[str]:
    """
    Collect explicit equipment IDs from kwargs and equipment_ids.

    Priority:
      1. target_eq_id
      2. primary_eq_id
      3. device_id / eq_id / equipment_id / machine_id / asset_id
      4. equipment_ids argument
    """
    ids: List[str] = []

    def add(value: Any) -> None:
        for item in _iter_values(value):
            text = _normalize_eq(item)

            if not _is_real_eq(text):
                continue

            if text not in ids:
                ids.append(text)

    # Strong explicit attribution fields.
    for key in (
        "target_eq_id",
        "primary_eq_id",
        "device_id",
        "eq_id",
        "equipment_id",
        "machine_id",
        "asset_id",
    ):
        if key in kwargs:
            add(kwargs.get(key))

    # Generic equipment list argument.
    add(equipment_ids)

    return ids


def _mine_referenced_ids(
    fault_type: str,
    diagnosis: str,
    recommended_action: str,
    source: str,
) -> List[str]:
    """
    Fallback text mining for known equipment IDs.

    This is intentionally scored so metadata such as fault_type/source wins
    over casual mentions in LLM prose.
    """
    haystack_diag = str(diagnosis or "").upper()
    haystack_action = str(recommended_action or "").upper()
    haystack_fault = str(fault_type or "").upper()
    haystack_source = str(source or "").upper()

    scored: List[Tuple[int, str]] = []

    for eq in KNOWN_EQ_IDS:
        score = 0

        if eq in haystack_diag:
            score += 10

        if eq in haystack_action:
            score += 8

        if eq in haystack_fault:
            score += 50

        if eq in haystack_source:
            score += 30

        if score:
            scored.append((score, eq))

    scored.sort(key=lambda x: (-x[0], KNOWN_EQ_IDS.index(x[1])))
    return [eq for _, eq in scored]


def _extract_ansi_code(
    fault_type: str,
    diagnosis: str,
    recommended_action: str,
    kwargs: Dict[str, Any],
) -> str:
    """
    Extract a plausible ANSI device number.

    Priority:
      1. explicit kwargs ansi_code / ansi / device_number / relay_code
      2. explicit ANSI-xx pattern in text
      3. standalone two-digit number fallback
    """
    # Explicit metadata first.
    for key in (
        "ansi_code",
        "ansi",
        "device_number",
        "relay_code",
        "protection_code",
    ):
        if key in kwargs:
            text = _normalize_text(kwargs.get(key))
            if text:
                digits = "".join(ch for ch in text if ch.isdigit())
                if digits:
                    return digits

    blob = f"{fault_type} {diagnosis} {recommended_action}"

    # Explicit ANSI style.
    m = re.search(r"ANSI[-_ ]?(\d{2,3})", blob, re.IGNORECASE)
    if m:
        return m.group(1)

    # Fallback: standalone two-digit number.
    # Avoid three-digit fallback because PLC test text often contains HR[150].
    m = re.search(r"(?<!\d)(\d{2})(?!\d)", blob)
    if m:
        return m.group(1)

    return ""


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _rotate_if_needed() -> None:
    """
    Move oldest incident files to archive when the cap is exceeded.
    """
    if not INCIDENTS_DIR.exists():
        return

    incidents = [p for p in INCIDENTS_DIR.glob("*.md") if p.is_file()]
    if len(incidents) <= ROTATION_LIMIT:
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    incidents.sort(key=_safe_mtime)
    overflow = len(incidents) - ROTATION_LIMIT

    for p in incidents[:overflow]:
        try:
            shutil.move(str(p), str(ARCHIVE_DIR / p.name))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def _read_primary_eq(path: Path) -> str:
    """
    Read primary_equipment from incident frontmatter.
    """
    try:
        in_fm = False

        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()

            if line == "---":
                if not in_fm:
                    in_fm = True
                    continue
                else:
                    break

            if not in_fm:
                continue

            lower = line.lower()

            if lower.startswith("primary_equipment:"):
                value = line.split(":", 1)[1].strip().upper()
                value = value.strip("[](){}'\"<>")
                value = value.strip(" \t\r\n.,;:!?")

                if value.startswith("[[") and value.endswith("]]"):
                    value = value[2:-2]

                return value.strip()

        return ""

    except Exception:
        return ""


def _strict_incident_matches(text: str, eq_upper: str) -> bool:
    """
    Strict attribution matcher.

    Only frontmatter signals are accepted:
      - primary_equipment: UTI-01
      - equipment: [[UTI-01]]

    Casual mentions in LLM prose are intentionally ignored.
    """
    in_fm = False

    for raw in text.splitlines():
        line = raw.strip()

        if line == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                break

        if not in_fm:
            continue

        lower = line.lower()

        if lower.startswith("primary_equipment:"):
            value = line.split(":", 1)[1].strip().upper()
            value = value.strip("[](){}'\"<>")
            value = value.strip(" \t\r\n.,;:!?")

            if value.startswith("[[") and value.endswith("]]"):
                value = value[2:-2]

            if value == eq_upper:
                return True

        if lower.startswith("equipment:"):
            value = line.split(":", 1)[1]

            if f"[[{eq_upper}]]" in value:
                return True

            if eq_upper in value.upper():
                return True

    return False


# ---------------------------------------------------------------------------
# Episodic prior-incident linking
# ---------------------------------------------------------------------------

def _related_prior_incidents(
    exclude_filename: str,
    preferred_eq_id: str = "",
    limit: int = 3,
) -> List[str]:
    """
    Return stems of recent prior incident notes, excluding the current file.

    If preferred_eq_id is supplied, prior incidents with the same primary
    equipment are ranked higher so the vault graph shows recurrence patterns
    instead of merely chronological neighbors.
    """
    try:
        if not INCIDENTS_DIR.exists():
            return []

        files = [
            p
            for p in INCIDENTS_DIR.glob("*.md")
            if p.is_file()
            and p.name != exclude_filename
            and not p.name.startswith(".tmp_")
        ]

        if not files:
            return []

        files.sort(key=_safe_mtime, reverse=True)

        # Bound the scan to keep this cheap.
        files = files[:50]

        pref = _normalize_eq(preferred_eq_id)
        scored: List[Tuple[float, Path]] = []

        for p in files:
            score = _safe_mtime(p)

            if pref:
                primary = _read_primary_eq(p)
                if primary == pref:
                    # Strong boost for same-equipment recurrence.
                    score += 1_000_000_000_000.0

            scored.append((score, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [p.stem for _, p in scored[:limit]]

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Incident creation
# ---------------------------------------------------------------------------

def _log_incident_once(
    equipment_ids: Any = None,
    fault_type: str = "",
    diagnosis: str = "",
    recommended_action: str = "",
    severity: str = "MEDIUM",
    source: str = "ai_engine",
    **kwargs: Any,
) -> str:
    """
    Write one incident report into the vault.

    Returns the file path, or an empty string on any failure.
    Never raises.

    LINKED: the body always contains at least one [[wiki-link]].

    ATTRIBUTED: explicit equipment_ids/kwargs are preferred. Text mining is
    only a fallback and is scored to reduce wrong-device attribution from
    LLM prose.
    """
    try:
        with _INCIDENT_LOCK:
            INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
            now = datetime.now()

            fault_type_str = _normalize_text(fault_type)
            diagnosis_str = _normalize_text(diagnosis)
            action_str = _normalize_text(recommended_action)
            source_str = _normalize_text(source) or "ai_engine"
            severity_str = _normalize_text(severity).upper() or "MEDIUM"

            explicit_ids = _collect_explicit_ids(equipment_ids, kwargs)

            if explicit_ids:
                real_ids = explicit_ids
                referenced: List[str] = []
            else:
                real_ids = []
                referenced = _mine_referenced_ids(
                    fault_type=fault_type_str,
                    diagnosis=diagnosis_str,
                    recommended_action=action_str,
                    source=source_str,
                )

            ansi_code = _extract_ansi_code(
                fault_type=fault_type_str,
                diagnosis=diagnosis_str,
                recommended_action=action_str,
                kwargs=kwargs,
            )

            if fault_type_str.upper().startswith("ANSI-"):
                standard_link = f"[[{fault_type_str}]]"
            elif ansi_code:
                standard_link = f"[[ANSI-{ansi_code}]]"
            else:
                # Keep human-readable fault class, but do not pretend it is a
                # vault note unless it already looks like one.
                standard_link = fault_type_str or "Unclassified"

            first_id = (
                real_ids[0]
                if real_ids
                else (referenced[0] if referenced else "SYSTEM")
            )

            # Microseconds reduce filename collisions during rapid fault sweeps.
            filename = f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{first_id}.md"
            filepath = INCIDENTS_DIR / filename

            if real_ids:
                machine_links = ", ".join(f"[[{eq}]]" for eq in real_ids)
            elif referenced:
                machine_links = ", ".join(f"[[{eq}]]" for eq in referenced)
            else:
                machine_links = "[[SYSTEM]]"

            related_stems = _related_prior_incidents(
                exclude_filename=filename,
                preferred_eq_id=first_id,
                limit=3,
            )

            if related_stems:
                related_md = "\n".join(f"- [[{stem}]]" for stem in related_stems)
            else:
                related_md = "- None"

            # Ensure diagnosis is not empty.
            if not diagnosis_str:
                diagnosis_str = fault_type_str or "Unclassified incident"

            if not action_str:
                action_str = "manual_review"

            content = f"""---
timestamp: {now.isoformat()}
severity: {severity_str}
equipment: {machine_links}
primary_equipment: {first_id}
fault: {standard_link}
ansi_code: {ansi_code or 'n/a'}
source: {source_str}
---

# Industrial Incident Report (auto-generated by NEXUS AI)

**Time:** {now.strftime('%Y-%m-%d %H:%M:%S')}  
**Severity:** {severity_str}  
**Equipment:** {machine_links}  
**Primary equipment:** [[{first_id}]]  
**Fault class:** {standard_link}  
**ANSI code:** `{ansi_code or 'n/a'}`

## Links

- Equipment: {machine_links}
- Primary equipment: [[{first_id}]]
- Protection standard: {standard_link}

## Diagnosis
{diagnosis_str}

## Recommended Action
{action_str}

## Related Prior Incidents
{related_md}

## Linked Equipment
"""

            for eq in real_ids:
                content += f"- [[{eq}]]\n"

            if referenced:
                content += "\n### Linked References (mined from diagnosis)\n\n"
                for eq in referenced:
                    content += f"- [[{eq}]]\n"

            if not real_ids and not referenced:
                content += "- [[SYSTEM]]\n"

            # Atomic write: temp file + replace.
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(INCIDENTS_DIR),
                prefix=".tmp_",
                suffix=".md",
            )

            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.write(content)

                os.replace(tmp_path, filepath)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            _rotate_if_needed()
            return str(filepath)

    except Exception as e:
        # Fire-and-forget: incident logging must never break the agent.
        print(f"[OBSIDIAN] incident logging skipped (non-fatal): {e}")
        return ""


def log_incident(
    equipment_ids: Any = None,
    fault_type: str = "",
    diagnosis: str = "",
    recommended_action: str = "",
    severity: str = "MEDIUM",
    source: str = "ai_engine",
    **kwargs: Any,
) -> str:
    """
    Public entry point for incident creation.

    Retry wrapper with light exponential backoff around the atomic writer.

    Compatible with existing calls:

        log_incident(
            equipment_ids=["UTI-01"],
            fault_type="ANSI-38",
            diagnosis="...",
            recommended_action="reduce_load",
            severity="HIGH",
            source="agent_audit",
        )

    Also accepts optional attribution kwargs:

        target_eq_id="UTI-01"
        primary_eq_id="UTI-01"
        device_id="UTI-01"
        ansi_code="38"
    """
    import time as _time

    result = ""

    for attempt in range(3):
        result = _log_incident_once(
            equipment_ids=equipment_ids,
            fault_type=fault_type,
            diagnosis=diagnosis,
            recommended_action=recommended_action,
            severity=severity,
            source=source,
            **kwargs,
        )

        if result:
            return result

        if attempt < 2:
            _time.sleep(0.25 * (2 ** attempt))

    return result


# ---------------------------------------------------------------------------
# Incident lookup (used by remediation append)
# ---------------------------------------------------------------------------

def find_latest_incident_for(eq_id: str) -> Optional[Path]:
    """
    Return the most recent incident file associated with ``eq_id``.

    Lookup strategy:
      1. Filename glob ``*_{eq_id}.md``.
      2. Case-insensitive stem-suffix match.
      3. Strict frontmatter scan of recent files.

    Strict frontmatter scan only accepts:
      - primary_equipment: UTI-01
      - equipment: [[UTI-01]]

    It intentionally does NOT accept casual mentions in LLM prose.
    """
    try:
        if not eq_id or not INCIDENTS_DIR.exists():
            return None

        eq_upper = _normalize_eq(eq_id)

        if not eq_upper:
            return None

        # Primary: filename match.
        by_name = [
            p
            for p in INCIDENTS_DIR.glob(f"*_{eq_upper}.md")
            if p.is_file()
        ]

        if not by_name:
            # Case-insensitive stem-suffix fallback.
            by_name = [
                p
                for p in INCIDENTS_DIR.glob("*.md")
                if p.is_file() and p.stem.upper().endswith(f"_{eq_upper}")
            ]

        if by_name:
            by_name.sort(key=_safe_mtime, reverse=True)
            return by_name[0]

        # Fallback: bounded strict frontmatter scan of newest files.
        all_md = [
            p
            for p in INCIDENTS_DIR.glob("*.md")
            if p.is_file() and not p.name.startswith(".tmp_")
        ]

        if not all_md:
            return None

        all_md.sort(key=_safe_mtime, reverse=True)

        for p in all_md[:_CONTENT_SCAN_LIMIT]:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if _strict_incident_matches(text, eq_upper):
                return p

        return None

    except Exception as e:
        print(f"[OBSIDIAN] find_latest_incident_for failed (non-fatal): {e}")
        return None


# ---------------------------------------------------------------------------
# Remediation section formatting
# ---------------------------------------------------------------------------

def _approved_action_types(decision: Dict[str, Any]) -> List[str]:
    """
    Extract approved-action type names from a decision dict.
    """
    raw = decision.get("approved_actions") or []
    names: List[str] = []

    for item in raw:
        if isinstance(item, dict):
            name = item.get("type") or item.get("action") or item.get("name")
        else:
            name = str(item)

        if name:
            names.append(str(name))

    return names


def _rejected_action_types(decision: Dict[str, Any]) -> List[str]:
    """
    Extract rejected-action type names from a decision dict.
    """
    raw = decision.get("rejected_actions") or []
    names: List[str] = []

    for item in raw:
        if isinstance(item, dict):
            name = (
                item.get("action_type")
                or item.get("type")
                or item.get("action")
            )
        else:
            name = str(item)

        if name:
            names.append(str(name))

    return names


def _decision_eq_id(decision: Dict[str, Any]) -> str:
    """
    Resolve equipment id from a remediation decision dict.
    """
    evidence = decision.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}

    for key in (
        "eq_id",
        "equipment_id",
        "device_id",
        "target_eq_id",
        "primary_eq_id",
    ):
        value = decision.get(key) or evidence.get(key)
        text = _normalize_eq(value)

        if _is_real_eq(text):
            return text

    return ""


def _decision_ansi_code(decision: Dict[str, Any]) -> str:
    """
    Resolve ANSI code from a remediation decision dict.
    """
    evidence = decision.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}

    for key in ("ansi_code", "ansi", "device_number", "relay_code"):
        value = decision.get(key) or evidence.get(key)
        text = _normalize_text(value)

        if text:
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                return digits

    return ""


def _format_remediation_section(
    decision: Dict[str, Any],
    execution_result: Dict[str, Any],
) -> str:
    """
    Build the markdown section that gets appended to the incident note.

    Both a compact bullet summary and a raw JSON block are emitted so
    the note stays human-readable while remaining machine-parseable.
    Includes [[wiki-links]] for equipment and ANSI code.
    """
    approved = _approved_action_types(decision)
    rejected = _rejected_action_types(decision)

    eq_id = _decision_eq_id(decision)
    ansi_code = _decision_ansi_code(decision)

    severity = _normalize_text(decision.get("severity")) or "unknown"
    policy_version = _normalize_text(decision.get("policy_version")) or "unknown"
    mode = _normalize_text(execution_result.get("mode")) or "unknown"
    executed = bool(execution_result.get("executed", False))

    if ansi_code and not ansi_code.upper().startswith("ANSI-"):
        ansi_link = f"ANSI-{ansi_code}"
    else:
        ansi_link = ansi_code

    approved_str = ", ".join(f"`{a}`" for a in approved) if approved else "none"
    rejected_str = ", ".join(f"`{r}`" for r in rejected) if rejected else "none"

    lines: List[str] = []
    lines.append("")
    lines.append(_REMEDIATION_HEADER)
    lines.append("")

    if eq_id:
        lines.append(f"- Equipment: [[{eq_id}]]")

    if ansi_link:
        lines.append(f"- Protection standard: [[{ansi_link}]]")

    lines.append(f"- ANSI code: `{ansi_code or 'n/a'}`")
    lines.append(f"- Severity: `{severity}`")
    lines.append(f"- Policy version: `{policy_version}`")
    lines.append(f"- Mode: `{mode}`")
    lines.append(f"- Approved actions: {approved_str}")
    lines.append(f"- Rejected actions: {rejected_str}")
    lines.append(f"- Executed: `{executed}`")

    results = execution_result.get("results") or []
    if results:
        lines.append("")
        lines.append("### Execution Detail")
        lines.append("")

        for entry in results:
            if isinstance(entry, dict):
                for k, v in entry.items():
                    lines.append(f"- {k}: {v}")
            else:
                lines.append(f"- {entry}")

    lines.append("")
    lines.append("### Raw Execution Result")
    lines.append("")
    lines.append("```json")

    try:
        lines.append(json.dumps(execution_result, indent=2, default=str))
    except Exception:
        lines.append("// <unserializable execution_result>")

    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-healing remediation append
# ---------------------------------------------------------------------------

def _create_missing_incident_for_eq(
    eq_id: str,
    decision: Dict[str, Any],
    execution_result: Dict[str, Any],
) -> Optional[Path]:
    """
    Self-healing helper.

    If remediation needs to append to an incident for eq_id but no correctly
    attributed incident exists, create a minimal incident with explicit
    equipment attribution.

    This prevents the autonomous remediation probe from failing merely because
    the earlier cognitive/Obsidian attribution path chose the wrong filename.
    """
    try:
        eq_norm = _normalize_eq(eq_id)

        if not _is_real_eq(eq_norm):
            return None

        decision = decision or {}
        execution_result = execution_result or {}

        evidence = decision.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}

        ansi_code = _decision_ansi_code(decision) or _normalize_text(
            evidence.get("ansi_code")
            or evidence.get("ansi")
            or evidence.get("device_number")
            or evidence.get("relay_code")
            or ""
        )

        if ansi_code:
            ansi_code = "".join(ch for ch in ansi_code if ch.isdigit())

        code = _normalize_text(
            decision.get("code")
            or decision.get("alarm_type")
            or evidence.get("code")
            or ""
        )

        severity = _normalize_text(
            decision.get("severity")
            or evidence.get("severity")
            or "HIGH"
        ).upper() or "HIGH"

        diagnosis = _normalize_text(
            decision.get("diagnosis")
            or decision.get("message")
            or evidence.get("diagnosis")
            or evidence.get("message")
            or f"{eq_norm}: autonomous remediation decision recorded."
        )

        if not diagnosis.upper().startswith(eq_norm.upper()):
            diagnosis = f"{eq_norm}: {diagnosis}"

        approved = _approved_action_types(decision)
        recommended_action = approved[0] if approved else _normalize_text(
            decision.get("recommended_action")
            or decision.get("action")
            or "manual_review"
        ) or "manual_review"

        fault_type = (
            f"ANSI-{ansi_code}"
            if ansi_code
            else code or "AUTONOMOUS_REMEDIATION"
        )

        source = _normalize_text(
            decision.get("source")
            or "remediation_engine"
        ) or "remediation_engine"

        # Try the newer attribution-aware signature first.
        try:
            path_str = log_incident(
                equipment_ids=[eq_norm],
                fault_type=fault_type,
                diagnosis=diagnosis,
                recommended_action=recommended_action,
                severity=severity,
                source=source,
                target_eq_id=eq_norm,
                primary_eq_id=eq_norm,
                device_id=eq_norm,
                ansi_code=ansi_code or None,
            )
        except TypeError:
            # Fallback for older obsidian_bridge.log_incident signatures.
            path_str = log_incident(
                equipment_ids=[eq_norm],
                fault_type=fault_type,
                diagnosis=diagnosis,
                recommended_action=recommended_action,
                severity=severity,
                source=source,
            )

        if not path_str:
            return None

        return Path(path_str)

    except Exception as e:
        print(f"[OBSIDIAN] missing incident self-heal failed (non-fatal): {e}")
        return None


def append_remediation_section(
    eq_id: str,
    decision: Dict[str, Any],
    execution_result: Dict[str, Any],
) -> bool:
    """
    Append a structured remediation outcome to the latest incident for
    ``eq_id``.

    This version is self-healing:
      - if a correctly attributed incident exists, append to it;
      - if none exists, create a minimal incident with explicit eq_id attribution;
      - never append to a wrong-device incident merely because LLM prose mentions eq_id.

    Returns:
        True  - section written (or already present).
        False - no incident could be found/created, or any I/O error occurred.

    Never raises. Safe to call from any thread.
    """
    try:
        eq_norm = _normalize_eq(eq_id)

        if not _is_real_eq(eq_norm):
            print(
                f"[OBSIDIAN] invalid or system-level eq_id for remediation append: "
                f"{eq_id!r}"
            )
            return False

        with _INCIDENT_LOCK:
            incident_path = find_latest_incident_for(eq_norm)

            if incident_path is None:
                print(
                    f"[OBSIDIAN] no incident found for {eq_norm}; "
                    "creating self-healed incident before remediation append"
                )

                incident_path = _create_missing_incident_for_eq(
                    eq_norm,
                    decision,
                    execution_result,
                )

            if incident_path is None:
                print(
                    f"[OBSIDIAN] no incident found or created for {eq_norm}; "
                    "remediation not linked"
                )
                return False

            # Idempotency: skip if a remediation section is already present.
            try:
                existing = incident_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
                if _REMEDIATION_HEADER in existing:
                    return True
            except Exception:
                # If we cannot read it, attempt the append anyway.
                pass

            decision = decision or {}
            execution_result = execution_result or {}

            section = _format_remediation_section(decision, execution_result)

            with incident_path.open("a", encoding="utf-8") as f:
                f.write(section)

            print(f"[OBSIDIAN] remediation appended to {incident_path.name}")
            return True

    except Exception as e:
        print(f"[OBSIDIAN] remediation append skipped (non-fatal): {e}")
        return False


# ---------------------------------------------------------------------------
# Episodic memory helper (feeds prior incidents into the LLM prompt)
# ---------------------------------------------------------------------------

def get_recent_incidents(limit: int = 5) -> List[Dict[str, str]]:
    """
    Return the N newest incidents as best-effort summary dicts.

    Used by the agent to give the LLM episodic context.
    """
    try:
        if not INCIDENTS_DIR.exists():
            return []

        files = [
            p
            for p in INCIDENTS_DIR.glob("*.md")
            if p.is_file() and not p.name.startswith(".tmp_")
        ]

        if not files:
            return []

        files.sort(key=_safe_mtime, reverse=True)

        out: List[Dict[str, str]] = []

        for p in files[:limit]:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            sev = ""
            diag = ""
            equipment = ""
            primary = _read_primary_eq(p)

            lines_list = text.splitlines()

            for i, line in enumerate(lines_list):
                stripped = line.strip()

                if stripped.startswith("severity:") and not sev:
                    sev = stripped.split(":", 1)[1].strip()

                if stripped.startswith("equipment:") and not equipment:
                    equipment = stripped.split(":", 1)[1].strip()

                if stripped.startswith("## Diagnosis") and not diag:
                    rest = lines_list[i + 1:]
                    diag = next((l.strip() for l in rest if l.strip()), "")[:180]
                    break

            out.append(
                {
                    "file": p.name,
                    "stem": p.stem,
                    "severity": sev,
                    "equipment": equipment,
                    "primary_equipment": primary,
                    "diagnosis": diag,
                }
            )

        return out

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Optional smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 1. Create an incident as the agent would.
    path = log_incident(
        equipment_ids=["STP-01"],
        fault_type="ANSI-49",
        diagnosis="Theta exceeded 100% for 5 consecutive polls - sustained thermal overload.",
        recommended_action="reduce_load",
        severity="HIGH",
        source="obsidian_bridge_smoke",
        target_eq_id="STP-01",
        primary_eq_id="STP-01",
        device_id="STP-01",
        ansi_code="49",
    )
    print(f"[TEST] incident written to: {path}")

    # 2. Append the remediation outcome as the engine would.
    decision = {
        "eq_id": "STP-01",
        "ansi_code": "49",
        "severity": "HIGH",
        "policy_version": "v1.2.3",
        "approved_actions": [{"type": "REDUCE_LOAD"}],
        "rejected_actions": [{"action_type": "SAFE_STOP_NON_CRITICAL"}],
    }

    execution_result = {
        "mode": "limited_autonomous",
        "executed": True,
        "results": [
            {"action": "REDUCE_LOAD", "old_load": 100, "new_load": 90},
        ],
    }

    ok = append_remediation_section("STP-01", decision, execution_result)
    print(f"[TEST] remediation append ok={ok}")

    # 3. Idempotency check: calling again should be a no-op.
    ok2 = append_remediation_section("STP-01", decision, execution_result)
    print(
        "[TEST] second append "
        f"(should be True, no duplication) ok={ok2}"
    )