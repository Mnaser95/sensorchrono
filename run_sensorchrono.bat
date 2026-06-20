@echo off
cd /d "%~dp0"
set SENSORCHRONO_LABRECORDER_DIR=C:\Program Files\SensorChrono\_internal\LabRecorder
.venv\Scripts\python -m sensorchrono
pause
