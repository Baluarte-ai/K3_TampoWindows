@echo off
title Instalador de Dependencias HMI KOSTAL (Windows Offline / Online)
cd /d "%~dp0"

echo ============================================================================
echo   INSTALACION DE LIBRERIAS PYTHON - INTERFAZ HMI KOSTAL TAMPOGRAFIADO
echo ============================================================================
echo.

if exist "wheels" (
    echo [MODO OFFLINE] Instalando librerias desde la carpeta local .\wheels ...
    python -m pip install --no-index --find-links=".\wheels" -r requirements.txt
) else (
    echo [MODO ONLINE] Instalando librerias desde requirements.txt ...
    python -m pip install -r requirements.txt
)

echo.
echo ============================================================================
echo   PROCESO FINALIZADO
echo ============================================================================
pause

