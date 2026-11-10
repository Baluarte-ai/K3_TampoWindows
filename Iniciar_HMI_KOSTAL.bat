@echo off
title KOSTAL Industrial HMI - Estacion de Tampografiado (Windows)
cd /d "%~dp0"

if exist "kostalcode_pyside6.py" (
    python "kostalcode_pyside6.py"
) else if exist "kostalcode.py" (
    python "kostalcode.py"
) else (
    echo [ERROR] No se encontro el archivo principal kostalcode_pyside6.py en la carpeta actual.
    pause
)
