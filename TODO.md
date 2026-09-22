# Week Plan: Context Enrichment + Obsidian Vault

## Feature A: 24h statistical context for LLM diagnostics
- [ ] get_equipment_statistics(equipment_id, hours=24) in database.py
- [ ] unit test with fixture DB
- [ ] inject one compact line per equipment into LLM prompt context
- [ ] verify rules path unchanged when LLM disabled
Acceptance: audit log shows history-aware diagnosis; regression clean.

## Feature B: Obsidian incident logging
- [ ] data/scada_vault/{Machines,Incidents,Standards} skeleton
- [ ] obsidian_bridge.py (fire-and-forget safe, rotation cap 500)
- [ ] hook in ai_engine._log_audit, severity >= high only
- [ ] fail-safe test (vault missing -> system still works)
Acceptance: high-severity diagnosis produces linked .md; LOW produces nothing.
