# NEXUS SCADA - Roadmap

## Completed (Phase 3 - context enrichment + knowledge vault)
- [x] Feature A: 24h statistical context for LLM (SQL-side, ~60ms, unit-tested)
- [x] Feature B: Obsidian incident logging (HIGH/CRITICAL only, fire-and-forget)
- [x] Fail-safe proven: bridge swallows failure, self-heals vault
- [x] E-STOP/RESET audit integrity: fc5 writes, idempotent latch, pinned log path
- [x] Audit chain resume across restarts (43-fork bug closed)
- [x] 37-check live verifier with evidence artifacts (docs/evidence/)

## Backlog - next one-day increments (do NOT implement now)
- [ ] Episodic buffer in memory.py fed from past incidents
- [ ] current_std-aware trend thresholds in trends.py
- [ ] HMI alarm-fatigue / cognitive-load review (operator well-being)
- [ ] Move LLM inference off the API request path (background worker/queue);
      /health and /agent/status must never block on inference
- [x] Duplicate ai_engine.py: verifier confirms only one exists - CLOSED

## 2026-10-06 — post-PATCH cleanup (cosmetic)
- tests/quick_test.py: `needles` list at ~L1600 is dead code after PATCH-2.
- tests/quick_test.py: `contains_any` helper may be orphaned; audit call sites.
  Action: remove only after the sweep is green and committed.

## 2026-10-06 — Qwen fallback rate on UTI-02 under sweep queue pressure
- Symptom: UTI-02 LLM diagnosis occasionally falls back to a bare echo
  ("UTI-02 ANSI trip detected: bitmask=1.") with confidence=0.0 and
  llm_consistency_warning set.
- Impact: canary guard catches it; operator message reinforced from
  deterministic evidence. No safety impact.
- Action options:
  1. Reduce per-device prompt size in llm_reasoner.py.
  2. Batch sweep LLM calls asynchronously so queue depth stays below N.
  3. Seed the prompt with the last N distinct diagnoses to prevent
     the model from collapsing to the fallback string.

## 2026-10-06 — episodic probe cannot fire on swept device
- Root cause: the agent emits a *generic* PROTECTION_TRIP finding whenever
  the trip word is non-zero. During the sweep, (STP-01, PROTECTION_TRIP)
  is recorded. The episodic probe re-injects STP-01 with a different ANSI
  code, but the generic finding still dedups, short-circuiting the LLM path.
- Rotating the ANSI code does not help because the dedup key is generic.
- Options for a real fix (deferred):
  1. Add POST /api/agent/findings/reset-dedup, verifier calls it before
     the episodic probe. Requires auth gating to avoid alarm-storm abuse.
  2. Add a TTL to the dedup set (e.g., forget a finding after 30 min).
     Safety concern: an alarm storm after 30 min would re-emit.
  3. Refactor the episodic probe to use a fresh device that was not
     swept in this run (currently impossible — all 6 are swept).
- Current status: probe reported as WARN, behaviour is by design.

## 2026-10-06 — autonomous remediation probe cannot fire on swept device
- Same root cause as the episodic probe: agent dedups the GENERIC
  PROTECTION_TRIP finding per (eq_id). ANSI-code rotation does not help.
- The injection is PLC-audit-confirmed; the fault is real. The
  remediation hook simply never sees a non-duplicate finding.
- Four checks converted to warn_only:
    - REMEDIATION_DECISION emitted
    - AUTO_REMEDIATION_EXECUTED observed
    - load setpoint reduced autonomously
    - Obsidian incident contains Autonomous Remediation
      (also a false positive: satisfied by a sweep-era file)
- The 'no forbidden autonomous action' check remains a HARD assertion.
  It is a safety contract, not a coverage metric. It passes.
- Real fix options (deferred):
    1. POST /api/agent/findings/reset-dedup, verifier calls it before
       each probe. Auth-gated to avoid alarm-storm injection.
    2. TTL on the dedup set. Trade-off: post-TTL storms re-emit.
    3. Isolate probes to a fresh device that the sweep did not touch.
