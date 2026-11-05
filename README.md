# K3_TampoWindows

Interfaz HMI industrial táctil para estación de tampografiado KOSTAL (Edición Windows 10/11 & Dual-Homing OPC UA).

## Archivo Principal
- **`kostalcode.py`**: Aplicación principal HMI optimizada para pantalla táctil `1920x1080`, conexión con PLC WAGO PFC200 (Puerto Ethernet 1 - OT) y Servidor OPC UA de Producción hacia red de planta/nube KOSTAL (Puerto Ethernet 2 - IT).
- **`Iniciar_HMI_KOSTAL.bat`**: Lanzador directo para Windows.
- **`Instalar_Dependencias_Windows.bat`**: Instalador de dependencias Python (soporta modo offline mediante carpeta local `wheels/` y modo online vía `requirements.txt`).

## Características
- **Control de Movimiento y Ciclo:** Inicio, Paro, Reset, Home, Set 0, Avanzar Pieza, modo Manual, Jog en cruz y 1 o 2 pasadas de tampo.
- **Matriz y Conteo por Jig:** Visualización dinámica de la cuadrícula de producción y conteo independiente de **Piezas por Jig** por receta.
- **Arquitectura OPC UA Dual-Homing:**
  - **Puerto Ethernet 1 (Cliente OPC UA OT):** Comunicación en tiempo real con PLC WAGO (`192.168.1.69:4840`).
  - **Puerto Ethernet 2 (Servidor OPC UA IT):** Publicación de `Piezas_Totales`, `Piezas_OK` y `Piezas_NOK` en `opc.tcp://<IP_PUERTO_NUBE>:4840/freeopcua/server/`.
- **Estadísticas y Reportes:** Registro de Fin de Hora y exportación a Excel (`reporte_produccion_kostal.xlsx`).
