@echo off
setlocal EnableDelayedExpansion
title NEXUS SCADA Orchestrator and Production Launcher

REM ============================================================================
REM  NEXUS SCADA - Production-Grade Distributed Launcher  (v3 - latency-tuned)
REM
REM  Component Topology:
REM    [1] PLC Simulator  : Modbus TCP Server + Telemetry  (127.0.0.1:5020)
REM    [2] Backend API    : FastAPI + Uvicorn + LLM Worker (127.0.0.1:8000)
REM    [3] Analytics UI   : Streamlit Dashboard            (127.0.0.1:8501)
REM    [4] Operator HMI   : Tkinter Native Industrial GUI
REM
REM  v3 changes:
REM    - Explicit SCADA_MODEL_PATH env var (anchored to %ROOT% models dir)
REM    - NEXUS_AGENT_INTERVAL env passthrough documented
REM    - (all v2 hardening retained: 150 s health budget, PS fallback,
REM      python-only port cleanup, canonical cmd /k quote form)
REM
REM  v3.1 fixes:
REM    - FIX-1: MODELS_DIR defined BEFORE PYTHONPATH (was silently empty)
REM    - FIX-2: netstat port regex no longer overridden by /c:
REM    - FIX-3: retry counter uses !RETRY_COUNT! (delayed expansion)
REM    - FIX-4: llm_available JSON check split into two robust findstr calls
REM    - FIX-5: LISTENING filter runs before port filter (faster, precise)
REM ============================================================================

REM --- Path Normalization and Environment Setup ---
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "VENV_PY=%ROOT%venv\Scripts\python.exe"
set "MODEL_PATH=%ROOT%models\qwen2.5-coder-1.5b-instruct-q6_k.gguf"
set "MODELS_DIR=%ROOT%models"
set "LOG_DIR=%ROOT%logs"

:: v3: EXPLICIT model path for the AI engine. ai_engine.py reads
:: SCADA_MODEL_PATH; the file-anchored default inside the module resolves
:: to the same file, but this makes the path explicit and survives any
:: future relocation of ai_engine.py.
set "SCADA_MODEL_PATH=%MODEL_PATH%"

:: CRITICAL: Set root directory and models dir as PYTHONPATH for absolute
:: package resolution. FIX-1: MODELS_DIR must be defined above this line.
set "PYTHONPATH=%ROOT%;%MODELS_DIR%"

:: Auth disabled unless intentionally set
set "NEXUS_API_KEY="

:: Agent loop cadence (seconds). 1.0 = default. Raise to 2.0+ if CPU is
:: saturated during sweeps and telemetry reads start lagging.
if not defined NEXUS_AGENT_INTERVAL set "NEXUS_AGENT_INTERVAL=1.0"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if /i "%DEBUG%"=="1" (
    set "SCADA_AI_VERBOSE=1"
    echo [INFO] Debug mode enabled: Verbose LLM and telemetry logging active.
)

echo.
echo ============================================================================
echo   STAGE 1: Pre-Flight Integrity Checks and Environment Audit
echo ============================================================================

REM --- Check 1: Python Virtual Environment ---
if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment Python binary not found:
    echo         %VENV_PY%
    echo [ACTION] python -m venv venv ^&^& venv\Scripts\activate ^&^& pip install -r requirements.txt
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
    echo         Ensure pymodbus, uvicorn, fastapi, streamlit, numpy are installed.
    echo.
    pause
    exit /b 1
)
echo [OK] Core dependencies verified.

REM --- Check 3: Quantized Model Artifact ---
if exist "%MODEL_PATH%" (
    echo [OK] LLM weights located: %MODEL_PATH%
    echo [INFO] Model path exported as SCADA_MODEL_PATH for ai_engine.
) else (
    echo [WARN] GGUF model not found at:
    echo        %MODEL_PATH%
    echo        System starts, but the cognitive agent runs in fallback mode.
)

REM --- Check 4: Health-probe tool availability (curl, with PS fallback) ---
set "HEALTH_PROBE=curl"
curl --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] curl not found - health polling will use PowerShell fallback.
    set "HEALTH_PROBE=powershell"
)

REM --- Check 5: Stale Port Cleanup (5020, 8000, 8501) ---
REM  FIX-2 + FIX-5: filter LISTENING first, then use pure regex for the port.
REM  The old form 'findstr /r /c:":%%P[^0-9]"' was silently no-op because
REM  /c: overrides /r and treats the pattern as a literal string.
echo [INFO] Inspecting ports 5020, 8000, 8501 for stale sockets...
for %%P in (5020 8000 8501) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr /i "LISTENING" ^| findstr /r ":%%P[^0-9]"') do (
        tasklist /fi "PID eq %%a" 2>nul | findstr /i "python" >nul
        if not errorlevel 1 (
            echo [WARN] Port %%P held by PYTHON pid %%a - terminating stale process...
            taskkill /F /PID %%a >nul 2>&1
        ) else (
            echo [WARN] Port %%P held by NON-python pid %%a - NOT killing. Close it manually if needed.
        )
    )
)
echo [OK] Port audit complete.

echo.
echo ============================================================================
echo   STAGE 2: Sequential Component Deployment
echo ============================================================================

REM --- Step 1: PLC Simulator ---
echo [1/4] Starting PLC Modbus Server (TCP 127.0.0.1:5020)...
start "NEXUS-1-PLC" /D "%ROOT%" cmd /k ""%VENV_PY%" plc_simulator\modbus_server.py"

timeout /t 2 /nobreak >nul

REM --- Step 2: Backend API Engine ---
echo [2/4] Starting Backend API Server (Uvicorn on 127.0.0.1:8000)...
start "NEXUS-2-API" /D "%ROOT%" cmd /k ""%VENV_PY%" -m uvicorn backend.api:app --host 127.0.0.1 --port 8000 --log-level info"

echo.
echo ============================================================================
echo   STAGE 3: Health Verification (v3: 150 s budget - Qwen cold load is slow)
echo ============================================================================

set /a RETRY_COUNT=0
set /a MAX_RETRIES=75
set "HEALTH_JSON=%LOG_DIR%\health_last.json"

:POLL_BACKEND
timeout /t 2 /nobreak >nul
set /a RETRY_COUNT+=1

if "%HEALTH_PROBE%"=="curl" (
    curl -s -f http://127.0.0.1:8000/api/health > "%HEALTH_JSON%" 2>nul
) else (
    powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:8000/api/health').Content | Out-File -Encoding utf8 '%HEALTH_JSON%' } catch { exit 1 }" >nul 2>&1
)

if errorlevel 1 (
    REM  FIX-3: use !RETRY_COUNT! / !MAX_RETRIES! so the counter updates
    echo [WAIT] [!RETRY_COUNT!/!MAX_RETRIES!] Backend initializing - Qwen load can take 60-90 s...
    if !RETRY_COUNT! geq !MAX_RETRIES! (
        echo.
        echo [ERROR] Backend API did not respond within 150 seconds.
        echo         Check the 'NEXUS BACKEND API' console window for the traceback.
        echo         Also inspect the newest log in: %LOG_DIR%
        goto LAUNCH_UI_CHOICE
    )
    goto POLL_BACKEND
)

echo [OK] Backend API is live and healthy!
echo --- API Health Status ---
type "%HEALTH_JSON%"
echo.
echo -------------------------
REM  FIX-4: robust JSON field check - two chained findstr calls
findstr /c:"llm_available" "%HEALTH_JSON%" | findstr /c:"true" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Cognitive engine: Qwen loaded and available.
) else (
    echo [INFO] Cognitive engine still loading or in fallback - /api/health will
    echo        report llm_available=true once inference is ready.
)
goto LAUNCH_INTERFACES

:LAUNCH_UI_CHOICE
echo [WARN] Proceeding to launch user interfaces despite backend health timeout...

:LAUNCH_INTERFACES
echo.
echo ============================================================================
echo   STAGE 4: Launching Human-Machine Interfaces
echo ============================================================================

echo [3/4] Starting Streamlit Analytics Dashboard (127.0.0.1:8501)...
start "NEXUS-3-UI" /D "%ROOT%" cmd /k ""%VENV_PY%" -m streamlit run dashboard\streamlit_app.py --server.port 8501 --server.headless false"

echo [4/4] Starting Native Industrial Operator HMI (Tkinter)...
start "NEXUS-4-HMI" /D "%ROOT%" cmd /k ""%VENV_PY%" hmi\hmi_gui.py"

echo.
echo ============================================================================
echo   SYSTEM DEPLOYMENT SEQUENCE COMPLETE
echo ============================================================================
echo   [+] Modbus PLC TCP Server : 127.0.0.1:5020
echo   [+] Backend REST and AI   : http://127.0.0.1:8000
echo   [+] Interactive OpenAPI   : http://127.0.0.1:8000/docs
echo   [+] Streamlit UI          : http://127.0.0.1:8501
echo   [+] Health snapshot       : %HEALTH_JSON%
echo   [+] Model path (explicit) : %SCADA_MODEL_PATH%
echo.
echo   NOTE: Each subsystem runs in an isolated console. If a window shows a
echo         traceback, that process crashed - read it before restarting.
echo ============================================================================
echo.
pause