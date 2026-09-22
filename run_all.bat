@echo off
setlocal EnableDelayedExpansion
title NEXUS SCADA Orchestrator & Production Launcher

REM ============================================================================
REM  NEXUS SCADA - Production-Grade Distributed Launcher
REM
REM  Component Topology:
REM    [1] PLC Simulator  : Modbus TCP Server + Internal Telemetry (127.0.0.1:5020)
REM    [2] Backend API    : FastAPI + Uvicorn + LLM Worker (127.0.0.1:8000)
REM    [3] Analytics UI   : Streamlit Dashboard (127.0.0.1:8501)
REM    [4] Operator HMI   : Tkinter Native Industrial GUI
REM ============================================================================

REM --- Path Normalization & Environment Setup ---
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "VENV_PY=%ROOT%venv\Scripts\python.exe"
set "MODEL_PATH=%ROOT%models\qwen2.5-coder-1.5b-instruct-q6_k.gguf"

:: CRITICAL: Set root directory as PYTHONPATH to ensure absolute package resolution
set "PYTHONPATH=%ROOT%"

:: Explicitly clear API key for dev mode (auth disabled unless intentionally set)
set "NEXUS_API_KEY="

if /i "%DEBUG%"=="1" (
    set "SCADA_AI_VERBOSE=1"
    echo [INFO] Debug mode enabled: Verbose LLM & telemetry logging active.
)

echo.
echo ============================================================================
echo   STAGE 1: Pre-Flight Integrity Checks & Environment Audit
echo ============================================================================

REM --- Check 1: Python Virtual Environment ---
if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment Python binary not found:
    echo         %VENV_PY%
    echo [ACTION] Create a virtual environment and install dependencies:
    echo         python -m venv venv
    echo         venv\Scripts\activate
    echo         pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo [OK] Python runtime verified: %VENV_PY%

REM --- Check 2: Core Package Dependencies ---
echo [INFO] Validating critical Python runtime packages...
"%VENV_PY%" -c "import pymodbus, uvicorn, fastapi, streamlit, numpy" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Dependency verification failed! Missing essential libraries.
    echo         Please ensure pymodbus, uvicorn, fastapi, and streamlit are installed.
    echo.
    pause
    exit /b 1
)
echo [OK] Core dependencies verified (pymodbus, uvicorn, fastapi, streamlit).

REM --- Check 3: Quantized Model Artifact ---
if exist "%MODEL_PATH%" (
    echo [OK] LLM weights located: %MODEL_PATH%
) else (
    echo [WARN] GGUF model artifact not found at:
    echo        %MODEL_PATH%
    echo        System will start, but AI predictive agents will run in fallback mode.
)

REM --- Check 4: Stale Port Cleanup (5020, 8000, 8501) ---
echo [INFO] Inspecting network ports (5020, 8000, 8501) for stale sockets...
for %%P in (5020 8000 8501) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr /r /c:":%%P[^0-9]" ^| findstr /i "LISTENING"') do (
        echo [WARN] Port %%P is occupied by PID %%a. Terminating stale process...
        taskkill /F /PID %%a >nul 2>&1
    )
)
echo [OK] Network ports cleared and ready.

echo.
echo ============================================================================
echo   STAGE 2: Sequential Component Deployment
echo ============================================================================

REM --- Step 1: Launch PLC Simulator (includes telemetry generation internally) ---
echo [1/4] Starting PLC Modbus Server (TCP 127.0.0.1:5020)...
start "NEXUS-1-PLC" /D "%ROOT%" cmd /k "title NEXUS PLC SIMULATOR && "%VENV_PY%" plc_simulator\modbus_server.py"

REM Give Modbus server a 2-second lead time to bind socket :5020
timeout /t 2 /nobreak >nul

REM --- Step 2: Launch Backend API Engine (FastAPI via Uvicorn) ---
echo [2/4] Starting Backend API Server (Uvicorn on 127.0.0.1:8000)...
start "NEXUS-2-API" /D "%ROOT%" cmd /k "title NEXUS BACKEND API && "%VENV_PY%" -m uvicorn backend.api:app --host 127.0.0.1 --port 8000 --log-level info"

echo.
echo ============================================================================
echo   STAGE 3: Active Health Verification (Waiting for Backend & LLM)
echo ============================================================================

set /a RETRY_COUNT=0
set /a MAX_RETRIES=20

:POLL_BACKEND
timeout /t 2 /nobreak >nul
set /a RETRY_COUNT+=1

REM Poll the health endpoint silently
curl -s -f http://127.0.0.1:8000/api/health > "%TEMP%\nexus_health_check.json" 2>nul
if errorlevel 1 (
    echo [WAIT] [%RETRY_COUNT%/%MAX_RETRIES%] Waiting for Backend API service to initialize...
    if !RETRY_COUNT! geq !MAX_RETRIES! (
        echo.
        echo [ERROR] Backend API failed to respond within 40 seconds.
        echo         Check the 'NEXUS BACKEND API' console window for traceback logs.
        goto LAUNCH_UI_CHOICE
    )
    goto POLL_BACKEND
)

echo [OK] Backend API is live and healthy!
echo --- API Health Status ---
type "%TEMP%\nexus_health_check.json"
echo.
echo -------------------------
goto LAUNCH_INTERFACES

:LAUNCH_UI_CHOICE
echo [WARN] Proceeding to launch user interfaces despite backend health timeout...

:LAUNCH_INTERFACES
echo.
echo ============================================================================
echo   STAGE 4: Launching Human-Machine Interfaces (HMI & Dashboard)
echo ============================================================================

REM --- Step 3: Launch Streamlit Analytics Dashboard ---
echo [3/4] Starting Streamlit Analytics Dashboard (127.0.0.1:8501)...
start "NEXUS-3-UI" /D "%ROOT%" cmd /k "title NEXUS STREAMLIT DASHBOARD && "%VENV_PY%" -m streamlit run dashboard\streamlit_app.py --server.port 8501 --server.headless false"

REM --- Step 4: Launch Native Tkinter HMI ---
echo [4/4] Starting Native Industrial Operator HMI (Tkinter)...
start "NEXUS-4-HMI" /D "%ROOT%" cmd /k "title NEXUS INDUSTRIAL HMI && "%VENV_PY%" hmi\hmi_gui.py"

echo.
echo ============================================================================
echo   SYSTEM DEPLOYMENT SUCCESSFUL
echo ============================================================================
echo   [+] Modbus PLC TCP Server : 127.0.0.1:5020
echo   [+] Backend REST & AI     : http://127.0.0.1:8000
echo   [+] Interactive OpenAPI   : http://127.0.0.1:8000/docs
echo   [+] Streamlit UI          : http://127.0.0.1:8501
echo.
echo   NOTE: Each subsystem is running in an isolated console.
echo         In case of an unhandled runtime error, the corresponding window
echo         will remain open showing the full Python traceback.
echo ============================================================================
echo.
pause