@echo off
rem Daily data-maturation cycle (scheduled for 09:00 New York time on weekdays).
rem Refreshes FRED, backfills settled Kalshi days, matures labels, journals today's forecasts.
cd /d "%~dp0"
echo ==== %DATE% %TIME% >> journal\run.log
"C:\Users\PC\AppData\Local\Programs\Python\Python311\python.exe" bsm_maturation.py run >> journal\run.log 2>&1
