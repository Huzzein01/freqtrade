@echo off
REM Start freqtrade in dry-run mode using GS_TEMA_BB strategy
REM Run this from the freqtrade root directory

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo Starting GS-FreqTrade (dry-run)...
echo Strategy : GS_TEMA_BB
echo Pairs     : ETH/USDT, SOL/USDT, LTC/USDT
echo Exchange  : Bybit (dry-run, no real money)
echo Timeframe : 1h
echo.

.venv\Scripts\python.exe -m freqtrade trade ^
    --config user_data/config.json ^
    --strategy GS_TEMA_BB ^
    --logfile user_data/logs/freqtrade.log

pause
