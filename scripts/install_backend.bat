@echo off
echo ========================================
echo   Installing Backend - Phase 3
echo ========================================

:: Go to project folder
cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"

:: Activate virtual environment
call "venv\Scripts\activate.bat"

:: Install fastapi and uvicorn
echo [INFO] Installing fastapi and uvicorn...
pip install fastapi uvicorn

echo.
echo ========================================
echo   Installation complete!
echo ========================================
pause