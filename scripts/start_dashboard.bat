@echo off
title NEXUS SCADA - Streamlit Dashboard
color 0E

cd /d "D:\Machine learning\data analyze Projects\phase 3 car-factory-electrical-data"
call "venv\Scripts\activate.bat"

echo ========================================
echo   NEXUS SCADA Dashboard
echo   http://localhost:8501
echo ========================================

cd dashboard
streamlit run streamlit_app.py --server.port 8501

pause