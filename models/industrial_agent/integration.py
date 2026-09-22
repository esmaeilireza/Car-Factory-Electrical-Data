"""
FastAPI integration helpers for NEXUS SCADA.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .agent import IndustrialCognitiveAgent
from .config import AgentConfig
from .modbus_adapter import ModbusSafetyAdapter


EQUIPMENT_IDS = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]


def _project_root() -> Path:
    # package: models/industrial_agent/integration.py
    # parents[0]=industrial_agent, parents[1]=models, parents[2]=project root
    return Path(__file__).resolve().parents[2]


def load_ai_engine() -> Optional[Any]:
    try:
        root = _project_root()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from ai_engine import get_ai_engine
        return get_ai_engine()
    except Exception as e:
        print(f"[INTEGRATION] AI engine unavailable: {e}")
        return None


def create_default_agent(
    command_callback: Optional[Any] = None,
    enable_modbus_adapter: bool = False,
) -> IndustrialCognitiveAgent:
    ai_engine = load_ai_engine()

    if command_callback is None and enable_modbus_adapter:
        adapter = ModbusSafetyAdapter()
        command_callback = adapter.execute

    return IndustrialCognitiveAgent(
        config=AgentConfig(),
        ai_engine=ai_engine,
        command_callback=command_callback,
    )


def install_agent_routes(app: Any, db: Any, enable_modbus_adapter: bool = False) -> IndustrialCognitiveAgent:
    """
    Attach agent background loop and REST endpoints to an existing FastAPI app.
    """
    agent = create_default_agent(enable_modbus_adapter=enable_modbus_adapter)
    app.state.agent = agent
    app.state.agent_system_status = {"estop_active": False, "any_trip_active": False}

    async def agent_background_loop() -> None:
        while True:
            try:
                equipment_data: Dict[str, Dict[str, Any]] = {}

                for eq_id in EQUIPMENT_IDS:
                    row = db.get_latest_data(eq_id)
                    if row:
                        equipment_data[eq_id] = row

                system_status = getattr(app.state, "agent_system_status", {})
                agent.ingest_data(equipment_data, system_status)

            except Exception as e:
                print(f"[AGENT LOOP] error: {e}")

            await asyncio.sleep(1.0)

    @app.on_event("startup")
    async def startup_agent() -> None:
        asyncio.create_task(agent_background_loop())
        print("[INTEGRATION] Industrial agent background loop started.")

    @app.get("/api/agent/status")
    async def get_agent_status() -> Dict[str, Any]:
        return agent.get_dashboard_summary()

    @app.get("/api/agent/operator-message")
    async def get_agent_operator_message() -> Dict[str, str]:
        return {"message": agent.get_operator_message()}

    @app.post("/api/agent/operator-presence")
    async def set_operator_presence(present: bool) -> Dict[str, Any]:
        agent.set_operator_presence(bool(present))
        return {
            "operator_present": agent.operator_present,
            "mode": agent.mode.value,
        }

    @app.post("/api/agent/system-status")
    async def set_system_status(payload: Dict[str, Any]) -> Dict[str, Any]:
        app.state.agent_system_status = {
            "estop_active": bool(payload.get("estop_active", False)),
            "any_trip_active": bool(payload.get("any_trip_active", False)),
        }
        return app.state.agent_system_status

    return agent
