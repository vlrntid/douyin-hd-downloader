@echo off
REM Douyin HD Downloader - double-click launcher (Windows)
cd /d "%~dp0"

REM Use the project's virtual-env Python (it has playwright, yt-dlp, customtkinter).
REM Fall back to system "python" only if the venv is missing.
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYEXE=%~dp0venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

"%PYEXE%" main.py
if errorlevel 1 (
    echo.
    echo [Error] The app exited with an error. See above for details.
    pause
)
