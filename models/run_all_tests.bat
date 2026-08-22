@echo off
title NEXUS AI - Full Test Suite
color 0A

cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"

echo ============================================================
echo   🚀 NEXUS AI - Full Test & Validation Suite
echo ============================================================
echo.

:: ---- 1. Activate Virtual Environment ----
echo [ENV] Activating virtual environment...
call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo ❌ ERROR: Failed to activate venv. Check path.
    pause
    exit /b 1
)
echo ✅ Virtual environment activated.
echo.

:: ---- 2. Run test_model.py (Basic Loading) ----
echo ============================================================
echo   [1/3] Running Basic Model Test (test_model.py)
echo ============================================================
python models\test_model.py
if errorlevel 1 (
    echo ⚠️  Warning: test_model.py exited with an error.
) else (
    echo ✅ test_model.py passed.
)
echo.
echo Press any key to continue to Integration Test...
pause >nul
echo.

:: ---- 3. Run test_integration.py ----
echo ============================================================
echo   [2/3] Running Integration Test (test_integration.py)
echo ============================================================
python models\test_integration.py
if errorlevel 1 (
    echo ⚠️  Warning: test_integration.py exited with an error.
) else (
    echo ✅ test_integration.py passed.
)
echo.
echo Press any key to continue to Final Test...
pause >nul
echo.

:: ---- 4. Run test_final.py ----
echo ============================================================
echo   [3/3] Running Final Test (test_final.py)
echo ============================================================
python models\test_final.py
if errorlevel 1 (
    echo ⚠️  Warning: test_final.py exited with an error.
) else (
    echo ✅ test_final.py passed.
)
echo.

:: ---- 5. Summary ----
echo ============================================================
echo   ✅ All Python test scripts have been executed!
echo ============================================================
echo.

:: ---- 6. Ask to launch Streamlit Dashboard ----
echo Do you want to start the Streamlit Dashboard now?
echo It will open in a SEPARATE window so the tests stay visible.
set /p choice="Start Streamlit? (Y/N): "

if /i "%choice%"=="Y" (
    echo.
    echo [STREAMLIT] Launching dashboard in a new window...
    start "NEXUS Streamlit Dashboard" cmd /k "cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data" && call venv\Scripts\activate.bat && streamlit run dashboard\streamlit_app.py"
    echo ✅ Streamlit is starting in a new window.
    echo    It may take a few seconds to load the UI.
) else (
    echo Skipping Streamlit launch.
)

echo.
echo ============================================================
echo   🎉 All tasks completed!
echo ============================================================
echo.
echo   💡 Tips:
echo   - Check the output above for any test failures.
echo   - If Streamlit was launched, look for the new window.
echo   - To test AI in the UI, go to the "HMI CONTROL" tab.
echo.
pause