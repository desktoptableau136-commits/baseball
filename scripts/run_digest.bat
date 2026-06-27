@echo off
cd /d "C:\Users\katzs\Desktop\baseball"
echo [%date% %time%] Starting daily digest... >> logs\digest.log
python send_digest.py >> logs\digest.log 2>&1
echo [%date% %time%] Done. >> logs\digest.log
