"""
FastAPI Backend for NEXUS SCADA
Optimized version with Industrial Cognitive Agent integration.

This file is the project API backend.
Do not confuse it with the installed requests library file requests/api.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from pymodbus.client import ModbusTcpClient
except Exception:
    ModbusTcpClient = None

# -----------------------------------------------------------------------------
# Path setup: make `backend/` and `models/` importable no matter how we run
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent          # .../backend
PROJECT_ROOT = BASE_DIR.parent                       # .../car-factory-electrical-data
MODELS_DIR = PROJECT_ROOT / "models"                 # contains industrial_agent/

for path_entry in (str(BASE_DIR), str(MODELS_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from database import db                              # noqa: E402
from industrial_agent import AgentConfig, IndustrialCognitiveAgent  # noqa: E402

try:
    from ai_engine import get_ai_engine
except Exception:
    get_ai_engine = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Auth is ENABLED only if NEXUS_API_KEY is set in the environment.
# For local development, leave it unset.
API_KEY = os.environ.get("NEXUS_API_KEY", "")
AUTH_ENABLED = bool(API_KEY)

CACHE_TTL_SECONDS = float(os.environ.get("NEXUS_CACHE_TTL", "60"))
AGENT_INTERVAL_SEC = float(os.environ.get("NEXUS_AGENT_INTERVAL", "1.0"))

ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:8501",
    "http://localhost:3000",
    "http://127.0.0.1",
    "http://127.0.0.1:8501",
]

EQUIPMENT_IDS = [
    "STP-01",
    "WLD-01",
    "PNT-01",
    "ASM-01",
    "UTI-01",
    "UTI-02",
]

MODBUS_HOST = os.environ.get("NEXUS_MODBUS_HOST", "127.0.0.1")
MODBUS_PORT = int(os.environ.get("NEXUS_MODBUS_PORT", "5020"))
MODBUS_SLAVE = int(os.environ.get("NEXUS_MODBUS_SLAVE", "1"))

SYS_STATUS_ADDR = 120
SYS_STATUS_ESTOP_BIT = 0
SYS_STATUS_ANY_TRIP_BIT = 1


# -----------------------------------------------------------------------------
# Thread-safe TTL cache
# -----------------------------------------------------------------------------

class TTLCache:
    """Simple async-safe cache with per-key TTL expiration."""

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._store: Dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None

            ts, value = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[key]
                return None

            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic(), value)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)


equipment_cache = TTLCache(ttl=CACHE_TTL_SECONDS)


# -----------------------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------------------

async def verify_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    Reject requests without a valid API key on protected endpoints.

    When NEXUS_API_KEY is not set (local dev), auth is disabled.
    """
    if not AUTH_ENABLED:
        return

    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# -----------------------------------------------------------------------------
# AI engine loader
# -----------------------------------------------------------------------------

def load_ai_engine() -> Optional[Any]:
    """
    Load local Qwen2.5-Coder engine if available.
    Failure must not crash the API.
    """
    if get_ai_engine is None:
        print("[API] ai_engine.get_ai_engine is not available.")
        return None

    try:
        print("[API] Loading local AI engine...")
        engine = get_ai_engine()
        print("[API] Local AI engine loaded.")
        return engine
    except Exception as e:
        print(f"[API] Failed to load AI engine: {e}")
        return None


# -----------------------------------------------------------------------------
# Modbus system status reader
# -----------------------------------------------------------------------------

def read_modbus_system_status_sync() -> Optional[Dict[str, Any]]:
    """
    Read HR[120] system status word from the PLC simulator.

    Returns None if Modbus is unavailable.
    """
    if ModbusTcpClient is None:
        return None

    try:
        client = ModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT, timeout=1)

        if not client.connect():
            return None

        try:
            result = client.read_holding_registers(
                address=SYS_STATUS_ADDR,
                count=1,
                slave=MODBUS_SLAVE,
            )

            if result.isError() or not result.registers:
                return None

            word = int(result.registers[0])

            return {
                "estop_active": bool(word & (1 << SYS_STATUS_ESTOP_BIT)),
                "any_trip_active": bool(word & (1 << SYS_STATUS_ANY_TRIP_BIT)),
                "source": "modbus",
                "updated_at": time.time(),
            }
        finally:
            try:
                client.close()
            except Exception:
                pass

    except Exception:
        return None


# -----------------------------------------------------------------------------
# Global agent / system state
# -----------------------------------------------------------------------------

agent: Optional[IndustrialCognitiveAgent] = None
agent_task: Optional[asyncio.Task] = None

system_status: Dict[str, Any] = {
    "estop_active": False,
    "any_trip_active": False,
    "source": "default",
    "updated_at": 0.0,
}


async def refresh_system_status() -> None:
    """Refresh global system status from Modbus (non-blocking)."""
    global system_status

    status = await asyncio.to_thread(read_modbus_system_status_sync)

    if status is not None:
        system_status = status
    else:
        system_status = {
            **system_status,
            "source": f"{system_status.get('source', 'unknown')}-stale",
        }


async def collect_equipment_data() -> Dict[str, Dict[str, Any]]:
    """Collect latest equipment data from SQLite without blocking the loop."""
    equipment_data: Dict[str, Dict[str, Any]] = {}

    for eq_id in EQUIPMENT_IDS:
        row = await asyncio.to_thread(db.get_latest_data, eq_id)
        if row:
            equipment_data[eq_id] = row

    return equipment_data


async def agent_background_loop() -> None:
    """
    Background supervisor loop.

    Pipeline:
        Modbus HR[120] + SQLite latest telemetry
            -> IndustrialCognitiveAgent
            -> findings / recommendations / audit
    """
    while True:
        try:
            if agent is not None:
                await refresh_system_status()
                equipment_data = await collect_equipment_data()

                if equipment_data:
                    await asyncio.to_thread(agent.ingest_data, equipment_data, system_status)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[AGENT LOOP] error: {e}")

        await asyncio.sleep(AGENT_INTERVAL_SEC)


# -----------------------------------------------------------------------------
# FastAPI lifespan
# -----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop background services."""
    global agent, agent_task

    print("[API] NEXUS SCADA Backend starting...")
    print(f"[API] Auth mode: {'ENABLED' if AUTH_ENABLED else 'DISABLED (dev mode)'}")

    try:
        db.cleanup_old_data(days=30)
    except Exception as e:
        print(f"[API] Startup cleanup skipped or failed: {e}")

    ai_engine = await asyncio.to_thread(load_ai_engine)

    agent = IndustrialCognitiveAgent(
        config=AgentConfig(),
        ai_engine=ai_engine,
        command_callback=None,
    )

    app.state.agent = agent
    app.state.system_status = system_status

    agent_task = asyncio.create_task(agent_background_loop())

    modbus_ok = await asyncio.to_thread(read_modbus_system_status_sync)
    print(
        "[API] Modbus HR[120] check: "
        + ("OK" if modbus_ok is not None else "UNREACHABLE (is the PLC simulator running?)")
    )

    print("[API] Industrial agent background loop started.")
    print("[API] Address: http://localhost:8000")
    print("[API] Docs: http://localhost:8000/docs")

    yield

    print("[API] Shutting down...")

    if agent_task is not None:
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass

    print("[API] Shutdown complete.")


# -----------------------------------------------------------------------------
# FastAPI application
# -----------------------------------------------------------------------------

app = FastAPI(
    title="NEXUS SCADA API",
    description="Production Backend - Phase 3 with Industrial Cognitive Agent",
    version="3.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# 422 diagnostics (keep during development; remove before production)
# -----------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def debug_validation_handler(request: Request, exc: RequestValidationError):
    """Log and return detailed validation errors instead of a bare 422."""
    body = (await request.body()).decode(errors="replace")

    print("\n[422 DEBUG] URL:", request.url.path)
    print("[422 DEBUG] BODY:", body[:500])
    print("[422 DEBUG] ERRORS:", jsonable_encoder(exc.errors())[:5], "\n")

    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


def get_agent() -> IndustrialCognitiveAgent:
    agent_obj = getattr(app.state, "agent", None)
    if agent_obj is None:
        raise HTTPException(status_code=503, detail="Industrial agent is not ready")
    return agent_obj


# -----------------------------------------------------------------------------
# Public endpoints
# -----------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "name": "NEXUS SCADA API",
        "version": "3.1.0",
        "status": "running",
        "auth_enabled": AUTH_ENABLED,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/health")
async def health_check():
    agent_obj = getattr(app.state, "agent", None)

    return {
        "status": "healthy",
        "database": "sqlite",
        "agent": "active" if agent_obj is not None else "not_started",
        "agent_state": agent_obj.state.value if agent_obj is not None else None,
        "operating_mode": agent_obj.mode.value if agent_obj is not None else None,
        "llm_available": agent_obj.llm_reasoner.available() if agent_obj is not None else False,
        "system_status": getattr(app.state, "system_status", system_status),
        "timestamp": datetime.now().isoformat(),
    }


# -----------------------------------------------------------------------------
# Equipment endpoints
# -----------------------------------------------------------------------------

@app.get("/api/equipment")
async def get_all_equipment():
    """Get status of all equipment. Missing equipment is returned as stale."""
    result: Dict[str, Any] = {}

    for eq_id in EQUIPMENT_IDS:
        cached = await equipment_cache.get(eq_id)

        if cached is not None:
            result[eq_id] = cached
            continue

        data = await asyncio.to_thread(db.get_latest_data, eq_id)

        if data:
            result[eq_id] = data
            await equipment_cache.set(eq_id, data)
        else:
            result[eq_id] = {
                "stale": True,
                "equipment_id": eq_id,
                "data": None,
            }

    return {"equipment": result}


@app.get("/api/equipment/{equipment_id}")
async def get_equipment(equipment_id: str):
    if equipment_id not in EQUIPMENT_IDS:
        raise HTTPException(status_code=404, detail="Unknown equipment")

    cached = await equipment_cache.get(equipment_id)
    if cached is not None:
        return cached

    data = await asyncio.to_thread(db.get_latest_data, equipment_id)
    if not data:
        raise HTTPException(status_code=404, detail="Equipment data not found")

    await equipment_cache.set(equipment_id, data)
    return data


@app.get("/api/equipment/{equipment_id}/history")
async def get_equipment_history(equipment_id: str, hours: int = 24):
    if equipment_id not in EQUIPMENT_IDS:
        raise HTTPException(status_code=404, detail="Unknown equipment")

    history = await asyncio.to_thread(db.get_history, equipment_id, hours)
    return {"equipment_id": equipment_id, "hours": hours, "history": history}


@app.post(
    "/api/equipment/{equipment_id}/data",
    dependencies=[Depends(verify_api_key)],
)
async def save_equipment_data(equipment_id: str, data: Dict[str, Any]):
    """
    Save new equipment data. Called by the Modbus server.

    Requires X-API-Key header when NEXUS_API_KEY is set.
    """
    if equipment_id not in EQUIPMENT_IDS:
        raise HTTPException(status_code=404, detail="Unknown equipment")

    success = await asyncio.to_thread(db.save_equipment_data, equipment_id, data)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to save data")

    await equipment_cache.set(equipment_id, data)

    return {
        "status": "saved",
        "equipment_id": equipment_id,
        "timestamp": datetime.now().isoformat(),
    }


# -----------------------------------------------------------------------------
# Alarm endpoints
# -----------------------------------------------------------------------------

@app.get("/api/alarms/active")
async def get_active_alarms():
    alarms = await asyncio.to_thread(db.get_active_alarms)
    return {"alarms": alarms, "count": len(alarms)}


@app.post(
    "/api/alarms/{alarm_id}/acknowledge",
    dependencies=[Depends(verify_api_key)],
)
async def acknowledge_alarm(alarm_id: int, user: str = "system"):
    success = await asyncio.to_thread(db.acknowledge_alarm, alarm_id, user)

    if success:
        return {"status": "acknowledged", "alarm_id": alarm_id, "user": user}

    raise HTTPException(status_code=404, detail="Alarm not found")


# -----------------------------------------------------------------------------
# Cleanup endpoint
# -----------------------------------------------------------------------------

@app.post("/api/cleanup", dependencies=[Depends(verify_api_key)])
async def cleanup_data(days: int = 30):
    await asyncio.to_thread(db.cleanup_old_data, days)
    return {"status": "cleanup completed", "days": days}


# -----------------------------------------------------------------------------
# Industrial agent endpoints
# -----------------------------------------------------------------------------

@app.get("/api/agent/status")
async def get_agent_status():
    """Full agent dashboard summary for HMI/Streamlit."""
    agent_obj = get_agent()
    return agent_obj.get_dashboard_summary()


@app.get("/api/agent/operator-message")
async def get_agent_operator_message():
    """Short natural-language message for operator display."""
    agent_obj = get_agent()
    return {
        "message": agent_obj.get_operator_message(),
        "system_state": agent_obj.state.value,
        "operating_mode": agent_obj.mode.value,
    }


@app.post(
    "/api/agent/operator-presence",
    dependencies=[Depends(verify_api_key)],
)
async def set_operator_presence(present: bool):
    """
    Set operator presence.

    present=true  -> agent defaults to ADVISORY mode.
    present=false -> agent defaults to AUTONOMOUS mode (still policy-constrained).
    """
    agent_obj = get_agent()
    agent_obj.set_operator_presence(bool(present))

    return {
        "operator_present": agent_obj.operator_present,
        "mode": agent_obj.mode.value,
    }


@app.post(
    "/api/agent/system-status",
    dependencies=[Depends(verify_api_key)],
)
async def set_system_status(payload: Dict[str, Any]):
    """
    Manually override system status.

    Example: {"estop_active": true, "any_trip_active": false}
    """
    global system_status

    system_status = {
        "estop_active": bool(payload.get("estop_active", False)),
        "any_trip_active": bool(payload.get("any_trip_active", False)),
        "source": "manual",
        "updated_at": time.time(),
    }

    app.state.system_status = system_status
    return system_status


# -----------------------------------------------------------------------------
# Run server
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )