@echo off
title KOSTAL Industrial HMI - Estacion de Tampografiado
cd /d "%~dp0"

if exist "kostalcode.py" (
    python "kostalcode.py"
) else if exist "gemini-code-1791499826404.py" (
    python "gemini-code-1791499826404.py"
) else (
    echo [ERROR] No se encontro el archivo principal kostalcode.py en la carpeta actual.
    pause
)

