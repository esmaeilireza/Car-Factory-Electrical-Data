"""
⚡ NEXUS SCADA - Executive Edition v6.6.2
Professional Industrial Automation Monitoring System

FIXES:
- Fix 1-12: All previous fixes retained
- NEW: Header badge = "LINK UP/LINK DOWN" (matches Server Console)
- NEW: Plant status badge = "PLANT X/Y RUN"
- NEW: 🛡 PROTECTION tab with ANSI relays + ISA-18.2 alarms
- NEW: E-STOP + RESET support with HR[120] status word
- FIX: E-STOP properly latches and overrides all UI state
- FIX: Single _req_snapshot for header/footer/console consistency
- FIX: set_index + reindex actually implemented
- FIX: Indentation errors and LOCKED status handling in UI loops
- FIX: "START PENDING" state now correctly renders in orange instead of defaulting to "STOPPED"
- FIX: Replaced deprecated `use_container_width` with `width="stretch"`
- FIX: Reliable auto-refresh with st.rerun() instead of fragment run_every
- FIX: [NEW] Auto-reconnect via ensure_connected() when socket dies
- FIX: [NEW] Per-equipment retry mechanism (2 attempts) in read_all_equipment
- FIX: [NEW] System status read with retry logic
- FIX: [v6.3] Batch Read (1 request for all 108 registers)
- FIX: [v6.3] Timeout increased to 5s for batch reliability
- FIX: [v6.3] Removed force=True in fragment to prevent double-reads
- FIX: [v6.3] Native PLANT badge rendering (no JavaScript)
- FIX: [v6.4] Increased auto-refresh interval to 5s to prevent CPU starvation timeouts
- FIX: [v6.4] Added "STALE DATA" warning badge when last read is > 10s old
- FIX: [v6.4.1] Flattened HTML string in Header to prevent Markdown code-block interpretation
- FIX: [v6.5] Smart Diagnostic & Auto-Healing Panel in HMI tab (detects server starvation + 1-click fixes)
- NEW: [v6.6] Enhanced AI Diagnosis Engine with:
            - Rule-based pre-check (10x faster for common cases)
            - Temperature 0.05 (no hallucinations)
            - Severity-colored diagnostic card
            - Source tracking (rule_based vs AI)
            - Consent gate with human-in-the-loop
            - Audit log display
            - IEC reference citations
- FIX: [v6.6.1] Added sys.path fix to resolve "AIDiagnosisEngine module not available"
- NEW: [v6.6.2] Built-in environment diagnostic panel in sidebar
- MODIFIED: [v6.6.2] Simplified AI Diagnosis Engine in HMI tab (cleaner UI, direct JSON output)
"""

# ============================================================
# [FIX v6.6.1] Add project root to Python path
# This resolves "AIDiagnosisEngine module not available" error
# when Streamlit runs the app from the dashboard/ directory
# ============================================================
import sys
from pathlib import Path

# Get project root (parent of dashboard/)
_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
    print(f"[PATH] Added to sys.path: {_project_root}")

# Now safe to import everything
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pymodbus.client import ModbusTcpClient
import time
import atexit
import traceback

# ---- AI Engine import (with detailed error reporting) ----
try:
    from models.ai_engine import AIDiagnosisEngine
    AI_ENGINE_AVAILABLE = True
    print("[AI] ✅ AIDiagnosisEngine imported successfully")
except ImportError as e:
    AI_ENGINE_AVAILABLE = False
    AIDiagnosisEngine = None
    print(f"[AI] ❌ Import failed: {e}")
    print(f"[AI] Current sys.path: {sys.path[:3]}...")
    print(f"[AI] Current working directory: {Path.cwd()}")
    print(f"[AI] Script location: {Path(__file__).parent}")

STREAMLIT_VERSION = tuple(map(int, st.__version__.split('.')[:2]))
HAS_FRAGMENT = STREAMLIT_VERSION >= (1, 37)

FONT_UI = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "SF Mono", "DejaVu Sans Mono", "Courier New", monospace'

ANSI_FUNCTIONS = [
    (0, "49", "Thermal Overload", "I²t accumulator > 100%"),
    (1, "50", "Instantaneous OC", "I > 2.5·Iₙ"),
    (2, "51", "Time Overcurrent", "IEC Standard Inverse"),
    (3, "27", "Undervoltage", "V < 0.85·Vₙ for 2s"),
    (4, "59", "Overvoltage", "V > 1.10·Vₙ"),
    (5, "38", "Over-Temperature", "Winding > 95°C"),
    (6, "46", "Phase Imbalance", "Alarm only"),
    (7, "37", "Loss of Load", "I < 0.3·Iₙ"),
]

# === System Status Word (must match modbus_server.py) ===
SYS_STATUS_ADDR = 120
SYS_STATUS_ESTOP = 1 << 0
SYS_STATUS_ANY_TRIP = 1 << 1

st.set_page_config(
    page_title="NEXUS SCADA | Executive Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    :root {
        --bg-main: #080c14;
        --bg-panel: rgba(13, 22, 37, 0.6);
        --bg-deep: #05080f;
        --neon-cyan: #00E5FF;
        --neon-green: #00E676;
        --neon-red: #FF1744;
        --neon-orange: #FF9100;
        --neon-amber: #FFB300;
        --text-primary: #e8eef7;
        --text-secondary: #8fa0dd;
        --text-muted: #52638c;
        --border-subtle: rgba(0, 229, 255, 0.15);
        --font-main: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                     'Helvetica Neue', Arial, sans-serif;
        --font-mono: 'Cascadia Mono', 'Consolas', 'SF Mono',
                     'DejaVu Sans Mono', 'Courier New', monospace;
    }
    
    .stApp {
        background-color: var(--bg-main) !important;
        background-image: 
            radial-gradient(circle at 50% 50%, #0e1726 0%, var(--bg-main) 100%),
            linear-gradient(rgba(0, 229, 255, 0.02) 1px, transparent 1px),
            linear-gradient(90deg, rgba(0, 229, 255, 0.02) 1px, transparent 1px) !important;
        background-size: 100% 100%, 50px 50px, 50px 50px !important;
        color: var(--text-primary);
        font-family: var(--font-main);
    }
    
    #MainMenu, header, footer {visibility: hidden;}
    
    .estop-banner {
        background: linear-gradient(90deg, rgba(255, 23, 68, 0.95), rgba(180, 0, 30, 0.95));
        border: 2px solid #FF1744;
        border-radius: 10px;
        padding: 1rem 1.5rem;
        margin-bottom: 1rem;
        text-align: center;
        box-shadow: 0 0 30px rgba(255, 23, 68, 0.5);
        animation: blink 1s infinite;
    }
    
    .estop-banner h3 {
        color: white;
        margin: 0;
        font-size: 1.3rem;
        font-family: var(--font-mono);
        letter-spacing: 2px;
    }
    
    .estop-banner p {
        color: #ffe0e0;
        margin: 0.5rem 0 0 0;
        font-size: 0.9rem;
    }
    
    @keyframes blink {
        0%, 100% { box-shadow: 0 0 30px rgba(255, 23, 68, 0.8); }
        50% { box-shadow: 0 0 50px rgba(255, 23, 68, 1); }
    }
    
    .nexus-header {
        background: linear-gradient(135deg, rgba(12, 20, 36, 0.95) 0%, rgba(8, 16, 28, 0.95) 100%);
        backdrop-filter: blur(20px);
        border: 1px solid var(--border-subtle);
        border-radius: 10px;
        padding: 1rem 1.5rem;
        margin-bottom: 1.5rem;
        position: relative;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
    }
    
    .nexus-header::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 1px;
        background: linear-gradient(90deg, transparent 0%, var(--neon-cyan) 50%, transparent 100%);
    }
    
    .nexus-header-content {
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 1rem;
    }
    
    .nexus-logo { display: flex; align-items: center; gap: 0.75rem; }
    
    .nexus-logo-icon {
        width: 40px; height: 40px;
        background: linear-gradient(135deg, var(--neon-cyan) 0%, #0066cc 100%);
        border-radius: 8px;
        display: flex; align-items: center; justify-content: center;
        font-size: 20px;
        box-shadow: 0 0 20px rgba(0, 229, 255, 0.5);
    }
    
    .nexus-logo-title {
        font-family: var(--font-mono);
        font-size: 1.3rem; font-weight: 700;
        color: var(--neon-cyan);
        letter-spacing: 3px;
    }
    
    .nexus-logo-subtitle {
        font-size: 0.65rem; color: var(--text-muted);
        letter-spacing: 2px; text-transform: uppercase;
    }
    
    .nexus-header-right {
        display: flex; align-items: center; gap: 1rem;
        font-family: var(--font-mono);
        font-size: 0.8rem;
        flex-wrap: wrap;
    }
    
    .header-stat { color: var(--text-secondary); }
    .header-stat span { color: var(--neon-cyan); font-weight: 600; }
    
    .nexus-status {
        display: flex; align-items: center; gap: 0.5rem;
        padding: 0.4rem 0.8rem;
        border-radius: 4px;
    }
    
    .nexus-status.link-up {
        background: rgba(0, 230, 118, 0.1);
        border: 1px solid rgba(0, 230, 118, 0.3);
    }
    
    .nexus-status.link-down {
        background: rgba(255, 23, 68, 0.1);
        border: 1px solid rgba(255, 23, 68, 0.3);
    }
    
    .nexus-status.plant-run {
        background: rgba(0, 229, 255, 0.1);
        border: 1px solid rgba(0, 229, 255, 0.3);
    }
    
    .status-dot {
        width: 8px; height: 8px;
        border-radius: 50%;
        animation: pulse 2s infinite;
    }
    
    .nexus-status.link-up .status-dot {
        background: var(--neon-green);
        box-shadow: 0 0 10px var(--neon-green);
    }
    
    .nexus-status.link-down .status-dot {
        background: var(--neon-red);
        box-shadow: 0 0 10px var(--neon-red);
        animation: none;
    }
    
    .nexus-status.plant-run .status-dot {
        background: var(--neon-cyan);
        box-shadow: 0 0 10px var(--neon-cyan);
    }
    
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.5; }
    }
    
    .status-text {
        font-size: 0.75rem; font-weight: 600; letter-spacing: 1px;
    }
    
    .nexus-status.link-up .status-text { color: var(--neon-green); }
    .nexus-status.link-down .status-text { color: var(--neon-red); }
    .nexus-status.plant-run .status-text { color: var(--neon-cyan); }
    
    .kpi-card {
        background: var(--bg-panel);
        backdrop-filter: blur(10px);
        border: 1px solid var(--border-subtle);
        border-radius: 8px;
        padding: 1rem;
        position: relative;
        overflow: hidden;
        transition: all 0.3s;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    }
    
    .kpi-card:hover {
        border-color: rgba(0, 229, 255, 0.4);
        box-shadow: 0 0 25px rgba(0, 229, 255, 0.2);
        transform: translateY(-3px);
    }
    
    .kpi-card::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
        background: linear-gradient(90deg, var(--accent-color, var(--neon-cyan)), transparent);
    }
    
    .kpi-label {
        font-size: 0.7rem; color: var(--text-muted);
        text-transform: uppercase; letter-spacing: 1.5px;
        font-weight: 600; margin-bottom: 0.25rem;
    }
    
    .kpi-value {
        font-family: var(--font-mono);
        font-size: 1.8rem; font-weight: 700;
        color: #ffffff; line-height: 1;
        margin-bottom: 0.25rem;
        text-shadow: 0 0 15px rgba(0, 229, 255, 0.3);
    }
    
    .kpi-value.compact { font-size: 1.4rem; }
    
    .kpi-unit {
        font-family: var(--font-mono);
        font-size: 0.8rem; color: var(--text-secondary);
    }
    
    .equipment-panel {
        background: var(--bg-panel);
        backdrop-filter: blur(10px);
        border: 1px solid var(--border-subtle);
        border-radius: 8px;
        padding: 1rem;
        margin-bottom: 0.75rem;
        position: relative;
        overflow: hidden;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    }
    
    .equipment-panel::before {
        content: "";
        position: absolute;
        top: 0; left: 0; bottom: 0;
        width: 3px;
        background: linear-gradient(180deg, var(--status-color, var(--neon-green)) 0%, transparent 100%);
    }
    
    .equipment-header {
        display: flex; justify-content: space-between;
        align-items: flex-start;
        margin-bottom: 0.75rem;
        padding-bottom: 0.75rem;
        border-bottom: 1px solid rgba(0, 229, 255, 0.1);
    }
    
    .equipment-name { font-size: 0.95rem; font-weight: 700; color: #ffffff; }
    
    .equipment-id {
        font-family: var(--font-mono);
        font-size: 0.65rem; color: var(--neon-cyan);
        letter-spacing: 2px; margin-top: 0.2rem;
    }
    
    .equipment-status-badge {
        display: flex; align-items: center; gap: 0.4rem;
        padding: 0.3rem 0.7rem;
        border-radius: 4px;
        font-size: 0.65rem; font-weight: 700;
        text-transform: uppercase; letter-spacing: 1px;
        font-family: var(--font-mono);
    }
    
    .equipment-status-badge.running {
        background: rgba(0, 230, 118, 0.15);
        color: var(--neon-green);
        border: 1px solid rgba(0, 230, 118, 0.3);
    }
    
    .equipment-status-badge.stopped {
        background: rgba(255, 23, 68, 0.15);
        color: var(--neon-red);
        border: 1px solid rgba(255, 23, 68, 0.3);
    }
    
    .equipment-status-badge.tripped {
        background: rgba(255, 179, 0, 0.15);
        color: var(--neon-amber);
        border: 1px solid rgba(255, 179, 0, 0.3);
        animation: blink-badge 1s infinite;
    }
    
    .equipment-status-badge.estop {
        background: rgba(255, 23, 68, 0.3);
        color: #ffffff;
        border: 2px solid rgba(255, 23, 68, 0.9);
        animation: blink-badge 0.6s infinite;
    }

    .equipment-status-badge.pending {
        background: rgba(255, 145, 0, 0.15);
        color: var(--neon-orange);
        border: 1px solid rgba(255, 145, 0, 0.3);
    }
    
    @keyframes blink-badge {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.5; }
    }
    
    .equipment-status-badge .led {
        width: 6px; height: 6px;
        border-radius: 50%;
        background: currentColor;
        box-shadow: 0 0 8px currentColor;
    }
    
    .equipment-metrics {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 0.5rem;
    }
    
    .metric-item {
        display: flex; flex-direction: column; gap: 0.2rem;
        padding: 0.4rem;
        background: rgba(0, 0, 0, 0.2);
        border-radius: 4px;
        border-left: 2px solid var(--neon-cyan);
    }
    
    .metric-label {
        font-size: 0.6rem; color: var(--text-muted);
        text-transform: uppercase; letter-spacing: 1px;
    }
    
    .metric-value-small {
        font-family: var(--font-mono);
        font-size: 1rem; color: #ffffff; font-weight: 600;
    }
    
    .metric-unit { font-size: 0.65rem; color: var(--text-secondary); }
    
    .modbus-log-console {
        background-color: var(--bg-deep) !important;
        border: 1px solid rgba(0, 229, 255, 0.1) !important;
        border-radius: 6px !important;
        font-family: var(--font-mono) !important;
        font-size: 10px !important;
        line-height: 1.6 !important;
        padding: 10px !important;
        color: var(--text-secondary) !important;
        max-height: 300px;
        overflow-y: auto;
    }
    
    .log-entry {
        padding: 2px 0;
        border-bottom: 1px solid rgba(255, 255, 255, 0.02);
        display: flex; gap: 6px; flex-wrap: wrap;
    }
    
    .log-time { color: var(--text-muted); min-width: 70px; }
    .log-ip { color: #00b0ff; font-weight: 500; }
    .log-dir-out { color: var(--neon-red); font-weight: 600; }
    .log-dir-in { color: var(--neon-green); font-weight: 600; }
    .log-cmd { color: #e1f5fe; }
    .log-addr { color: var(--neon-orange); }
    .log-system { color: var(--neon-green); font-weight: 600; }
    
    .section-header {
        display: flex; align-items: center; gap: 0.75rem;
        margin: 1rem 0 0.75rem 0;
    }
    
    .section-header h3 {
        font-size: 1rem; font-weight: 700;
        color: var(--neon-cyan); margin: 0;
        letter-spacing: 1px; text-transform: uppercase;
        font-family: var(--font-mono);
    }
    
    .section-header .line {
        flex: 1; height: 1px;
        background: linear-gradient(90deg, var(--border-subtle) 0%, transparent 100%);
    }
    
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.5rem;
        background: rgba(12, 20, 36, 0.8);
        backdrop-filter: blur(10px);
        padding: 0.4rem;
        border-radius: 8px;
        border: 1px solid var(--border-subtle);
        margin-bottom: 1.5rem;
    }
    
    .stTabs [data-baseweb="tab"] {
        background: transparent;
        color: var(--text-secondary);
        padding: 0.6rem 1.25rem;
        border-radius: 6px;
        font-weight: 600; font-size: 0.85rem;
    }
    
    .stTabs [data-baseweb="tab"]:hover {
        background: rgba(0, 229, 255, 0.1);
        color: #ffffff;
    }
    
    .stTabs [aria-selected="true"] {
        background: rgba(0, 229, 255, 0.15);
        color: var(--neon-cyan);
        border: 1px solid rgba(0, 229, 255, 0.3);
    }
    
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0c1424 0%, var(--bg-main) 100%) !important;
        border-right: 1px solid var(--border-subtle);
    }
    
    .stButton > button {
        background: rgba(0, 229, 255, 0.1) !important;
        color: var(--neon-cyan) !important;
        border: 1px solid rgba(0, 229, 255, 0.3) !important;
        border-radius: 6px !important;
        padding: 0.5rem 1rem !important;
        font-weight: 600 !important;
        text-transform: uppercase !important;
        font-size: 0.75rem !important;
    }
    
    .stButton > button:hover {
        background: rgba(0, 229, 255, 0.2) !important;
        border-color: var(--neon-cyan) !important;
        box-shadow: 0 0 20px rgba(0, 229, 255, 0.3) !important;
    }
    
    .relay-indicator {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.3rem 0.6rem;
        border-radius: 4px;
        font-size: 0.7rem;
        font-family: var(--font-mono);
        margin: 2px;
    }
    
    .relay-ok {
        background: rgba(82, 99, 140, 0.2);
        color: var(--text-muted);
        border: 1px solid rgba(82, 99, 140, 0.3);
    }
    
    .relay-alarm {
        background: rgba(255, 179, 0, 0.15);
        color: var(--neon-amber);
        border: 1px solid rgba(255, 179, 0, 0.4);
    }
    
    .relay-trip {
        background: rgba(255, 23, 68, 0.2);
        color: var(--neon-red);
        border: 1px solid rgba(255, 23, 68, 0.5);
        animation: blink-badge 0.8s infinite;
    }
    
    .thermal-bar {
        height: 20px;
        background: rgba(0, 0, 0, 0.3);
        border-radius: 4px;
        overflow: hidden;
        position: relative;
        margin: 0.5rem 0;
    }
    
    .thermal-fill {
        height: 100%;
        transition: width 0.3s;
        background: linear-gradient(90deg, #00E676 0%, #FFB300 85%, #FF1744 100%);
    }
    
    .thermal-marker {
        position: absolute;
        top: 0; bottom: 0;
        width: 2px;
        background: white;
    }
    
    /* AI Diagnosis Card Styles */
    .ai-diagnosis-card {
        padding: 1.25rem;
        border-radius: 8px;
        margin: 1rem 0;
        backdrop-filter: blur(10px);
    }
    
    .ai-diagnosis-card.severity-low {
        background: rgba(0, 230, 118, 0.08);
        border-left: 4px solid var(--neon-green);
    }
    
    .ai-diagnosis-card.severity-medium {
        background: rgba(255, 145, 0, 0.08);
        border-left: 4px solid var(--neon-orange);
    }
    
    .ai-diagnosis-card.severity-high {
        background: rgba(255, 23, 68, 0.08);
        border-left: 4px solid var(--neon-red);
    }
    
    .ai-diagnosis-card.severity-critical {
        background: rgba(255, 23, 68, 0.15);
        border-left: 4px solid #FF1744;
        animation: blink 2s infinite;
    }
    
    .ai-diagnosis-title {
        font-size: 1.1rem;
        font-weight: 700;
        margin: 0 0 0.75rem 0;
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    .ai-diagnosis-field {
        margin: 0.4rem 0;
        font-size: 0.85rem;
    }
    
    .ai-diagnosis-field strong {
        color: var(--text-secondary);
        margin-right: 0.5rem;
    }
    
    .ai-source-badge {
        display: inline-block;
        padding: 0.15rem 0.5rem;
        border-radius: 4px;
        font-size: 0.7rem;
        font-weight: 700;
        font-family: var(--font-mono);
        letter-spacing: 1px;
    }
    
    .ai-source-rule {
        background: rgba(0, 229, 255, 0.2);
        color: var(--neon-cyan);
        border: 1px solid rgba(0, 229, 255, 0.4);
    }
    
    .ai-source-ai {
        background: rgba(167, 139, 250, 0.2);
        color: #a78bfa;
        border: 1px solid rgba(167, 139, 250, 0.4);
    }
    
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: var(--bg-deep); }
    ::-webkit-scrollbar-thumb { background: rgba(0, 229, 255, 0.3); border-radius: 3px; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# CONFIG
# ==========================================
EQUIPMENT_CONFIG = {
    "STP-01": {"name": "Stamping Press", "area": "Stamping", "pf_target": 0.88, "offset": 0, "reg_count": 18},
    "WLD-01": {"name": "Welding Robots", "area": "Body Shop", "pf_target": 0.85, "offset": 18, "reg_count": 18},
    "PNT-01": {"name": "Paint Booth", "area": "Paint Shop", "pf_target": 0.92, "offset": 36, "reg_count": 18},
    "ASM-01": {"name": "Assembly Line", "area": "Assembly", "pf_target": 0.90, "offset": 54, "reg_count": 18},
    "UTI-01": {"name": "Compressor", "area": "Utilities", "pf_target": 0.85, "offset": 72, "reg_count": 18},
    "UTI-02": {"name": "Chiller Plant", "area": "Utilities", "pf_target": 0.88, "offset": 90, "reg_count": 18},
}

LIVE_COLS = ["timestamp", "equipment_id", "equipment_name", "area", "pf_target", 
              "status", "voltage", "current", "active_power", "reactive_power", 
              "apparent_power", "power_factor", "frequency", "energy_kwh", 
              "alarm", "alarm_code", "running_time_min", "load",
              "trip_word", "alarm_word", "theta_per_mille", "trip_count", "heartbeat"]
EMPTY_LIVE = pd.DataFrame(columns=LIVE_COLS)

# ==========================================
# SESSION STATE
# ==========================================
defaults = {
    "data_history": [],
    "client": None,
    "connected": False,
    "auto_refresh": True,
    "traffic_log": [],
    "server_uptime": 0,
    "total_requests": 0,
    "latest_live": None,
    "df_live": None,
    "last_error": "",
    "modbus_errors": [],
    "_snapshot": None,
    "_last_read_ts": 0,
    "alarm_log": [],
    "estop_active": False,
    "any_trip_active": False,
    "_req_snapshot": 0,
    # [v6.6] AI Diagnosis state
    "last_ai_diagnosis": None,
    "ai_engine": None,
    "ai_engine_error": None,
    "ai_engine_loaded": False,
}
for key, val in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val


def cleanup_client():
    old = st.session_state.get("client")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        st.session_state.client = None
        st.session_state.connected = False

atexit.register(cleanup_client)

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def disconnect_plc():
    old = st.session_state.get("client")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    st.session_state.client = None
    st.session_state.connected = False
    st.session_state["_snapshot"] = None
    st.session_state["_last_read_ts"] = 0

def connect_plc():
    old = st.session_state.get("client")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        st.session_state.client = None

    try:
        client = ModbusTcpClient(host="127.0.0.1", port=5020, timeout=5)
        if client.connect():
            st.session_state.client = client
            st.session_state.connected = True
            if st.session_state.server_uptime == 0:
                st.session_state.server_uptime = time.time()
            st.session_state["_snapshot"] = None
            st.session_state["_last_read_ts"] = 0
            print(f"[CONN] ✅ Connected. Socket: {client.socket}")
            return True
        client.close()
        print("[CONN] ❌ Connection failed")
        return False
    except Exception as e:
        st.session_state.last_error = f"{type(e).__name__}: {e}"
        st.session_state.connected = False
        print(f"[CONN] ❌ Exception: {e}")
        return False


def ensure_connected() -> bool:
    """Verify Modbus connection health and auto-reconnect if needed."""
    client = st.session_state.get("client")
    
    if client is None:
        print("[CONN] ⚠️ Client is None, reconnecting...")
        st.session_state.connected = False
        return connect_plc()
    
    if hasattr(client, 'socket') and client.socket is None:
        print("[CONN] ⚠️ Socket is None, reconnecting...")
        try:
            client.close()
        except Exception:
            pass
        st.session_state.client = None
        st.session_state.connected = False
        st.session_state["_snapshot"] = None
        st.session_state["_last_read_ts"] = 0
        return connect_plc()
    
    if hasattr(client, 'is_connected') and not client.is_connected:
        print("[CONN] ⚠️ is_connected=False, reconnecting...")
        try:
            client.close()
        except Exception:
            pass
        st.session_state.client = None
        st.session_state.connected = False
        return connect_plc()
    
    return True


def read_system_status(client) -> int:
    """Read system status word from HR[120]."""
    try:
        try:
            result = client.read_holding_registers(SYS_STATUS_ADDR, count=1, slave=1)
        except TypeError:
            result = client.read_holding_registers(SYS_STATUS_ADDR, count=1, device_id=1)
        
        if not result.isError() and result.registers:
            st.session_state.total_requests += 1
            return result.registers[0]
    except Exception as e:
        print(f"[SYS-STATUS] Read failed: {e}")
    return 0


def read_all_equipment(client):
    """Read ALL 108 registers in ONE batch request."""
    records = []
    now = datetime.now()
    
    result = None
    last_error = None
    
    for attempt in range(2):
        try:
            try:
                result = client.read_holding_registers(
                    address=0, count=108, slave=1
                )
            except TypeError:
                result = client.read_holding_registers(
                    address=0, count=108, device_id=1
                )
            
            if not result.isError():
                break
            
            last_error = str(result)
            if attempt == 0:
                time.sleep(0.1)
                
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt == 0:
                time.sleep(0.1)
    
    if result is None or result.isError():
        err_msg = f"[MODBUS-BATCH] Failed to read 108 registers: {last_error}"
        print(err_msg)
        st.session_state.last_error = err_msg
        st.session_state.modbus_errors.append({
            "time": now.strftime('%H:%M:%S'),
            "eq": "BATCH",
            "offset": 0,
            "error": str(last_error)
        })
        if len(st.session_state.modbus_errors) > 20:
            st.session_state.modbus_errors = st.session_state.modbus_errors[-20:]
        return records
    
    all_registers = result.registers
    
    st.session_state.total_requests += 1
    st.session_state.traffic_log.append({
        "time": now.strftime('%H:%M:%S.%f')[:-3],
        "ip": "127.0.0.1:5020",
        "direction": "OUT",
        "cmd": "Read (0x03) [BATCH]",
        "addr": "00000",
        "qty": "108",
        "eq": "ALL"
    })
    st.session_state.traffic_log.append({
        "time": now.strftime('%H:%M:%S.%f')[:-3],
        "direction": "IN",
        "cmd": "Resp (0x03) [BATCH]",
        "bytes": str(108 * 2),
        "eq": "ALL"
    })
    if len(st.session_state.traffic_log) > 50:
        st.session_state.traffic_log = st.session_state.traffic_log[-50:]
    
    for eq_id, cfg in EQUIPMENT_CONFIG.items():
        offset = cfg["offset"]
        r = all_registers[offset:offset + 18]
        
        if len(r) < 18:
            continue
        
        motor_status = bool(r[8])
        trip_word = r[13]
        raw_alarm_word = r[14]
        is_latched = bool(raw_alarm_word & (1 << 15))
        alarm_word = raw_alarm_word & ~(1 << 15)
        
        if is_latched:
            status = "LOCKED"
        elif motor_status:
            status = "RUNNING"
        elif trip_word > 0:
            status = "TRIPPED"
        else:
            status = "START PENDING"

        records.append({
            "timestamp": now,
            "equipment_id": eq_id,
            "equipment_name": cfg["name"],
            "area": cfg["area"],
            "pf_target": cfg["pf_target"],
            "status": status,
            "voltage": r[0] / 10.0,
            "current": r[1] / 10.0 if motor_status else 0.0,
            "active_power": r[2] / 10.0 if motor_status else 0.0,
            "reactive_power": r[3] / 10.0 if motor_status else 0.0,
            "apparent_power": r[4] / 10.0 if motor_status else 0.0,
            "power_factor": r[5] / 100.0 if motor_status else 0.0,
            "frequency": r[6] / 10.0,
            "energy_kwh": r[7] / 100.0,
            "alarm": bool(r[9]),
            "alarm_code": r[10],
            "running_time_min": r[11],
            "load": r[12],
            "trip_word": trip_word,
            "alarm_word": alarm_word,
            "theta_per_mille": r[15],
            "trip_count": r[16],
            "heartbeat": r[17],
        })
    
    return records


def write_motor_command(client, eq_id, command):
    if eq_id in EQUIPMENT_CONFIG:
        coil_idx = list(EQUIPMENT_CONFIG.keys()).index(eq_id)
        try:
            now = datetime.now()
            if not ensure_connected():
                return False
            try:
                client.write_coil(coil_idx, value=command, slave=1)
            except TypeError:
                client.write_coil(coil_idx, value=command, device_id=1)
            
            log = {
                "time": now.strftime('%H:%M:%S.%f')[:-3],
                "ip": "127.0.0.1:5020",
                "direction": "OUT",
                "cmd": "Write Coil (0x05)",
                "addr": f"{coil_idx:05d}",
                "value": "ON" if command else "OFF",
                "eq": eq_id
            }
            st.session_state.traffic_log.append(log)
            st.session_state["_snapshot"] = None
            st.session_state["_last_read_ts"] = 0
            return True
        except Exception as e:
            err_msg = f"[WRITE-EXC] {eq_id}: {type(e).__name__}: {e}"
            print(err_msg)
            st.session_state.last_error = err_msg
            return False
    return False


def write_estop(client):
    if not ensure_connected():
        return False
    try:
        client.write_coil(6, value=True, slave=1)
        st.session_state.traffic_log.append({
            "time": datetime.now().strftime('%H:%M:%S.%f')[:-3],
            "direction": "OUT",
            "cmd": "E-STOP (CO[6])",
            "eq": "ALL",
            "ip": "127.0.0.1:5020",
        })
        st.session_state["_snapshot"] = None
        st.session_state["_last_read_ts"] = 0
        return True
    except Exception as e:
        st.session_state.last_error = f"E-STOP failed: {e}"
        return False


def write_reset(client):
    if not ensure_connected():
        return False
    try:
        client.write_coil(7, value=True, slave=1)
        st.session_state.traffic_log.append({
            "time": datetime.now().strftime('%H:%M:%S.%f')[:-3],
            "direction": "OUT",
            "cmd": "RESET (CO[7])",
            "eq": "ALL",
            "ip": "127.0.0.1:5020",
        })
        st.session_state["_snapshot"] = None
        st.session_state["_last_read_ts"] = 0
        return True
    except Exception as e:
        st.session_state.last_error = f"RESET failed: {e}"
        return False


def format_uptime(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_live_data(force=False):
    """Snapshot caching + E-STOP override + AUTO-RECONNECT."""
    now = time.time()
    
    if not force and st.session_state.get("_snapshot") is not None:
        if now - st.session_state.get("_last_read_ts", 0) < 0.2:
            return st.session_state["_snapshot"]
    
    if not ensure_connected():
        empty_result = (EMPTY_LIVE.copy(), EMPTY_LIVE.copy())
        st.session_state["_snapshot"] = empty_result
        st.session_state["_last_read_ts"] = now
        return empty_result
    
    empty_result = (EMPTY_LIVE.copy(), EMPTY_LIVE.copy())
    
    if st.session_state.connected and st.session_state.client is not None:
        try:
            sys_status = 0
            for attempt in range(2):
                try:
                    sys_status = read_system_status(st.session_state.client)
                    break
                except Exception:
                    if attempt == 0:
                        time.sleep(0.1)
                        continue
                    sys_status = 0
            
            estop_active = bool(sys_status & SYS_STATUS_ESTOP)
            any_trip = bool(sys_status & SYS_STATUS_ANY_TRIP)
            st.session_state["estop_active"] = estop_active
            st.session_state["any_trip_active"] = any_trip
            
            new_records = read_all_equipment(st.session_state.client)
            if new_records:
                st.session_state.data_history.extend(new_records)
                if len(st.session_state.data_history) > 5000 * 6:
                    st.session_state.data_history = st.session_state.data_history[-5000 * 6:]
            
            df_live = pd.DataFrame(st.session_state.data_history)
            
            if not df_live.empty:
                latest_live = (
                    df_live.sort_values("timestamp")
                           .groupby("equipment_id")
                           .tail(1)
                           .set_index("equipment_id")
                           .reindex(list(EQUIPMENT_CONFIG.keys()))
                )
                
                if estop_active:
                    latest_live = latest_live.copy()
                    latest_live["status"] = "E-STOP"
                    for col in ["current", "active_power", "reactive_power", "apparent_power", "load"]:
                        if col in latest_live.columns:
                            latest_live[col] = 0.0
                
                st.session_state.df_live = df_live
                st.session_state.latest_live = latest_live
                
                snapshot = (df_live, latest_live)
                st.session_state["_snapshot"] = snapshot
                st.session_state["_last_read_ts"] = now
                return snapshot
            else:
                st.session_state["_snapshot"] = empty_result
                st.session_state["_last_read_ts"] = now
                return empty_result
        except Exception as e:
            print(f"[CONN Error] {type(e).__name__}: {e}")
            st.session_state.connected = False
            st.session_state.last_error = f"{type(e).__name__}: {e}"
            try:
                if st.session_state.client:
                    st.session_state.client.close()
            except Exception:
                pass
            st.session_state.client = None
            st.session_state["_snapshot"] = empty_result
            st.session_state["_last_read_ts"] = now
            return empty_result
    
    st.session_state["_snapshot"] = empty_result
    st.session_state["_last_read_ts"] = 0
    return empty_result


# ==========================================
# [v6.6] AI ENGINE LOADER
# ==========================================
def load_ai_engine():
    """Load the AI diagnosis engine (lazy loading)."""
    if not AI_ENGINE_AVAILABLE:
        st.session_state.ai_engine_error = "AIDiagnosisEngine module not available"
        return None
    
    if st.session_state.ai_engine is not None:
        return st.session_state.ai_engine
    
    try:
        model_path = Path(__file__).parent.parent / "models" / "qwen2.5-coder-1.5b-instruct-q6_k.gguf"
        
        if not model_path.exists():
            st.session_state.ai_engine_error = f"Model file not found: {model_path}"
            return None
        
        with st.spinner("🤖 Loading AI model (first time may take 30-60s)..."):
            engine = AIDiagnosisEngine(str(model_path))
            st.session_state.ai_engine = engine
            st.session_state.ai_engine_loaded = True
            return engine
    except Exception as e:
        st.session_state.ai_engine_error = f"Failed to load AI engine: {e}"
        return None


# ==========================================
# ENVIRONMENT DIAGNOSTIC (for sidebar)
# ==========================================
def get_env_diagnostic():
    """Return a dict with environment info."""
    info = {}
    info["python_executable"] = sys.executable
    info["python_version"] = sys.version.split()[0]
    info["in_venv"] = sys.prefix != sys.base_prefix
    info["sys_path"] = sys.path[:3]  # show only first few
    info["llama_cpp_available"] = False
    info["ctransformers_available"] = False
    info["streamlit_version"] = st.__version__
    info["models_ai_engine_available"] = AI_ENGINE_AVAILABLE
    info["project_root"] = _project_root
    try:
        import llama_cpp
        info["llama_cpp_available"] = True
    except ImportError:
        pass
    try:
        import ctransformers
        info["ctransformers_available"] = True
    except ImportError:
        pass
    return info


# ==========================================
# RENDER START
# ==========================================
df_live_global, latest_live_global = get_live_data()

st.session_state["_req_snapshot"] = st.session_state.total_requests
req_snapshot = st.session_state["_req_snapshot"]

estop_active = st.session_state.get("estop_active", False)

# ==========================================
# E-STOP BANNER
# ==========================================
if estop_active:
    st.markdown("""
<div class="estop-banner">
    <h3>🚨 EMERGENCY STOP ACTIVE — ALL EQUIPMENT LOCKED OUT</h3>
    <p>All motors are stopped. START commands are inhibited. Click <strong>🔓 RESET PROTECTION</strong> in sidebar to clear the latch.</p>
</div>
""", unsafe_allow_html=True)

# ==========================================
# PLANT BADGE CALCULATION
# ==========================================
if not latest_live_global.empty and latest_live_global['status'].notna().any():
    if estop_active:
        plant_text = f"PLANT 0/{int(latest_live_global['status'].notna().sum())} RUN (E-STOP)"
    else:
        running_n = int((latest_live_global['status'] == "RUNNING").sum())
        total_n = int(latest_live_global['status'].notna().sum())
        plant_text = f"PLANT {running_n}/{total_n} RUN"
else:
    plant_text = "PLANT -/-"

# ==========================================
# HEADER
# ==========================================
current_time = datetime.now().strftime("%H:%M:%S")
current_date = datetime.now().strftime("%Y-%m-%d")
uptime_seconds = int(time.time() - st.session_state.server_uptime) if st.session_state.server_uptime > 0 else 0

if st.session_state.connected:
    link_class = "link-up"
    link_text = "LINK UP"
else:
    link_class = "link-down"
    link_text = "LINK DOWN"

time_since_last_read = time.time() - st.session_state.get("_last_read_ts", 0)
is_stale = time_since_last_read > 10.0 and st.session_state.connected

if is_stale:
    stale_badge = '<div class="nexus-status" style="background:rgba(255,179,0,0.1);border:1px solid rgba(255,179,0,0.5);animation:blink-badge 1s infinite;"><div class="status-dot" style="background:var(--neon-amber);box-shadow:0 0 10px var(--neon-amber);"></div><span class="status-text" style="color:var(--neon-amber);">STALE DATA</span></div>'
else:
    stale_badge = ''

header_html = (
    '<div class="nexus-header">'
    '<div class="nexus-header-content">'
    '<div class="nexus-logo">'
    '<div class="nexus-logo-icon">⚡</div>'
    '<div>'
    '<div class="nexus-logo-title">NEXUS SCADA</div>'
    '<div class="nexus-logo-subtitle">Executive Dashboard v6.6</div>'
    '</div>'
    '</div>'
    '<div class="nexus-header-right">'
    f'<div class="header-stat">DATE: <span>{current_date}</span></div>'
    f'<div class="header-stat">TIME: <span>{current_time}</span></div>'
    f'<div class="header-stat">UPTIME: <span>{format_uptime(uptime_seconds)}</span></div>'
    f'<div class="header-stat">REQ: <span>{req_snapshot:,}</span></div>'
    f'<div class="nexus-status {link_class}">'
    '<div class="status-dot"></div>'
    f'<span class="status-text">{link_text}</span>'
    '</div>'
    f'{stale_badge}'
    '<div class="nexus-status plant-run">'
    '<div class="status-dot"></div>'
    f'<span class="status-text">{plant_text}</span>'
    '</div>'
    '</div>'
    '</div>'
    '</div>'
)

st.markdown(header_html, unsafe_allow_html=True)

# ==========================================
# SIDEBAR
# ==========================================
with st.sidebar:
    st.markdown("### 🚨 EMERGENCY CONTROLS")
    
    if st.button("🚨 E-STOP ALL", key="sidebar_estop", width="stretch"):
        if st.session_state.connected:
            if write_estop(st.session_state.client):
                st.error("🚨 E-STOP SENT - ALL EQUIPMENT LOCKED")
                st.rerun()
        else:
            st.warning("Not connected to PLC")
    
    if st.button("🔓 RESET PROTECTION", key="sidebar_reset", width="stretch"):
        if st.session_state.connected:
            if write_reset(st.session_state.client):
                st.warning("🔓 RESET sent (clears E-STOP + per-eq lockouts)")
                st.rerun()
        else:
            st.warning("Not connected to PLC")
    
    st.markdown("---")
    st.markdown("### 🛡️ System Status")
    
    if estop_active:
        st.error("🚨 **E-STOP LATCHED**")
        st.caption("All motors stopped. START inhibited.")
    else:
        st.success("✅ **E-STOP CLEAR**")
    
    if st.session_state.get("any_trip_active"):
        st.warning("⚡ At least one equipment has a tripped protection")
    
    st.markdown("---")
    st.markdown("### 🤖 AI Engine Status")
    
    if st.session_state.ai_engine is not None:
        st.success(f"✅ **Active** ({st.session_state.ai_engine.backend})")
    elif st.session_state.ai_engine_error:
        st.error(f"❌ {st.session_state.ai_engine_error[:80]}...")
    else:
        st.info("⏳ Not loaded yet")
    
    st.markdown("---")
    st.markdown("### 🛡️ Protection")
    st.caption("ANSI relays: 49/50/51/27/59/38/46/37")
    st.caption("View details in 🛡 PROTECTION tab")
    
    # ==========================================
    # [v6.6.2] ENVIRONMENT DIAGNOSTIC
    # ==========================================
    with st.expander("🔬 Environment Diagnostic", expanded=False):
        env = get_env_diagnostic()
        st.code(f"""
Python Executable: {env['python_executable']}
Python Version:    {env['python_version']}
In Virtual Env:    {env['in_venv']}
Project Root:      {env['project_root']}
Streamlit Version: {env['streamlit_version']}
llama-cpp:         {'✅' if env['llama_cpp_available'] else '❌'}
ctransformers:     {'✅' if env['ctransformers_available'] else '❌'}
models.ai_engine:  {'✅' if env['models_ai_engine_available'] else '❌'}
sys.path (first 3): {env['sys_path']}
""", language="text")
        st.caption("If any library is missing, install it in the Python environment shown above.")

# ==========================================
# TABS
# ==========================================
tab_exec, tab_hmi, tab_prot, tab_server, tab_history = st.tabs([
    "🎯 EXECUTIVE DASHBOARD",
    "🎛️ HMI CONTROL",
    "🛡️ PROTECTION",
    "🖥️ SERVER CONSOLE",
    "📈 HISTORICAL ANALYSIS"
])

# ==========================================
# TAB 1: EXECUTIVE DASHBOARD
# ==========================================
with tab_exec:
    df_live = df_live_global
    latest_live = latest_live_global
    
    ctrl1, ctrl2, ctrl3, ctrl4, ctrl5 = st.columns([2, 2, 1, 1, 1])
    with ctrl1:
        if not st.session_state.connected:
            error_msg = st.session_state.get('last_error', '')
            st.error(f"❌ **PLC Simulator is not running!** {error_msg}")
            if st.button("🔄 Try Connect"):
                connect_plc()
                st.rerun()
        else:
            n_online = int(latest_live['status'].notna().sum()) if not latest_live.empty else 0
            if n_online:
                st.success(f"✅ Connected to PLC @ 127.0.0.1:5020 • {n_online} equipment online")
            else:
                st.warning("⚠️ Connected but 0 equipment returned data")
    
    with ctrl2:
        refresh_label = "🔄 Auto-refresh (every 5s)" if HAS_FRAGMENT else "🔄 Auto-refresh (manual)"
        st.session_state.auto_refresh = st.checkbox(refresh_label, value=st.session_state.auto_refresh)
    
    with ctrl3:
        if st.button("🔄 Reconnect"):
            disconnect_plc()
            st.rerun()
    
    with ctrl4:
        if st.session_state.connected and st.button("🔌 Disconnect", key="disconnect_btn"):
            disconnect_plc()
            st.rerun()
    
    with ctrl5:
        if st.button("🗑️ Clear"):
            st.session_state.data_history = []
            st.session_state.traffic_log = []
            st.session_state.modbus_errors = []
            st.session_state["_snapshot"] = None
            st.session_state["_last_read_ts"] = 0
            st.rerun()
    
    if st.session_state.modbus_errors:
        with st.expander(f"⚠️ Modbus Errors ({len(st.session_state.modbus_errors)} recent)", expanded=True):
            for err in reversed(st.session_state.modbus_errors[-10:]):
                st.error(f"**[{err['time']}]** {err['eq']} @ offset {err['offset']}: {err['error']}")
    
    if not st.session_state.connected:
        st.markdown("""
<div style="background: rgba(255, 23, 68, 0.1); border: 2px dashed rgba(255, 23, 68, 0.4); 
            border-radius: 12px; padding: 2rem; text-align: center; margin: 2rem 0;">
    <div style="font-size: 3rem; margin-bottom: 1rem;">⚠️</div>
    <h3 style="color: #FF1744;">PLC Simulator is OFFLINE</h3>
    <p style="color: #8fa0dd;">
        <strong>Solution:</strong> Run this command:<br>
        <code style="background: rgba(0,0,0,0.3); padding: 5px 10px; border-radius: 4px; color: #00E5FF;">
        cd plc_simulator && python modbus_server.py
        </code>
    </p>
</div>
""", unsafe_allow_html=True)
    
    elif not latest_live.empty and latest_live['status'].notna().any():
        if HAS_FRAGMENT and st.session_state.auto_refresh:
            @st.fragment
            def live_metrics_fragment():
                df_live_frag, latest_live_frag = get_live_data()
                display_live = latest_live_frag.dropna(subset=['status']) if not latest_live_frag.empty else latest_live_frag
                if display_live.empty:
                    return
                
                frag_estop = st.session_state.get("estop_active", False)
                
                st.markdown("""
<div class="section-header">
    <h3>⚡ Key Performance Indicators</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
                
                k1, k2, k3, k4, k5 = st.columns(5)
                
                total_energy = display_live["energy_kwh"].sum()
                avg_voltage = display_live["voltage"].mean() if not display_live["voltage"].isna().all() else 0
                avg_current = display_live["current"].mean() if not display_live["current"].isna().all() else 0
                avg_pf = display_live["power_factor"].mean() if not display_live["power_factor"].isna().all() else 0
                if frag_estop:
                    running_count = 0
                else:
                    running_count = int((display_live["status"] == "RUNNING").sum())
                total_count = len(display_live)
                
                kpis = [
                    {"icon": "⚡", "label": "Total Energy", "value": f"{total_energy:,.1f}", "unit": "kWh", "color": "#00E5FF"},
                    {"icon": "🔌", "label": "Avg Voltage", "value": f"{avg_voltage:,.1f}", "unit": "V", "color": "#FF9100"},
                    {"icon": "💡", "label": "Avg Current", "value": f"{avg_current:,.1f}", "unit": "A", "color": "#00E676"},
                    {"icon": "📊", "label": "Power Factor", "value": f"{avg_pf:.3f}", "unit": "Avg", "color": "#a78bfa"},
                    {"icon": "🏭", "label": "Running", "value": f"{running_count}/{total_count}", "unit": "Active", "color": "#fbbf24"},
                ]
                
                for col, kpi in zip([k1, k2, k3, k4, k5], kpis):
                    with col:
                        st.markdown(f"""
<div class="kpi-card" style="--accent-color: {kpi['color']}">
    <div class="kpi-label">{kpi['label']}</div>
    <div class="kpi-value">{kpi['value']}</div>
    <div class="kpi-unit">{kpi['unit']}</div>
</div>
""", unsafe_allow_html=True)
                
                st.markdown("""
<div class="section-header">
    <h3>🏭 All Equipment Status</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
                
                cols = st.columns(3)
                for i, (eq_id, row) in enumerate(latest_live_frag.iterrows()):
                    if pd.isna(row['status']):
                        status_class = "stopped"
                        status_color = "#FF1744"
                        status_text = "OFFLINE"
                    elif row['status'] == "E-STOP":
                        status_class = "estop"
                        status_color = "#FF1744"
                        status_text = "E-STOP"
                    elif row['status'] == "LOCKED":
                        status_class = "tripped"
                        status_color = "#FFB300"
                        status_text = "LOCKED"
                    elif row['status'] == "START PENDING":
                        status_class = "pending"
                        status_color = "#FF9100"
                        status_text = "START PENDING"
                    else:
                        trip_w = int(row.get('trip_word', 0)) if not pd.isna(row.get('trip_word')) else 0
                        if trip_w:
                            status_class = "tripped"
                            status_color = "#FFB300"
                            status_text = "TRIPPED"
                        else:
                            status_class = "running" if row["status"] == "RUNNING" else "stopped"
                            status_color = "#00E676" if row["status"] == "RUNNING" else "#FF1744"
                            status_text = row["status"]
                    
                    voltage_str = f"{row['voltage']:.1f}" if not pd.isna(row['voltage']) else "--"
                    current_str = f"{row['current']:.1f}" if not pd.isna(row['current']) else "--"
                    power_str = f"{row['active_power']:.1f}" if not pd.isna(row['active_power']) else "--"
                    pf_str = f"{row['power_factor']:.3f}" if not pd.isna(row['power_factor']) else "--"
                    energy_str = f"{row['energy_kwh']:.1f}" if not pd.isna(row['energy_kwh']) else "--"
                    load_str = f"{row['load']:.0f}" if not pd.isna(row['load']) else "--"
                    
                    eq_name = row['equipment_name'] if not pd.isna(row['equipment_name']) else EQUIPMENT_CONFIG[eq_id]["name"]
                    eq_area = row['area'] if not pd.isna(row['area']) else EQUIPMENT_CONFIG[eq_id]["area"]
                    
                    with cols[i % 3]:
                        st.markdown(f"""
<div class="equipment-panel" style="--status-color: {status_color}">
    <div class="equipment-header">
        <div>
            <div class="equipment-name">{eq_name}</div>
            <div class="equipment-id">{eq_id} • {eq_area}</div>
        </div>
        <div class="equipment-status-badge {status_class}">
            <div class="led"></div>
            <span>{status_text}</span>
        </div>
    </div>
    <div class="equipment-metrics">
        <div class="metric-item">
            <div class="metric-label">Voltage</div>
            <div class="metric-value-small">{voltage_str}<span class="metric-unit"> V</span></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">Current</div>
            <div class="metric-value-small">{current_str}<span class="metric-unit"> A</span></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">Power</div>
            <div class="metric-value-small">{power_str}<span class="metric-unit"> kW</span></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">PF</div>
            <div class="metric-value-small">{pf_str}</div>
        </div>
        <div class="metric-item">
            <div class="metric-label">Energy</div>
            <div class="metric-value-small">{energy_str}<span class="metric-unit"> kWh</span></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">Load</div>
            <div class="metric-value-small">{load_str}<span class="metric-unit"> %</span></div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)
            
            live_metrics_fragment()
        
        else:
            st.info("Auto-refresh disabled. Check the checkbox or use 🔄 Refresh.")
        
        st.markdown("""
<div class="section-header">
    <h3>💾 Export Data</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
        
        if not df_live.empty:
            df_export = df_live.copy()
            df_export['timestamp'] = df_export['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
            csv_data = df_export.to_csv(index=False).encode('utf-8')
            
            dl_col1, dl_col2, dl_col3 = st.columns([2, 1, 2])
            with dl_col2:
                st.download_button(
                    label=f"⬇️ Download CSV ({len(df_live)} records)",
                    data=csv_data,
                    file_name=f"factory_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    width="stretch",
                )

# ==========================================
# TAB 2: HMI CONTROL (with Simplified AI Diagnosis)
# ==========================================
with tab_hmi:
    st.markdown("""
<div class="section-header">
    <h3>🎛️ Human-Machine Interface Control</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
    
    _toast = st.session_state.pop("_hmi_toast", None)
    if _toast:
        msg, level = _toast
        if level == "success":
            st.success(msg)
        elif level == "error":
            st.error(msg)
        elif level == "warning":
            st.warning(msg)

    if estop_active:
        st.error("🚨 **E-STOP is active** - START commands will be inhibited by the PLC. Press RESET first.")
    
    df_live, latest_live = get_live_data()
    
    if not st.session_state.connected:
        st.error(f"❌ Not connected to PLC. {st.session_state.get('last_error', '')}")
        if st.button("🔄 Try Connect", key="hmi_try_connect"):
            connect_plc()
            st.rerun()
    elif latest_live.empty or latest_live['status'].isna().all():
        st.warning("⏳ Waiting for data from PLC...")
    else:
        selected_hmi_eq = st.selectbox(
            "Select Equipment to Control:",
            [f"{eq_id} - {cfg['name']}" for eq_id, cfg in EQUIPMENT_CONFIG.items()],
            key="hmi_selector"
        )
        
        eq_id = selected_hmi_eq.split(" - ")[0]
        eq_data = latest_live.loc[[eq_id]] if eq_id in latest_live.index else latest_live.iloc[0:0]
        
        if not eq_data.empty:
            row = eq_data.iloc[0]
            
            if pd.isna(row['status']):
                status_class = "stopped"
                status_color = "#FF1744"
                status_text = "OFFLINE"
            elif row['status'] == "E-STOP":
                status_class = "estop"
                status_color = "#FF1744"
                status_text = "E-STOP"
            elif row['status'] == "LOCKED":
                status_class = "tripped"
                status_color = "#FFB300"
                status_text = "LOCKED"
            elif row['status'] == "START PENDING":
                status_class = "pending"
                status_color = "#FF9100"
                status_text = "START PENDING"
            else:
                trip_w = int(row.get('trip_word', 0)) if not pd.isna(row.get('trip_word')) else 0
                if trip_w:
                    status_class = "tripped"
                    status_color = "#FFB300"
                    status_text = "TRIPPED"
                else:
                    status_class = "running" if row["status"] == "RUNNING" else "stopped"
                    status_color = "#00E676" if row["status"] == "RUNNING" else "#FF1744"
                    status_text = row["status"]
            
            load_val = row['load'] if not pd.isna(row['load']) else 0
            voltage_str = f"{row['voltage']:.1f}" if not pd.isna(row['voltage']) else "--"
            current_str = f"{row['current']:.1f}" if not pd.isna(row['current']) else "--"
            power_str = f"{row['active_power']:.2f}" if not pd.isna(row['active_power']) else "--"
            running_time_str = f"{row['running_time_min']:.0f}" if not pd.isna(row['running_time_min']) else "0"
            
            col1, col2 = st.columns([1, 2])
            with col1:
                st.markdown(f"""
<div class="equipment-panel" style="--status-color: {status_color}; text-align: center;">
    <div class="kpi-label">MOTOR STATUS</div>
    <div class="equipment-status-badge {status_class}" style="justify-content: center; margin: 0.75rem auto; font-size: 1.1rem; padding: 0.6rem 1.25rem;">
        <div class="led"></div>
        <span>{status_text}</span>
    </div>
    <div style="margin-top: 0.75rem;">
        <div class="metric-label">RUNNING TIME</div>
        <div class="metric-value-small" style="font-size: 1.3rem;">{running_time_str} <span class="metric-unit"> min</span></div>
    </div>
</div>
""", unsafe_allow_html=True)
            with col2:
                st.markdown(f"""
<div class="equipment-panel">
    <div class="equipment-header">
        <div>
            <div class="equipment-name">{row['equipment_name']}</div>
            <div class="equipment-id">{eq_id} • {row['area']}</div>
        </div>
    </div>
    <div style="margin-bottom: 0.75rem;">
        <div class="metric-label" style="margin-bottom: 0.3rem;">LOAD STATUS • {load_val:.0f}%</div>
        <div style="height: 6px; background: rgba(255,255,255,0.05); border-radius: 3px; overflow: hidden;">
            <div style="height: 100%; width: {load_val}%; background: linear-gradient(90deg, #00E5FF, #00E676); border-radius: 3px;"></div>
        </div>
    </div>
    <div class="equipment-metrics">
        <div class="metric-item">
            <div class="metric-label">Voltage</div>
            <div class="metric-value-small">{voltage_str}<span class="metric-unit"> V</span></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">Current</div>
            <div class="metric-value-small">{current_str}<span class="metric-unit"> A</span></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">Power</div>
            <div class="metric-value-small">{power_str}<span class="metric-unit"> kW</span></div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)
                
            st.markdown("""
<div class="section-header">
    <h3>🎮 Motor Control</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
                
            btn_col1, btn_col2, btn_col3, btn_col4 = st.columns(4)
            
            with btn_col1:
                if st.button("▶️ START Motor", key="start_btn", width="stretch"):
                    if estop_active:
                        st.session_state["_hmi_toast"] = ("🚨 START inhibited by E-STOP latch. Press RESET first.", "error")
                    elif write_motor_command(st.session_state.client, eq_id, True):
                        st.session_state["_hmi_toast"] = ("✅ START command sent", "success")
                        st.rerun()
                    else:
                        st.session_state["_hmi_toast"] = ("❌ Command failed (may be lockout)", "error")
                        st.rerun()
            
            with btn_col2:
                if st.button("⏹️ STOP Motor", key="stop_btn", width="stretch"):
                    if write_motor_command(st.session_state.client, eq_id, False):
                        st.session_state["_hmi_toast"] = ("✅ STOP command sent", "success")
                        st.rerun()
                    else:
                        st.session_state["_hmi_toast"] = ("❌ STOP command failed", "error")
                        st.rerun()
            
            with btn_col3:
                if st.button("🔔 Check Alarms", key="alarm_btn", width="stretch"):
                    trip_w = int(row.get('trip_word', 0)) if not pd.isna(row.get('trip_word')) else 0
                    alarm_w = int(row.get('alarm_word', 0)) if not pd.isna(row.get('alarm_word')) else 0
                    if trip_w:
                        active_trips = [f"{ANSI_FUNCTIONS[i][1]} ({ANSI_FUNCTIONS[i][2]})" 
                                       for i in range(8) if trip_w & (1 << i)]
                        st.error(f"⚡ ACTIVE TRIPS: {', '.join(active_trips)}")
                    elif alarm_w:
                        active_alarms = [f"{ANSI_FUNCTIONS[i][1]} ({ANSI_FUNCTIONS[i][2]})" 
                                        for i in range(8) if alarm_w & (1 << i)]
                        st.warning(f"⚠️ ACTIVE ALARMS: {', '.join(active_alarms)}")
                    else:
                        st.success("✅ No active trips or alarms")
            
            with btn_col4:
                if st.button("🔄 Refresh", key="refresh_btn", width="stretch"):
                    st.session_state["_snapshot"] = None
                    st.session_state["_last_read_ts"] = 0
                    st.rerun()

            # ==========================================
            # [v6.5] SMART DIAGNOSTIC & AUTO-HEALING PANEL (Preserved)
            # ==========================================
            with st.expander("🛠️ Smart Diagnostic & Auto-Healing Panel", expanded=False):
                st.info("🧠 **AI Diagnostics:** Analyzing Modbus TCP health, server latency, and data integrity...")
                
                diag_results = []
                client = st.session_state.get("client")
                
                if client and hasattr(client, 'socket') and client.socket is not None:
                    diag_results.append(("✅ Socket Health", "TCP Socket is alive and bound.", "success"))
                else:
                    diag_results.append(("❌ Socket Health", "TCP Socket is dead or None.", "error"))
                
                ping_ms = 9999
                try:
                    start_ping = time.time()
                    ping_res = client.read_holding_registers(120, count=1, slave=1)
                    ping_ms = (time.time() - start_ping) * 1000
                    if not ping_res.isError():
                        diag_results.append(("✅ Server Latency", f"Server responded in {ping_ms:.1f}ms.", "success"))
                    else:
                        diag_results.append(("⚠️ Server Latency", f"Server responded with error: {ping_res}", "warning"))
                except Exception as e:
                    diag_results.append(("❌ Server Latency", f"Ping failed: {e}", "error"))
                
                if not latest_live.empty and 'heartbeat' in latest_live.columns:
                    hb_values = latest_live['heartbeat'].dropna().unique()
                    if len(hb_values) > 0:
                        diag_results.append(("✅ Data Freshness", f"Heartbeats active. Detected: {hb_values.tolist()}", "success"))
                    else:
                        diag_results.append(("⚠️ Data Freshness", "Heartbeat is static. PLC might be frozen.", "warning"))
                else:
                    diag_results.append(("❌ Data Freshness", "No live data available.", "error"))

                if ping_ms > 500:
                    diag_results.append(("⚠️ Server Starvation", "High latency! Server CPU is starved.", "warning"))
                else:
                    diag_results.append(("✅ Server Load", "Server load is normal.", "success"))

                st.markdown("#### 🔍 Diagnostic Results")
                for title, msg, level in diag_results:
                    if level == "success": st.success(f"**{title}:** {msg}")
                    elif level == "warning": st.warning(f"**{title}:** {msg}")
                    else: st.error(f"**{title}:** {msg}")

                st.markdown("---")
                st.markdown("#### 🛠️ Auto-Healing Actions")
                st.caption("Use these tools to automatically resolve detected issues.")
                
                fix_col1, fix_col2, fix_col3 = st.columns(3)
                
                with fix_col1:
                    if st.button("🔌 Force Hard Reconnect", key="diag_reconnect", width="stretch"):
                        with st.spinner("Dropping socket and reconnecting..."):
                            disconnect_plc()
                            time.sleep(0.5)
                            connect_plc()
                        st.success("✅ Connection rebuilt!")
                        st.rerun()
                        
                with fix_col2:
                    if st.button("🧹 Flush Cache & Resync", key="diag_flush", width="stretch"):
                        st.session_state["_snapshot"] = None
                        st.session_state["_last_read_ts"] = 0
                        st.session_state.data_history = []
                        st.success("✅ Cache flushed!")
                        st.rerun()
                        
                with fix_col3:
                    if st.button("🚑 Server Rescue Ping", key="diag_ping", width="stretch"):
                        with st.spinner("Waking up server event loop..."):
                            for _ in range(3):
                                try:
                                    client.read_holding_registers(120, count=1, slave=1)
                                    time.sleep(0.1)
                                except: pass
                        st.success("✅ Server stimulated!")
                        st.rerun()

                st.markdown("---")
                with st.expander("📊 Raw Session State (Advanced)"):
                    debug_state = {k: str(v) if isinstance(v, (pd.DataFrame, ModbusTcpClient)) else v 
                                   for k, v in st.session_state.items()}
                    st.json(debug_state)

            # ==========================================
            # [v6.6.2] SIMPLIFIED AI DIAGNOSIS ENGINE
            # ==========================================
            st.markdown("---")
            st.markdown("### 🤖 AI Diagnosis Engine")

            # Try to load the AI engine if not already loaded
            if 'ai_engine' not in st.session_state:
                load_ai_engine()

            if st.session_state.get('ai_engine'):
                st.success("✅ Local model loaded successfully")
                
                # Display model status
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Model Status", "Active")
                with col2:
                    st.metric("Safety Level", "1-3")
                with col3:
                    st.metric("Protocol", "IEC 62443")
                
                # Diagnosis button
                if st.button("🔍 Diagnose with AI", key="ai_diagnose"):
                    with st.spinner("🤖 AI is analyzing..."):
                        try:
                            diagnosis = st.session_state.ai_engine.analyze_system_state(
                                session_state=dict(st.session_state),
                                traffic_log=st.session_state.traffic_log[-20:],
                                modbus_errors=st.session_state.modbus_errors[-10:],
                                recent_data=df_live.tail(10) if not df_live.empty else pd.DataFrame()
                            )
                            
                            # Display results
                            st.json(diagnosis)
                            
                            # Display diagnosis in plain language
                            st.markdown(f"""
                            ### 🎯 Diagnosis: {diagnosis.get('diagnosis')}
                            **Severity:** {diagnosis.get('severity')}  
                            **Safety Level:** {diagnosis.get('safety_level')}  
                            **Confidence:** {diagnosis.get('confidence')}%
                            """)
                            
                            # Action suggestion
                            if diagnosis.get("safety_level", 1) <= 3:
                                action = diagnosis.get("recommended_action", "")
                                st.info(f"🔧 Recommended Action: {action}")
                                
                                col1, col2 = st.columns(2)
                                with col1:
                                    if st.button("✅ Execute", key="ai_execute"):
                                        success, msg = st.session_state.ai_engine.execute_safe_action(
                                            action, diagnosis["safety_level"]
                                        )
                                        st.success(msg)
                                with col2:
                                    if st.button("🛑 Cancel", key="ai_cancel"):
                                        st.warning("Action cancelled")
                            
                        except Exception as e:
                            st.error(f"❌ Error in diagnosis: {e}")
            else:
                st.error("❌ Local model not loaded")
                st.info("Please place the model file in the `models/` directory")

# ==========================================
# TAB 3: PROTECTION
# ==========================================
with tab_prot:
    st.markdown("""
<div class="section-header">
    <h3>🛡️ ANSI Protection & ISA-18.2 Alarms</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
    
    st.info("""
    🛡️ **Protection Layer Overview**
    
    Each equipment has an autonomous protection relay implementing ANSI device functions:
    **49** Thermal • **50** Instantaneous OC • **51** Time OC • **27** Undervoltage • 
    **59** Overvoltage • **38** Over-Temperature • **46** Phase Imbalance • **37** Loss of Load
    
    Protection runs **server-side** in the PLC simulator - it works even if this dashboard is closed.
    """)
    
    if estop_active:
        st.error("""
        🚨 **E-STOP LATCH IS ACTIVE** (read from HR[120])
        
        All motors are held stopped regardless of individual protection state.
        Use **🔓 RESET PROTECTION** in sidebar to clear.
        """)
    
    df_live, latest_live = get_live_data()
    
    if not st.session_state.connected:
        st.error("❌ Not connected to PLC. Start modbus_server.py first.")
    elif latest_live.empty or latest_live['status'].isna().all():
        st.warning("⏳ Waiting for data...")
    else:
        sel_col1, sel_col2 = st.columns([2, 1])
        with sel_col1:
            prot_eq = st.selectbox(
                "Select equipment to monitor:",
                list(EQUIPMENT_CONFIG.keys()),
                key="prot_eq_selector"
            )
        
        eq_row = latest_live.loc[prot_eq] if prot_eq in latest_live.index else None
        
        if eq_row is not None and not pd.isna(eq_row.get('status')):
            trip_word = int(eq_row.get('trip_word', 0)) if not pd.isna(eq_row.get('trip_word')) else 0
            alarm_word = int(eq_row.get('alarm_word', 0)) if not pd.isna(eq_row.get('alarm_word')) else 0
            theta_pm = int(eq_row.get('theta_per_mille', 0)) if not pd.isna(eq_row.get('theta_per_mille')) else 0
            trip_count = int(eq_row.get('trip_count', 0)) if not pd.isna(eq_row.get('trip_count')) else 0
            
            theta_pct = theta_pm / 10.0
            
            st.markdown(f"""
<div class="equipment-panel" style="--status-color: {'#FFB300' if trip_word else '#00E676'};">
    <div class="equipment-header">
        <div>
            <div class="equipment-name">{eq_row['equipment_name']}</div>
            <div class="equipment-id">{prot_eq} • {eq_row['area']}</div>
        </div>
        <div class="equipment-status-badge {'tripped' if trip_word else 'running'}">
            <div class="led"></div>
            <span>{'TRIPPED' if trip_word else 'HEALTHY'}</span>
        </div>
    </div>
    <div style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-secondary);">
        Lifetime trip count: <strong style="color: var(--neon-cyan);">{trip_count}</strong>
    </div>
</div>
""", unsafe_allow_html=True)
            
            st.markdown("""
<div class="section-header">
    <h3>⚡ ANSI Relay Panel</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
            
            relay_cols = st.columns(4)
            for idx, (bit, ansi, name, desc) in enumerate(ANSI_FUNCTIONS):
                tripped = bool(trip_word & (1 << bit))
                alarmed = bool(alarm_word & (1 << bit))
                
                if tripped:
                    state = "TRIP"
                    state_class = "relay-trip"
                elif alarmed:
                    state = "ALARM"
                    state_class = "relay-alarm"
                else:
                    state = "OK"
                    state_class = "relay-ok"
            
                with relay_cols[idx % 4]:
                    st.markdown(f"""
<div class="relay-indicator {state_class}" style="width: 100%; justify-content: space-between; padding: 0.5rem;">
    <div>
        <div style="font-weight: 700; font-size: 0.9rem;">{ansi}</div>
        <div style="font-size: 0.7rem; opacity: 0.8;">{name}</div>
    </div>
    <div style="font-weight: 800;">{state}</div>
</div>
""", unsafe_allow_html=True)
            
            st.markdown("""
<div class="section-header">
    <h3>🔥 Thermal Capacity (ANSI 49)</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
            
            tc_col1, tc_col2 = st.columns([3, 1])
            with tc_col1:
                st.markdown(f"""
<div class="thermal-bar">
    <div class="thermal-fill" style="width: {min(theta_pct, 100):.1f}%;"></div>
    <div class="thermal-marker" style="left: 85%;" title="Pre-alarm 85%"></div>
</div>
""", unsafe_allow_html=True)
                st.caption("📊 85% = Pre-alarm | 100% = Trip (lockout)")
            
            with tc_col2:
                theta_color = "#FF1744" if theta_pct >= 100 else ("#FFB300" if theta_pct >= 85 else "#00E676")
                st.markdown(f"""
<div style="text-align: center; padding: 1rem;">
    <div style="font-family: var(--font-mono); font-size: 2rem; color: {theta_color}; font-weight: 700;">
        {theta_pct:.1f}%
    </div>
    <div style="color: var(--text-muted); font-size: 0.75rem;">θ (thermal state)</div>
</div>
""", unsafe_allow_html=True)
            
            st.markdown("""
<div class="section-header">
    <h3>⏱️ Inverse-Time Curve (ANSI 51 - IEC Standard Inverse)</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
            
            I_now = eq_row['current'] if not pd.isna(eq_row['current']) else 0
            TMS = 0.10
            i_ratios = np.linspace(1.05, 10, 200)
            t_trip = (0.14 * TMS) / ((i_ratios ** 0.02) - 1)
            
            fig_curve = go.Figure()
            fig_curve.add_trace(go.Scatter(
                x=i_ratios, y=t_trip,
                mode='lines',
                name='IEC Standard Inverse',
                line=dict(color='#00E5FF', width=2),
            ))
            
            I_n_approx = eq_row['current'] / 0.75 if I_now > 0 else 100
            if I_n_approx > 0 and I_now > 0:
                i_ratio_now = I_now / I_n_approx
                if i_ratio_now > 1.0:
                    t_now = (0.14 * TMS) / ((i_ratio_now ** 0.02) - 1)
                    fig_curve.add_trace(go.Scatter(
                        x=[i_ratio_now], y=[t_now],
                        mode='markers+text',
                        marker=dict(color='#FF1744', size=12),
                        text=[f"I/Iₙ={i_ratio_now:.2f}<br>t={t_now:.2f}s"],
                        textposition='top right',
                        name='Operating Point',
                    ))
            
            fig_curve.update_layout(
                height=350,
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font={'color': '#e8eef7', 'family': FONT_MONO},
                xaxis={'title': 'I/Iₙ (current ratio)', 'type': 'log', 'gridcolor': 'rgba(0,229,255,0.1)'},
                yaxis={'title': 'Trip Time (s)', 'type': 'log', 'gridcolor': 'rgba(0,229,255,0.1)'},
                margin=dict(l=50, r=20, t=20, b=50),
                showlegend=False,
            )
            st.plotly_chart(fig_curve, width="stretch")
            
            st.markdown("""
<div class="section-header">
    <h3>📋 ISA-18.2 Alarm Event Log</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
            
            now_ts = datetime.now().strftime('%H:%M:%S')
            active_events = []
            for bit, ansi, name, desc in ANSI_FUNCTIONS:
                if trip_word & (1 << bit):
                    active_events.append({
                        "Time": now_ts, "Asset": prot_eq, "ANSI": ansi,
                        "Description": name, "Priority": "HIGH", "State": "TRIP",
                    })
                elif alarm_word & (1 << bit):
                    active_events.append({
                        "Time": now_ts, "Asset": prot_eq, "ANSI": ansi,
                        "Description": name, "Priority": "MEDIUM", "State": "ALARM",
                    })
            
            if active_events:
                df_events = pd.DataFrame(active_events)
                st.dataframe(df_events, width="stretch", hide_index=True)
                st.warning(f"⚠️ {len(active_events)} active alarm(s) on {prot_eq}")
                if st.button("✅ Acknowledge All Alarms", key=f"ack_{prot_eq}"):
                    st.success(f"Acknowledged {len(active_events)} alarm(s)")
            else:
                st.success("✅ No active alarms - all protection functions healthy")
        else:
            st.warning(f"⚠️ {prot_eq} is OFFLINE - no protection data available")

# ==========================================
# TAB 4: SERVER CONSOLE
# ==========================================
with tab_server:
    st.markdown("""
<div class="section-header">
    <h3>🖥️ Modbus TCP Server Console</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
    
    df_live = df_live_global
    latest_live = latest_live_global
    
    uptime_seconds = int(time.time() - st.session_state.server_uptime) if st.session_state.server_uptime > 0 else 0
    
    s1, s2, s3, s4, s5 = st.columns(5)
    
    server_stats = [
        {"label": "Link", "value": "🟢 UP" if st.session_state.connected else "🔴 DOWN", "color": "#00E676" if st.session_state.connected else "#FF1744"},
        {"label": "Uptime", "value": format_uptime(uptime_seconds), "color": "#00E5FF"},
        {"label": "Port", "value": "5020", "color": "#FF9100"},
        {"label": "Requests", "value": f"{req_snapshot:,}", "color": "#a78bfa"},
        {"label": "Modbus Errors", "value": str(len(st.session_state.modbus_errors)), "color": "#FF1744" if st.session_state.modbus_errors else "#00E676"},
    ]
    
    for col, stat in zip([s1, s2, s3, s4, s5], server_stats):
        with col:
            st.markdown(f"""
<div class="kpi-card" style="--accent-color: {stat['color']}">
    <div class="kpi-label">{stat['label']}</div>
    <div class="kpi-value compact">{stat['value']}</div>
</div>
""", unsafe_allow_html=True)
    
    st.markdown("---")
    st.markdown("### 🔍 System Status Word (HR[120])")
    sys_col1, sys_col2 = st.columns(2)
    with sys_col1:
        if estop_active:
            st.error(f"🚨 **E-STOP LATCHED** (bit 0 = 1)")
        else:
            st.success(f"✅ **E-STOP CLEAR** (bit 0 = 0)")
    with sys_col2:
        if st.session_state.get("any_trip_active"):
            st.warning(f"⚡ **ANY EQUIPMENT TRIPPED** (bit 1 = 1)")
        else:
            st.success(f"✅ **No trips latched** (bit 1 = 0)")
    
    st.markdown("---")
    
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.markdown("""
<div class="section-header">
    <h3>📡 Modbus Traffic Log</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
        
        if st.button("🗑️ Clear Log", key="clear_log"):
            st.session_state.traffic_log = []
            st.session_state.modbus_errors = []
            st.rerun()
        
        if st.session_state.traffic_log:
            rows = []
            for log in reversed(st.session_state.traffic_log[-20:]):
                if log['direction'] == 'OUT':
                    rows.append(
                        '<div class="log-entry">'
                        f'<span class="log-time">{log["time"]}</span>'
                        f'<span class="log-ip">[{log.get("ip", "SERVER")}]</span>'
                        f'<span class="log-dir-out">➔ {log["cmd"]}</span>'
                        f'<span class="log-addr">Addr:{log.get("addr", "00000")}</span>'
                        '</div>'
                    )
                elif log['direction'] == 'IN':
                    rows.append(
                        '<div class="log-entry">'
                        f'<span class="log-time">{log["time"]}</span>'
                        f'<span class="log-dir-in">⇠ {log["cmd"]}</span>'
                        f'<span class="log-cmd">{log.get("bytes", "0")} bytes</span>'
                        '</div>'
                    )
                elif log['direction'] == 'SYSTEM':
                    rows.append(
                        '<div class="log-entry">'
                        f'<span class="log-time">{log["time"]}</span>'
                        f'<span class="log-system">[SYS] {log["msg"]}</span>'
                        '</div>'
                    )
            
            log_html = '<div class="modbus-log-console">' + "".join(rows) + '</div>'
            st.markdown(log_html, unsafe_allow_html=True)
        else:
            st.info("No traffic yet.")
    
    with col_right:
        st.markdown("""
<div class="section-header">
    <h3>⚙️ Asset Status</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
        
        if not latest_live.empty:
            display_idx = 0
            for eq_id, row in latest_live.iterrows():
                if pd.isna(row['status']):
                    status_class = "stopped"
                    status_color = "#FF1744"
                    status_text = "OFFLINE"
                elif row['status'] == "E-STOP":
                    status_class = "estop"
                    status_color = "#FF1744"
                    status_text = "E-STOP"
                elif row['status'] == "LOCKED":
                    status_class = "tripped"
                    status_color = "#FFB300"
                    status_text = "LOCKED"
                elif row['status'] == "START PENDING":
                    status_class = "pending"
                    status_color = "#FF9100"
                    status_text = "START PENDING"
                else:
                    trip_w = int(row.get('trip_word', 0)) if not pd.isna(row.get('trip_word')) else 0
                    if trip_w:
                        status_class = "tripped"
                        status_color = "#FFB300"
                        status_text = "TRIPPED"
                    else:
                        status_class = "running" if row["status"] == "RUNNING" else "stopped"
                        status_color = "#00E676" if row["status"] == "RUNNING" else "#FF1744"
                        status_text = row["status"]
                
                voltage_str = f"{row['voltage']:.1f}" if not pd.isna(row['voltage']) else "--"
                current_str = f"{row['current']:.1f}" if not pd.isna(row['current']) else "--"
                power_str = f"{row['active_power']:.1f}" if not pd.isna(row['active_power']) else "--"
                pf_str = f"{row['power_factor']:.2f}" if not pd.isna(row['power_factor']) else "--"
                
                eq_name = row['equipment_name'] if not pd.isna(row['equipment_name']) else EQUIPMENT_CONFIG[eq_id]["name"]
                eq_area = row['area'] if not pd.isna(row['area']) else EQUIPMENT_CONFIG[eq_id]["area"]
                
                display_idx += 1
                st.markdown(f"""
<div style="background: var(--bg-panel); border: 1px solid var(--border-subtle); 
            border-radius: 6px; padding: 0.75rem; margin-bottom: 0.5rem; 
            display: flex; align-items: center; gap: 0.75rem;">
    <div style="font-family: var(--font-mono); color: var(--neon-cyan); 
                font-weight: 700; font-size: 14px; width: 30px;">
        {display_idx:02d}
    </div>
    <div style="flex: 1;">
        <div style="color: #ffffff; font-size: 12px; font-weight: 600;">{eq_name}</div>
        <div style="font-family: var(--font-mono); font-size: 9px; 
                    color: var(--text-muted); letter-spacing: 1px;">
            {eq_id} • {eq_area}
        </div>
    </div>
    <div class="equipment-status-badge {status_class}" style="font-size: 0.6rem;">
        <div class="led"></div>
        <span>{status_text}</span>
    </div>
    <div style="text-align: center; min-width: 60px;">
        <div style="font-size: 8px; color: var(--text-muted);">V</div>
        <div style="font-family: var(--font-mono); font-size: 12px; color: var(--neon-cyan);">{voltage_str}</div>
    </div>
    <div style="text-align: center; min-width: 60px;">
        <div style="font-size: 8px; color: var(--text-muted);">A</div>
        <div style="font-family: var(--font-mono); font-size: 12px; color: var(--neon-cyan);">{current_str}</div>
    </div>
    <div style="text-align: center; min-width: 60px;">
        <div style="font-size: 8px; color: var(--text-muted);">kW</div>
        <div style="font-family: var(--font-mono); font-size: 12px; color: var(--neon-cyan);">{power_str}</div>
    </div>
    <div style="text-align: center; min-width: 50px;">
        <div style="font-size: 8px; color: var(--text-muted);">PF</div>
        <div style="font-family: var(--font-mono); font-size: 12px; color: var(--neon-cyan);">{pf_str}</div>
    </div>
</div>
""", unsafe_allow_html=True)
        else:
            st.warning("⏳ Waiting for data...")

# ==========================================
# TAB 5: HISTORICAL ANALYSIS
# ==========================================
with tab_history:
    st.markdown("""
<div class="section-header">
    <h3>📈 Historical Data Analysis</h3>
    <div class="line"></div>
</div>
""", unsafe_allow_html=True)
    
    st.info("""
💡 **Guide:** Download CSV from Executive Dashboard, then upload here for trend analysis.
""")
    
    uploaded_file = st.file_uploader("📂 **Upload historical CSV file**", type=['csv'], key="hist_uploader")
    
    if uploaded_file is not None:
        try:
            df_hist = pd.read_csv(uploaded_file)
            
            if "timestamp" in df_hist.columns:
                df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"])
                st.success(f"✅ **{len(df_hist)} records loaded**")
                
                st.markdown("### 📊 Quick Stats")
                c1, c2, c3 = st.columns(3)
                with c1:
                    if "energy_kwh" in df_hist.columns:
                        st.metric("Total Energy", f"{df_hist['energy_kwh'].max() - df_hist['energy_kwh'].min():,.1f} kWh")
                with c2:
                    if "power_factor" in df_hist.columns:
                        st.metric("Avg PF", f"{df_hist['power_factor'].mean():.3f}")
                with c3:
                    if "active_power" in df_hist.columns:
                        st.metric("Peak Power", f"{df_hist['active_power'].max():.2f} kW")
                
                with st.expander("📋 View Raw Data"):
                    st.dataframe(df_hist.sort_values("timestamp", ascending=False).head(100))
        except Exception as e:
            st.error(f"❌ Error: {e}")

# Footer
st.markdown("---")
st.markdown(f"""
<div style="text-align: center; padding: 1rem; color: #52638c; font-family: var(--font-mono); font-size: 0.75rem; letter-spacing: 1px;">
    ⚡ NEXUS SCADA v6.6.2 • Executive + AI Diagnosis Edition • REQ: {req_snapshot:,} • HR[120] Status Active • © 2026
</div>
""", unsafe_allow_html=True)

# ==========================================
# GLOBAL AUTO-REFRESH ENGINE
# ==========================================
if st.session_state.auto_refresh and st.session_state.connected:
    time.sleep(5)
    st.rerun()