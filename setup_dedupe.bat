@echo off
REM ====================================================================
REM  One-time setup for the Song De-duplicator.
REM  Installs Python libs (mutagen, rapidfuzz) and downloads fpcalc.exe
REM  (Chromaprint) used for audio fingerprinting. Double-click to run.
REM ====================================================================
cd /d "%~dp0"

echo Installing Python libraries (mutagen, rapidfuzz) ...
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install --upgrade mutagen rapidfuzz
) else (
    python -m pip install --upgrade mutagen rapidfuzz
)

echo.
echo Downloading fpcalc.exe (Chromaprint) for audio fingerprinting ...
where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0get_fpcalc.py"
) else (
    python "%~dp0get_fpcalc.py"
)

echo.
echo Setup finished. You can now run "Run Dedupe.bat".
echo (Audio fingerprinting needs fpcalc.exe; names/exact work without it.)
pause
