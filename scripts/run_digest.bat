@echo off
cd /d "C:\Users\katzs\Desktop\baseball"
REM Console output (incl. tracebacks) goes to run_console.log. The structured
REM "sent" line is written by send_digest.py to digest.log -- keep them in
REM separate files so both processes never hold a handle on the same log.
echo [%date% %time%] Starting daily digest... >> logs\run_console.log
python send_digest.py >> logs\run_console.log 2>&1
echo [%date% %time%] Done. >> logs\run_console.log
