"""
FastAPI Backend for Nexus SCADA
Lightweight version for low‑spec systems (no Docker)
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict
from datetime import datetime

from database import db

# ==========================================
# Create FastAPI application
# ==========================================
app = FastAPI(
    title="NEXUS SCADA API",
    description="Production Backend - Phase 3",
    version="3.0.0"
)

# Allow access from Streamlit and HMI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In‑memory cache (replaces Redis for low‑spec systems)
equipment_cache: Dict[str, Dict] = {}


# ==========================================
# Public endpoints
# ==========================================
@app.get("/")
async def root():
    """General API information"""
    return {
        "name": "NEXUS SCADA API",
        "version": "3.0.0",
        "status": "running",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/health")
async def health_check():
    """Health check"""
    return {
        "status": "healthy",
        "database": "sqlite",
        "timestamp": datetime.now().isoformat()
    }


# ==========================================
# Equipment endpoints
# ==========================================
@app.get("/api/equipment")
async def get_all_equipment():
    """Get status of all equipment"""
    equipment_ids = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
    result = {}
    for eq_id in equipment_ids:
        if eq_id in equipment_cache:
            result[eq_id] = equipment_cache[eq_id]
        else:
            data = db.get_latest_data(eq_id)
            if data:
                result[eq_id] = data
    return {"equipment": result}


@app.get("/api/equipment/{equipment_id}")
async def get_equipment(equipment_id: str):
    """Get latest data for one equipment"""
    if equipment_id in equipment_cache:
        return equipment_cache[equipment_id]
    data = db.get_latest_data(equipment_id)
    if not data:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return data


@app.get("/api/equipment/{equipment_id}/history")
async def get_equipment_history(equipment_id: str, hours: int = 24):
    """Get historical data"""
    hours = min(hours, 168)  # max 7 days
    history = db.get_history(equipment_id, hours)
    return {
        "equipment_id": equipment_id,
        "hours": hours,
        "records": len(history),
        "data": history
    }


@app.post("/api/equipment/{equipment_id}/data")
async def save_equipment_data(equipment_id: str, data: Dict):
    """Save new data (called by the Modbus Server)"""
    success = db.save_equipment_data(equipment_id, data)
    if success:
        equipment_cache[equipment_id] = data
        return {"status": "saved", "timestamp": datetime.now().isoformat()}
    raise HTTPException(status_code=500, detail="Failed to save data")


# ==========================================
# Alarm endpoints
# ==========================================
@app.get("/api/alarms/active")
async def get_active_alarms():
    """Get active alarms"""
    alarms = db.get_active_alarms()
    return {"alarms": alarms, "count": len(alarms)}


@app.post("/api/alarms/{alarm_id}/acknowledge")
async def acknowledge_alarm(alarm_id: int, user: str = "system"):
    """Acknowledge an alarm"""
    success = db.acknowledge_alarm(alarm_id, user)
    if success:
        return {"status": "acknowledged", "alarm_id": alarm_id}
    raise HTTPException(status_code=404, detail="Alarm not found")


# ==========================================
# System status endpoint
# ==========================================
@app.get("/api/system/status")
async def get_system_status():
    """Overall plant status"""
    equipment_ids = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
    running_count = 0
    for eq_id in equipment_ids:
        data = equipment_cache.get(eq_id) or db.get_latest_data(eq_id)
        if data and data.get("status") == "RUNNING":
            running_count += 1

    return {
        "total_equipment": len(equipment_ids),
        "running_count": running_count,
        "active_alarms": len(db.get_active_alarms()),
        "timestamp": datetime.now().isoformat()
    }


# ==========================================
# Cleanup endpoint
# ==========================================
@app.post("/api/cleanup")
async def cleanup_data(days: int = 30):
    """Delete old data"""
    db.cleanup_old_data(days)
    return {"status": "cleanup completed", "days": days}


# ==========================================
# Run the server
# ==========================================
if __name__ == "__main__":
    import uvicorn
    # Auto‑cleanup on startup
    db.cleanup_old_data(days=30)
    print("[API] NEXUS SCADA Backend starting...")
    print("[API] Address: http://localhost:8000")
    print("[API] Docs: http://localhost:8000/docs")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")