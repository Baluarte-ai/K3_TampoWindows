@echo off
chcp 65001 > nul
title Restauracion de Recetas y Datos - HMI KOSTAL EST-KOSTAL-04

echo =========================================================================
echo       KOSTAL MEXICANA - SISTEMA HMI TAMPOGRAFIA EST-KOSTAL-04
echo           RESTAURADOR DE RECETAS, DATOS Y ASSETS RESPALDADOS
echo =========================================================================
echo.

echo Este asistente restaurará sus recetas y datos respaldados en esta máquina.
echo Asegúrese de haber colocado la carpeta de respaldo o sus archivos junto a este script.
echo.

set /p ORIGEN="Arrastre aquí la carpeta de respaldo o ingrese la ruta (deje vacío si están en la misma carpeta): "

if "%ORIGEN%"=="" (
    set "RUTA_RESPALDO=."
) else (
    set "RUTA_RESPALDO=%ORIGEN:"=%"
)

echo.
echo [1/3] Restaurando config_hmi.json (Recetas y Calibraciones)...
if exist "%RUTA_RESPALDO%\config_hmi.json" (
    copy /Y "%RUTA_RESPALDO%\config_hmi.json" "config_hmi.json" > nul
    echo   - config_hmi.json restaurado exitosamente.
) else (
    echo   - [AVISO] No se encontró config_hmi.json en %RUTA_RESPALDO%
)

echo [2/3] Restaurando BaseDatos_KOSTAL...
if exist "%RUTA_RESPALDO%\BaseDatos_KOSTAL" (
    if not exist "BaseDatos_KOSTAL" mkdir "BaseDatos_KOSTAL"
    xcopy /E /I /Y "%RUTA_RESPALDO%\BaseDatos_KOSTAL\*" "BaseDatos_KOSTAL\" > nul
    echo   - Base de datos SQLite restaurada.
)

echo [3/3] Restaurando Assets (Fotos de Recetas) y Reportes...
if exist "%RUTA_RESPALDO%\Assets" (
    if not exist "Assets" mkdir "Assets"
    xcopy /E /I /Y "%RUTA_RESPALDO%\Assets\*" "Assets\" > nul
    echo   - Fotos y miniaturas de piezas restauradas.
)
if exist "%RUTA_RESPALDO%\Reportes_Produccion" (
    if not exist "Reportes_Produccion" mkdir "Reportes_Produccion"
    xcopy /E /I /Y "%RUTA_RESPALDO%\Reportes_Produccion\*" "Reportes_Produccion\" > nul
    echo   - Reportes de producción restaurados.
)

echo.
echo =========================================================================
echo [LISTO] Proceso de restauracion finalizado.
echo Puede ejecutar 'Iniciar_HMI_KOSTAL.bat' para abrir la interfaz con
echo todas sus recetas calibradas y datos intactos.
echo =========================================================================
echo.
pause
