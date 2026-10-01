# Autonomous Remediation Protocol

## Purpose

This document defines the bounded autonomous remediation behavior of the NEXUS SCADA industrial agent.

## Non-goal

The local LLM is not a safety controller. It must never directly write safety-critical coils or bypass ANSI protection functions.

## Allowed Modes

### advisory

The agent produces recommendations only. A human operator must approve actions.

### limited_autonomous

The agent may execute only actions approved by the deterministic safety guard.

## Allowed Actions

- REDUCE_LOAD
- HOLD_STATE
- REQUEST_OPERATOR_ACK
- ISOLATE_NON_CRITICAL
- SAFE_STOP_NON_CRITICAL

## Forbidden Actions

- RESET_GLOBAL_ESTOP
- CLEAR_GLOBAL_ESTOP_LATCH
- INCREASE_LOAD
- BYPASS_ANSI_TRIP
- WRITE_SAFETY_COILS
- START_LOCKED_EQUIPMENT
- DISABLE_PROTECTION_RELAYS

## Safety Guard Rule

The guard must not silently rewrite requested actions.

If a requested load reduction would breach the policy minimum, the guard rejects the action and forces the engine to propose a smaller step or escalate to the operator.

## Audit Events

- REMEDIATION_DECISION
- AUTO_REMEDIATION_EXECUTED
- REMEDIATION_NOT_EXECUTED
- REMEDIATION_HOOK_ERROR

## Obsidian Output

High-severity incidents should include an Autonomous Remediation section containing:

- ANSI code
- severity
- approved actions
- rejected actions
- execution result
- old/new load setpoint when applicable
