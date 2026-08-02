@echo off
REM ====================================================================
REM  Launches the Song De-duplicator GUI. Double-click this file.
REM ====================================================================
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0dedupe_songs.py"
) else (
    python "%~dp0dedupe_songs.py"
)

if %errorlevel% neq 0 (
    echo.
    echo Something went wrong. If Python isn't installed, get it from
    echo https://www.python.org/downloads/ ^(check "Add to PATH"^),
    echo then run setup_dedupe.bat once.
    pause
)
