"""
NEXUS SCADA Industrial Cognitive Agent Package.

This package implements a lightweight but production-oriented
industrial supervisor agent for SCADA/HMI/PLC environments.

Important:
    This is NOT a certified safety controller.
    It is a supervisory cognitive layer above deterministic PLC logic.
"""

from .config import AgentConfig, EquipmentProfile, DEFAULT_PROFILES
from .models import (
    OperatingMode,
    SystemState,
    Severity,
    EquipmentSnapshot,
    Finding,
)
from .agent import IndustrialCognitiveAgent

__all__ = [
    "AgentConfig",
    "EquipmentProfile",
    "DEFAULT_PROFILES",
    "OperatingMode",
    "SystemState",
    "Severity",
    "EquipmentSnapshot",
    "Finding",
    "IndustrialCognitiveAgent",
]
