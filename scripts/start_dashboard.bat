@echo off
title NEXUS SCADA - Streamlit Dashboard
color 0E

REM Change directory to the project root (parent of the scripts folder)
cd /d "%~dp0.."

REM Activate the virtual environment
call "venv\Scripts\activate.bat"

echo ========================================
echo   NEXUS SCADA Dashboard
echo   http://localhost:8501
echo ========================================

REM Run Streamlit from the project root, pointing to the app file
venv\Scripts\python.exe -m streamlit run dashboard\streamlit_app.py --server.port 8501

pause