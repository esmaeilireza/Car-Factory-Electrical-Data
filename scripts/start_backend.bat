@echo off
title NEXUS SCADA - Backend API
color 0A

cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"
call "venv\Scripts\activate.bat"

echo ========================================
echo   NEXUS SCADA Backend Server
echo   http://localhost:8000
echo   Docs: http://localhost:8000/docs
echo ========================================

cd backend
python api.py

pause