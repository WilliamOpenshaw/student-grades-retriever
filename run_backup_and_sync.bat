@echo off
:: Daily job, 3:40 PM: back up grades.db to Google Drive, pull current grades from Classroom and update
:: the staff dashboard. Doesn't add a snapshot, send email or change grade reports.
:: Can be run by hand or from Windows Task Scheduler. Output goes to nightly_log.txt too.
cd /d "%~dp0"
chcp 65001 >nul
title Student Grades - Daily Dashboard Update
set PYTHONIOENCODING=utf-8
set GRADES_NO_BROWSER=1

echo ============================================================
echo   STUDENT GRADES RETRIEVER - DAILY DASHBOARD UPDATE
echo   Started %date% %time%
echo.
echo   Backs up the database, then reads current grades from Google
echo   Classroom for the staff dashboard. It sends no email to anyone.
echo   You can keep working or minimize this window. It closes by
echo   itself in a few minutes. Please don't close it.
echo ============================================================
echo.

echo ===== %date% %time% ===== >> "nightly_log.txt"
echo [1 of 2] Backing up the database...
".venv\Scripts\python.exe" "backup_database.py" >> "nightly_log.txt" 2>&1
echo [2 of 2] Reading current grades and updating the dashboard...
".venv\Scripts\python.exe" "refresh_dashboard.py" --log "nightly_log.txt"

echo.
echo Finished. Details are in nightly_log.txt. This window closes in 20 seconds.
timeout /t 20 >nul
