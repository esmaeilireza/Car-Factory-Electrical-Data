@echo off
title NEXUS SCADA - Modbus Server
color 0B

cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"
call "venv\Scripts\activate.bat"

echo ========================================
echo   NEXUS SCADA Modbus Server (Port 5020)
echo ========================================

cd plc_simulator
python modbus_server.py

pause