@echo off
chcp 65001 > nul
title Respaldo de Recetas y Datos - HMI KOSTAL EST-KOSTAL-04

echo =========================================================================
echo       KOSTAL MEXICANA - SISTEMA HMI TAMPOGRAFIA EST-KOSTAL-04
echo           GENERADOR DE RESPALDO DE RECETAS, DATOS Y ASSETS
echo =========================================================================
echo.

set "FECHA=%DATE:~6,4%-%DATE:~3,2%-%DATE:~0,2%"
set "HORA=%TIME:~0,2%-%TIME:~3,2%"
set "HORA=%HORA: =0%"
set "CARPETA_DESTINO=RESPALDO_RECETAS_KOSTAL_%FECHA%_%HORA%"
set "ZIP_DESTINO=RESPALDO_RECETAS_KOSTAL_%FECHA%_%HORA%.zip"

echo [1/4] Creando carpeta de respaldo temporal: %CARPETA_DESTINO% ...
mkdir "%CARPETA_DESTINO%" > nul 2>&1
mkdir "%CARPETA_DESTINO%\Assets" > nul 2>&1
mkdir "%CARPETA_DESTINO%\BaseDatos_KOSTAL" > nul 2>&1
mkdir "%CARPETA_DESTINO%\Reportes_Produccion" > nul 2>&1

echo [2/4] Copiando archivo maestro de Recetas y Configuraciones...
if exist "config_hmi.json" (
    copy /Y "config_hmi.json" "%CARPETA_DESTINO%\" > nul
    echo   - config_hmi.json: RECETAS Y CALIBRACIONES OK
) else (
    echo   - [ALERTA] No se encontro config_hmi.json
)

echo [3/4] Copiando Base de Datos, Imagenes de Piezas y Reportes...
if exist "BaseDatos_KOSTAL" (
    xcopy /E /I /Y "BaseDatos_KOSTAL\*" "%CARPETA_DESTINO%\BaseDatos_KOSTAL\" > nul
    echo   - BaseDatos_KOSTAL: SQLite Produccion OK
)
if exist "Assets" (
    xcopy /E /I /Y "Assets\*" "%CARPETA_DESTINO%\Assets\" > nul
    echo   - Assets: Fotos e Iconos de Recetas OK
)
if exist "Reportes_Produccion" (
    xcopy /E /I /Y "Reportes_Produccion\*" "%CARPETA_DESTINO%\Reportes_Produccion\" > nul
    echo   - Reportes_Produccion: Excel Maestro y Diarios OK
)
if exist "MANUAL_DE_USUARIO_Y_TECNICO_KOSTAL.docx" (
    copy /Y "MANUAL_DE_USUARIO_Y_TECNICO_KOSTAL.docx" "%CARPETA_DESTINO%\" > nul
    echo   - Manual Oficial en Word OK
)

echo [4/4] Comprimiendo respaldo a archivo ZIP para traslado seguro...
powershell -Command "Compress-Archive -Path '%CARPETA_DESTINO%\*' -DestinationPath '%ZIP_DESTINO%' -Force" > nul 2>&1

if exist "%ZIP_DESTINO%" (
    echo.
    echo =========================================================================
    echo [EXITO] Respaldo generado correctamente:
    echo Archivo ZIP : %ZIP_DESTINO%
    echo Carpeta     : %CARPETA_DESTINO%
    echo.
    echo INSTRUCCION:
    echo Copie el archivo '%ZIP_DESTINO%' o la carpeta '%CARPETA_DESTINO%' a una
    echo memoria USB o disco externo antes de formatear o cambiar el sistema operativo.
    echo =========================================================================
) else (
    echo.
    echo [OK] Carpeta generada: %CARPETA_DESTINO%
    echo Copie esta carpeta a su memoria USB.
)

echo.
pause
