# NEXUS SCADA Industrial Cognitive Agent

This package implements a supervisory AI layer for the car-factory electrical SCADA project.

## Design Principle

- Deterministic rules protect the plant.
- Statistical trends detect early degradation.
- Local LLM explains and recommends.
- Action policy enforces guardrails.
- PLC remains the final deterministic control layer.

## Important Safety Notice

This agent is **not** a certified safety controller.

It must never:
- automatically clear E-STOP
- automatically restart locked-out equipment
- bypass ANSI protection relays
- modify PLC safety logic

Allowed autonomous actions must be limited to conservative operations such as:
- stop_equipment
- reduce_load
- raise_alarm
- reconnect_modbus
- flush_session_cache

## Package Layout

- `config.py`: equipment profiles and thresholds
- `models.py`: snapshots, findings, enums
- `memory.py`: rolling metric windows
- `rules.py`: deterministic safety/data-quality engine
- `trends.py`: slope/z-score/statistical monitors
- `state_machine.py`: system state transitions
- `llm_reasoner.py`: Qwen2.5-Coder local reasoning
- `action_policy.py`: guardrails and allowlists
- `audit.py`: tamper-evident JSONL audit log
- `agent.py`: main orchestrator
- `modbus_adapter.py`: optional safe Modbus command executor
- `integration.py`: FastAPI attachment helper
- `hmi_bridge.py`: HMI polling helper
