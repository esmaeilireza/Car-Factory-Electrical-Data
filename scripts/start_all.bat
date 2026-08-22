@echo off
title NEXUS SCADA - Full System
color 0F

cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"

echo ========================================
echo   NEXUS SCADA - Full System Launcher
echo ========================================
echo.

:: Check if .bat files exist
if not exist "scripts\start_modbus.bat" (
    echo ❌ Error: File scripts\start_modbus.bat not found!
    pause
    exit /b 1
)

:: 1. Start Modbus Server (Must be first - others connect to it)
echo [1/4] Starting Modbus Server...
start "Modbus Server" cmd /k "scripts\start_modbus.bat"
timeout /t 3 /nobreak >nul

:: 2. Start Backend (After Modbus)
echo [2/4] Starting FastAPI Backend...
start "Backend API" cmd /k "scripts\start_backend.bat"
timeout /t 3 /nobreak >nul

:: 3. Start HMI
echo [3/4] Starting HMI Simulator...
start "HMI Simulator" cmd /k "scripts\start_hmi.bat"
timeout /t 2 /nobreak >nul

:: 4. Start Dashboard (Last - highest resource usage)
echo [4/4] Starting Streamlit Dashboard...
start "Streamlit Dashboard" cmd /k "scripts\start_dashboard.bat"

echo.
echo ========================================
echo   ✅ All services have been started!
echo ========================================
echo.
echo   Modbus Server : localhost:5020 (PLC Simulator)
echo   Backend API   : http://localhost:8000
echo   API Docs      : http://localhost:8000/docs
echo   Dashboard     : http://localhost:8501
echo   HMI           : Tkinter window opened
echo.
echo   💡 Tip: To stop everything, close this window
echo      or press Ctrl+C in each window.
echo.
pause