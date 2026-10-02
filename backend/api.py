"""
FastAPI Backend for NEXUS SCADA
Optimized version with Industrial Cognitive Agent integration.

This file is the project API backend.
Do not confuse it with the installed requests library file requests/api.py.

Remediation control layer
-------------------------
* Modes: ``advisory`` (default) and ``limited_autonomous``.
  The default is intentionally the *safest* one: the system only
  recommends; a human decides.
* ``limited_autonomous`` may be enabled by an operator for live
  verification, but never silently and never without auth.
* The mode set is enforced at the API boundary: unknown modes are
  rejected before reaching the engine.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import (
    APIRouter,
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
from pydantic import BaseModel

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

# Remediation policy loader is optional. If the module is absent, the
# remediation endpoints still respond -- they just report "unavailable".
try:
    from industrial_agent.remediation import load_policy  # type: ignore
except Exception:
    load_policy = None


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
# Remediation configuration
# -----------------------------------------------------------------------------

# The default mode is deliberately the *safest* one. An operator (or the
# live verifier) must explicitly promote the system to limited_autonomous
# through POST /api/agent/remediation/mode.
DEFAULT_REMEDIATION_MODE = "advisory"
VALID_REMEDIATION_MODES = frozenset({"advisory", "limited_autonomous"})

REMEDIATION_POLICY_PATH = os.environ.get(
    "NEXUS_REMEDIATION_POLICY_PATH",
    str(PROJECT_ROOT / "configs" / "remediation_policy.json"),
)

REMEDIATION_HISTORY_MAX = 500


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
# Bounded in-memory remediation history
# -----------------------------------------------------------------------------
#
# The remediation engine can append entries here (via
# ``record_remediation_event``) so the API exposes a rolling audit trail
# even before a database-backed history is wired in.
#
_remediation_history: Deque[Dict[str, Any]] = deque(maxlen=REMEDIATION_HISTORY_MAX)


def record_remediation_event(event: Dict[str, Any]) -> None:
    """
    Append an entry to the bounded in-memory remediation history.

    Safe to call from any thread. The deque itself enforces the upper
    bound, so callers do not need to manage size.
    """
    if not isinstance(event, dict):
        return
    entry = {"timestamp": time.time(), **event}
    _remediation_history.append(entry)


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
        Modbus HR[120] + SQLite latest telemetry + periodic 24h statistics
            -> IndustrialCognitiveAgent
            -> findings / recommendations / audit
    """
    while True:
        try:
            if agent is not None:
                await refresh_system_status()
                equipment_data = await collect_equipment_data()

                if equipment_data:
                    stats_cycle = getattr(agent_background_loop, "cycle", 0) + 1
                    agent_background_loop.cycle = stats_cycle

                    # Defensive: immune to future agent refactors that may
                    # rename/remove _latest_stats
                    stats_payload = getattr(agent, "_latest_stats", None) or {}
                    # Every 5 cycles (~5 seconds), fetch 24-hour statistics
                    if stats_cycle % 5 == 1 or not stats_payload:
                        stats_payload = {
                            eq_id: await asyncio.to_thread(
                                db.get_equipment_statistics, eq_id, 24
                            )
                            for eq_id in equipment_data
                        }

                    await asyncio.to_thread(
                        agent.ingest_data, equipment_data, system_status, stats_payload
                    )

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
    print(f"[API] Remediation default mode: {DEFAULT_REMEDIATION_MODE}")

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

    # ALARM-SINK WIRING: persist HIGH/CRITICAL findings to alarm_events.
    try:
        from industrial_agent.remediation_hook import set_alarm_sink
        set_alarm_sink(db)
        # OBSIDIAN-HANDLE-WIRING: pass the obsidian bridge module itself —
        # it exposes the module-level append_remediation_section(...) the
        # engine looks for via hasattr(...).
        import obsidian_bridge as _ob
        set_obsidian_handle(_ob)
        print("[API] alarm_events sink wired into remediation hook")
    except Exception as _sink_err:
        print(f"[API] alarm sink wiring failed: {_sink_err}")

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
    version="3.2.0",
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


def get_remediation_engine() -> Optional[Any]:
    """
    Return the agent's remediation engine if it has been attached.

    Remediation is an optional capability. When the engine is missing
    the API reports ``available: false`` instead of failing.
    """
    agent_obj = getattr(app.state, "agent", None)
    if agent_obj is None:
        return None
    return getattr(agent_obj, "remediation_engine", None)


# -----------------------------------------------------------------------------
# Public endpoints
# -----------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "name": "NEXUS SCADA API",
        "version": "3.2.0",
        "status": "running",
        "auth_enabled": AUTH_ENABLED,
        "remediation_default_mode": DEFAULT_REMEDIATION_MODE,
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
        "remediation_available": get_remediation_engine() is not None,
        "timestamp": datetime.now().isoformat(),
    }


# -----------------------------------------------------------------------------
# Equipment endpoints
# -----------------------------------------------------------------------------

@app.get("/api/equipment")
async def get_all_equipment():
    """Get status of all equipment. All entries use consistent wrapped shape."""
    result: Dict[str, Any] = {}

    for eq_id in EQUIPMENT_IDS:
        cached = await equipment_cache.get(eq_id)

        if cached is not None:
            result[eq_id] = {
                "equipment_id": eq_id,
                "stale": False,
                "data": cached,
            }
            continue

        data = await asyncio.to_thread(db.get_latest_data, eq_id)

        if data:
            result[eq_id] = {
                "equipment_id": eq_id,
                "stale": False,
                "data": data,
            }
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
# Remediation control endpoints
# -----------------------------------------------------------------------------
#
# Safety contract
# ---------------
# * Default mode is ``advisory`` (recommend only).
# * ``limited_autonomous`` may be enabled by an authenticated operator
#   for live verification, but the engine is still bound by its own
#   policy (allowed/forbidden actions).
# * Unknown modes are rejected at the API boundary -- the engine never
#   sees them.

remediation_router = APIRouter(
    prefix="/api/agent/remediation",
    tags=["remediation"],
)


class RemediationModeRequest(BaseModel):
    mode: str


@remediation_router.get("/status")
async def remediation_status():
    """
    Report the current remediation mode and the policy in force.

    Always returns 200. When the engine is not initialized, reports
    ``available: false`` with the default mode so clients see a
    consistent shape.
    """
    engine = get_remediation_engine()

    if engine is None:
        return {
            "available": False,
            "mode": DEFAULT_REMEDIATION_MODE,
            "policy_version": None,
            "allowed_actions": [],
            "forbidden_actions": [],
            "reason": "Remediation engine not initialized.",
        }

    policy = getattr(engine, "policy", None)
    return {
        "available": True,
        "mode": getattr(policy, "mode", DEFAULT_REMEDIATION_MODE),
        "policy_version": getattr(policy, "version", None),
        "allowed_actions": list(getattr(policy, "allowed_actions", {}).keys()),
        "forbidden_actions": list(getattr(policy, "forbidden_actions", [])),
    }


@remediation_router.post("/mode", dependencies=[Depends(verify_api_key)])
async def set_remediation_mode(req: RemediationModeRequest):
    """
    Change the remediation mode.

    Only modes in ``VALID_REMEDIATION_MODES`` are accepted. This is
    enforced *before* the request reaches the engine, so a typo or an
    unexpected value cannot weaken the safety layer.
    """
    requested = (req.mode or "").strip().lower()

    if requested not in VALID_REMEDIATION_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported remediation mode '{req.mode}'. "
                f"Allowed: {sorted(VALID_REMEDIATION_MODES)}"
            ),
        )

    engine = get_remediation_engine()
    if engine is None:
        return {
            "ok": False,
            "mode": DEFAULT_REMEDIATION_MODE,
            "error": "Remediation engine not initialized.",
        }

    ok = bool(engine.set_mode(requested))

    if ok:
        record_remediation_event(
            {
                "event": "mode_change",
                "requested_mode": requested,
                "applied_mode": getattr(
                    getattr(engine, "policy", None), "mode", requested
                ),
            }
        )

    return {
        "ok": ok,
        "mode": getattr(getattr(engine, "policy", None), "mode", requested),
        "error": None if ok else f"Engine rejected mode: {requested}",
    }


@remediation_router.get("/policy")
async def remediation_policy():
    """
    Return the current remediation policy document.

    Prefers the live engine's policy; falls back to loading it from
    disk. Returns an explicit ``available: false`` payload when neither
    is possible.
    """
    engine = get_remediation_engine()
    policy_obj = getattr(engine, "policy", None) if engine is not None else None

    if policy_obj is not None:
        raw = getattr(policy_obj, "raw", None)
        if raw is not None:
            return {"available": True, "source": "engine", "policy": raw}

    if load_policy is not None:
        try:
            loaded = load_policy(REMEDIATION_POLICY_PATH)
            raw = getattr(loaded, "raw", None) or loaded
            return {"available": True, "source": "file", "policy": raw}
        except Exception as e:
            return {
                "available": False,
                "source": "file",
                "error": f"Failed to load policy: {type(e).__name__}: {e}",
            }

    return {
        "available": False,
        "source": "none",
        "error": "Remediation module is not importable and no engine is attached.",
    }


@remediation_router.get("/history")
async def remediation_history(limit: int = 100):
    """
    Return recent remediation events.

    Order of preference:
      1. Engine-provided history (``engine.get_history()`` or
         ``engine.history``), which may be persisted.
      2. In-memory ring populated via ``record_remediation_event``.

    ``limit`` is clamped to ``[1, REMEDIATION_HISTORY_MAX]``.
    """
    limit = max(1, min(int(limit), REMEDIATION_HISTORY_MAX))

    engine = get_remediation_engine()

    if engine is not None:
        if hasattr(engine, "get_history"):
            try:
                entries = list(engine.get_history(limit=limit))
                return {
                    "source": "engine",
                    "count": len(entries),
                    "entries": entries,
                }
            except Exception as e:
                print(f"[REMEDIATION] engine.get_history failed: {e}")

        engine_history = getattr(engine, "history", None)
        if engine_history is not None:
            try:
                entries = list(engine_history)[-limit:]
                return {
                    "source": "engine",
                    "count": len(entries),
                    "entries": entries,
                }
            except Exception as e:
                print(f"[REMEDIATION] engine.history access failed: {e}")

    entries = list(_remediation_history)[-limit:]
    return {
        "source": "in_memory",
        "count": len(entries),
        "entries": entries,
    }


app.include_router(remediation_router)


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
# NEXUS_REMEDIATION_ROUTER_V1
try:
    from .remediation_api import router as _nexus_remediation_router
    try:
        app.include_router(_nexus_remediation_router)
    except NameError:
        application.include_router(_nexus_remediation_router)
except Exception:
    pass
