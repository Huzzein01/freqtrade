@echo off
REM Sync closed freqtrade dry-run trades to GS Supabase
REM Run this anytime, or add to Task Scheduler alongside gs_retrain_check.py

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

.venv\Scripts\python.exe user_data/scripts/gs_bridge.py %*
