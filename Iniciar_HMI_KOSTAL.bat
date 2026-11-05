@echo off
title KOSTAL Industrial HMI - Estacion de Tampografiado (Windows)
cd /d "%~dp0"

if exist "kostalcode.py" (
    python "kostalcode.py"
) else (
    echo [ERROR] No se encontro el archivo principal kostalcode.py en la carpeta actual.
    pause
)
