@echo off
rem ---------------------------------------------------------------------------
rem  Cube J1 -> Home Assistant (MQTT) setup USB builder - launcher
rem  Double-click this file. It elevates and runs setup-cube-j1.ps1.
rem  (ASCII only on purpose: avoids console codepage problems.)
rem ---------------------------------------------------------------------------
setlocal
title Cube J1 setup USB builder

set "PS1=%~dp0setup-cube-j1.ps1"
if not exist "%PS1%" (
    echo [ERROR] setup-cube-j1.ps1 not found next to this file.
    echo         Keep both files in the same folder.
    pause
    exit /b 1
)

net session >nul 2>&1
if not errorlevel 1 goto :run

echo Requesting administrator rights ^(needed for FAT32 format^)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File',\"%PS1%\""
exit /b 0

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
echo.
pause
