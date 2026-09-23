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
