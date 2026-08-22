@echo off
title NEXUS SCADA - HMI Simulator
color 0C

cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"
call "venv\Scripts\activate.bat"

echo ========================================
echo   NEXUS SCADA HMI (Delta DOP-B Style)
echo ========================================

cd hmi
python hmi_gui.py

pause