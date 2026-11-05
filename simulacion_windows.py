"""
Lanzador del Simulador HMI KOSTAL para Windows
----------------------------------------------
Permite probar la interfaz completa en cualquier PC Windows sin necesidad de
conectar físicamente el PLC WAGO ni usar la pantalla táctil de Linux.

Características de Simulación en Windows:
- Ventana redimensionable y cómoda con mouse (cursor 100% visible).
- Inicio instantáneo sin esperas ni timeouts de red al PLC WAGO.
- Controles de simulación integrados en la barra inferior:
    * '⚡ Disparar Pintado (Sim)': simula el disparo del sensor/tampo avanzando 1 pieza.
    * '▶ Auto-Sim': ejecuta automáticamente el ciclo pieza por pieza cada ~1.4s.
- Prueba completa de todas las funciones:
    * Cruceta JOG y ajuste de paso JOG grande.
    * Pestaña desplegable de Set 0 y Set Home.
    * Selector de recetas con pantalla completa blanca.
    * Alternador de patrón Vertical / Horizontal.
    * Catálogo de recetas del administrador (límite 99).
"""
import os
import sys

# 1. Forzar variables de entorno para simulación Windows
os.environ["HMI_SIMULACION"] = "1"

print("=" * 72)
print("       KOSTAL INDUSTRIAL HMI — MODO SIMULACIÓN PARA WINDOWS")
print("=" * 72)
print("✓ Modo Simulación: ACTIVADO")
print("✓ Ventana: Ajustada para mouse y teclado estándar de Windows")
print("✓ Conexión PLC: Simulada en memoria (MockNodes)")
print("✓ Botones de simulación disponibles en la barra inferior:")
print("    • [⚡ Disparar Pintado (Sim)]: Avanza la pieza simulada manualmente")
print("    • [▶ Auto-Sim]: Avanza automáticamente la secuencia completa")
print("=" * 72 + "\n")

# 2. Ejecutar el archivo principal de la interfaz
import runpy

base_dir = os.path.dirname(os.path.abspath(__file__))
script_principal = os.path.join(base_dir, "kostalcode.py")
if not os.path.exists(script_principal):
    script_principal = os.path.join(base_dir, "gemini-code-1791499826404.py")

if not os.path.exists(script_principal):
    print(f"Error: No se encontró el archivo principal (kostalcode.py) en: {base_dir}")
    input("Presione Enter para salir...")
    sys.exit(1)

try:
    runpy.run_path(script_principal, run_name="__main__")
except KeyboardInterrupt:
    print("\nSimulación finalizada por el usuario.")
except Exception as e:
    import traceback
    print("\n" + "=" * 72)
    print("ERROR DURANTE LA EJECUCIÓN DE LA SIMULACIÓN:")
    print("=" * 72)
    traceback.print_exc()
    print("=" * 72)
    print(f"Detalle del error: {e}")
    input("\nPresione Enter para cerrar...")
