import os
import sys
import time
import json
import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageDraw, ImageOps, ImageEnhance
import customtkinter as ctk
import pandas as pd
from opcua import Client, ua

# Bandera para activar Modo Simulación (especialmente útil en Windows para pruebas sin PLC)
MODO_SIM_WINDOWS = (
    os.environ.get("HMI_SIMULACION") == "1"
    or "--sim" in sys.argv
    or "--simulacion" in sys.argv
    or (sys.platform == "win32" and os.environ.get("HMI_FORZAR_PLC") != "1")
)

# ==============================================================================
# PARCHE DE SOPORTE PARA PANTALLAS TÁCTILES (LINUX / TOUCHSCREEN INDUSTRIAL)
# ==============================================================================
# En Linux y pantallas táctiles, los toques rápidos disparan <ButtonPress-1> y
# <ButtonRelease-1> sin generar previamente un evento de movimiento/hover (<Enter>).
# CustomTkinter por defecto cancela los clics si `self._mouse_inside` es False.
# Además, un ligero deslizamiento del dedo durante el toque activa <Leave>.
# Este parche asegura que los toques táctiles siempre se registren correctamente.
try:
    # Estado de visibilidad del teclado táctil
    _estado_teclado_visible = False

    # Función para solicitar cierre del teclado cuando ya no se usa (ejecución asíncrona no bloqueante)
    def ocultar_teclado_nativo(e=None, forzar=False):
        global _estado_teclado_visible
        if not _estado_teclado_visible and not forzar:
            return
        _estado_teclado_visible = False
        if sys.platform.startswith("linux"):
            import threading
            def _async_ocultar():
                try:
                    import subprocess
                    subprocess.run([
                        "dbus-send", "--type=method_call",
                        "--dest=org.onboard.Onboard",
                        "/org/onboard/Onboard/Keyboard",
                        "org.onboard.Onboard.Keyboard.Hide"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                    subprocess.run([
                        "gdbus", "call", "--session",
                        "--dest", "org.gnome.Shell",
                        "--object-path", "/org/gnome/Shell",
                        "--method", "org.gnome.Shell.Eval",
                        "Main.keyboard.close();"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                    if forzar:
                        subprocess.run(["pkill", "-f", "onboard"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                except Exception:
                    pass
            threading.Thread(target=_async_ocultar, daemon=True).start()

    def _patched_btn_on_release(self, event=None):
        if self._state not in ("disabled", tk.DISABLED):
            ahora = time.time()
            ultimo = getattr(self, "_ultimo_toque_ts", 0)
            if ahora - ultimo < 0.20:
                return
            self._ultimo_toque_ts = ahora
            self._mouse_inside = True
            try:
                self._on_enter()
                self.after(100, self._on_leave)
            except Exception:
                pass
            # Al tocar cualquier botón de acción, ocultar el teclado automáticamente
            ocultar_teclado_nativo()
            if self._command is not None:
                self._command()

    _orig_btn_draw = ctk.CTkButton._draw
    def _patched_btn_draw(self, no_color_updates=False):
        _orig_btn_draw(self, no_color_updates)
        try:
            self.configure(cursor="none")
        except Exception:
            pass
        for w in [self._canvas, self._text_label, self._image_label]:
            if w is not None and not getattr(w, "_touch_press_bound", False):
                w._touch_press_bound = True
                try:
                    w.bind("<Button-1>", lambda e, s=self: s._on_release(e), add="+")
                except Exception:
                    pass

    ctk.CTkButton._on_release = _patched_btn_on_release
    ctk.CTkButton._draw = _patched_btn_draw

    # Función para solicitar apertura del teclado escalado y asegurando que flote al frente (asíncrona y debounced)
    _ultimo_invocar_teclado_ts = 0.0
    def invocar_teclado_nativo_ubuntu(e=None, forzar=False):
        global _estado_teclado_visible, _ultimo_invocar_teclado_ts
        ahora = time.time()
        if _estado_teclado_visible and not forzar and (ahora - _ultimo_invocar_teclado_ts < 0.8):
            return
        _ultimo_invocar_teclado_ts = ahora
        _estado_teclado_visible = True
        if sys.platform.startswith("linux"):
            import threading
            def _async_mostrar():
                try:
                    import subprocess
                    # Desplegar Onboard si está disponible
                    subprocess.run(["onboard"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                    subprocess.run([
                        "dbus-send", "--type=method_call",
                        "--dest=org.onboard.Onboard",
                        "/org/onboard/Onboard/Keyboard",
                        "org.onboard.Onboard.Keyboard.Show"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                    # GNOME Shell teclado nativo
                    subprocess.run([
                        "gdbus", "call", "--session",
                        "--dest", "org.gnome.Shell",
                        "--object-path", "/org/gnome/Shell",
                        "--method", "org.gnome.Shell.Eval",
                        "Main.keyboard.open(null, true);"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                    subprocess.run(["wmctrl", "-r", "Onboard", "-b", "add,above"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                    subprocess.run(["wmctrl", "-a", "Onboard"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
                except Exception:
                    pass
            threading.Thread(target=_async_mostrar, daemon=True).start()

    def alternar_teclado_tactil():
        """Alterna visibilidad del teclado táctil con un solo toque del operador"""
        global _estado_teclado_visible
        if _estado_teclado_visible:
            ocultar_teclado_nativo(forzar=True)
        else:
            invocar_teclado_nativo_ubuntu(forzar=True)

    # En CTkEntry, asegurar que al tocar reciba foco inmediato y despliegue teclado sin bloquear.
    # Soporta teclas Enter y KP_Enter para confirmación fluida.
    _orig_entry_init = ctk.CTkEntry.__init__
    def _patched_entry_init(self, *args, **kwargs):
        _orig_entry_init(self, *args, **kwargs)
        def _al_tocar_entry(e=None):
            if getattr(self, "_no_teclado_auto", False):
                return
            try:
                if hasattr(self, "_entry") and self._entry is not None:
                    self._entry.focus_set()
                invocar_teclado_nativo_ubuntu()
            except Exception:
                pass

        try:
            self.bind("<Button-1>", _al_tocar_entry, add="+")
        except Exception:
            pass
        if hasattr(self, "_canvas") and self._canvas is not None:
            try:
                self._canvas.bind("<Button-1>", _al_tocar_entry, add="+")
            except Exception:
                pass
        if hasattr(self, "_entry") and self._entry is not None:
            try:
                self._entry.bind("<FocusIn>", lambda e: _al_tocar_entry(e), add="+")
                self._entry.bind("<Button-1>", lambda e: _al_tocar_entry(e), add="+")
                self._entry.bind("<Return>", lambda e: ocultar_teclado_nativo(), add="+")
                self._entry.bind("<KP_Enter>", lambda e: ocultar_teclado_nativo(), add="+")
            except Exception:
                pass
    ctk.CTkEntry.__init__ = _patched_entry_init

    # -------------------------------------------------------------------------
    # ESCALADO DE FUENTES PARA PANTALLAS TÁCTILES INDUSTRIALES
    # Aumenta automáticamente el tamaño de textos para máxima legibilidad
    # en la pantalla táctil industrial (evitando fuentes microscópicas de 8-10pt)
    # -------------------------------------------------------------------------
    _orig_apply_font_scaling = ctk.windows.widgets.scaling.scaling_base_class.CTkScalingBaseClass._apply_font_scaling
    def _patched_apply_font_scaling(self, font):
        if isinstance(font, tuple) and len(font) >= 2 and isinstance(font[1], (int, float)):
            size = font[1]
            if size <= 8:
                new_size = 11
            elif size <= 10:
                new_size = int(round(size * 1.35))   # 9 -> 12, 10 -> 14
            elif size <= 12:
                new_size = int(round(size * 1.30))   # 11 -> 14, 12 -> 16
            elif size <= 14:
                new_size = int(round(size * 1.25))   # 13 -> 16, 14 -> 18
            elif size <= 18:
                new_size = int(round(size * 1.20))   # 15 -> 18, 17 -> 20, 18 -> 22
            else:
                new_size = int(round(size * 1.15))
            font = (font[0], new_size) + font[2:]
        return _orig_apply_font_scaling(self, font)
    ctk.windows.widgets.scaling.scaling_base_class.CTkScalingBaseClass._apply_font_scaling = _patched_apply_font_scaling
except Exception as _e_patch:
    print(f"Aviso: No se pudo aplicar parche táctil: {_e_patch}")

# ==============================================================================
# CONFIGURACIÓN VISUAL MODERNA HMI KOSTAL (CUSTOMTKINTER INDUSTRIAL)
# ==============================================================================
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# Paleta Oficial KOSTAL y Dashboard Industrial (Fiel a los Mockups de las Imágenes)
COLOR_KOSTAL_NAVY   = "#162746"   # Azul corporativo profundo KOSTAL (Sidebar, botones primarios)
COLOR_KOSTAL_DARK   = "#0F1E36"   # Azul muy oscuro para barra superior y acentos
COLOR_KOSTAL_LIGHT  = "#243B66"   # Azul intermedio
COLOR_KOSTAL_ACCENT = "#004F9F"   # Azul vibrante de acción y selección activa

BG_MAIN             = "#F3F5F9"   # Fondo general neutro industrial
BG_CARD             = "#FFFFFF"   # Tarjetas blancas limpias
BG_INPUT            = "#F8FAFC"   # Fondo de campos y tablas
BORDER_CARD         = "#E2E8F0"   # Borde sutil industrial
BORDER_LIGHT        = "#CBD5E1"   # Separadores suaves

TEXT_PRIMARY        = "#111827"   # Texto principal oscuro de alto contraste
TEXT_SECONDARY      = "#4B5563"   # Texto secundario legible
TEXT_MUTED          = "#6B7280"   # Texto tenue

COLOR_OK            = "#10B981"   # Verde Inicio / OK
COLOR_OK_HOVER      = "#059669"
COLOR_OK_SOFT       = "#D1FAE5"   # Verde pastel
COLOR_OK_TEXT       = "#065F46"

COLOR_NOK           = "#EF4444"   # Rojo Paro Emergencia / Alarma
COLOR_NOK_HOVER     = "#DC2626"
COLOR_NOK_SOFT      = "#FEE2E2"   # Rojo pastel
COLOR_NOK_TEXT      = "#991B1B"

COLOR_WARN          = "#F59E0B"   # Ámbar advertencia / Reset
COLOR_WARN_HOVER    = "#D97706"
COLOR_WARN_SOFT     = "#FEF3C7"   # Ámbar pastel
COLOR_WARN_TEXT     = "#92400E"

COLOR_PURPLE        = "#7C3AED"   # Púrpura Admin / Fin Hora
COLOR_PURPLE_HOVER  = "#6D28D9"
COLOR_PURPLE_SOFT   = "#F3E8FF"
COLOR_PURPLE_TEXT   = "#7E22CE"

# Fuentes escaladas industriales (Adaptativas para Linux Ubuntu y Windows)
FONT_MAIN           = "Ubuntu" if sys.platform.startswith("linux") else "Segoe UI"
FONT_MONO           = "Ubuntu Mono" if sys.platform.startswith("linux") else "Consolas"

# ==============================================================================
# LÍMITES FÍSICOS DE COORDENADAS (Eje X: 0.0 a 88.0 mm | Eje Y: 0.0 a 200.0 mm)
# ==============================================================================
COORD_MIN = 0.0
COORD_X_MAX = 88.0   # Limitación estricta en el eje X solicitada por el usuario (88.0 mm máx)
COORD_Y_MAX = 200.0
COORD_MAX = 200.0

def limitar_coord(valor, eje=None):
    """Limita cualquier coordenada: eje X hasta 88.0 mm máx, eje Y hasta 200.0 mm máx"""
    try:
        val_f = float(valor)
        if eje in ("X", "x"):
            return max(COORD_MIN, min(COORD_X_MAX, val_f))
        elif eje in ("Y", "y"):
            return max(COORD_MIN, min(COORD_Y_MAX, val_f))
        return max(COORD_MIN, min(COORD_MAX, val_f))
    except (ValueError, TypeError):
        return COORD_MIN

def limitar_coord_x(valor):
    """Limita estrictamente la coordenada X entre 0.0 y 88.0 mm"""
    return limitar_coord(valor, "X")

def limitar_coord_y(valor):
    """Limita estrictamente la coordenada Y entre 0.0 y 200.0 mm"""
    return limitar_coord(valor, "Y")

# ==============================================================================
# 1. PERSISTENCIA DE CONFIGURACIÓN Y SET 0 (MEMORIA PERMANENTE)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
CONFIG_FILE = os.path.join(BASE_DIR, "config_hmi.json")

DEFAULT_USUARIOS = [
    {"num": "KOSTAL-0842", "nombre": "Juan Pérez", "rol": "OPERADOR", "pass": "1234", "tutorial_completado": True},
    {"num": "KOSTAL-0001", "nombre": "Carlos Hernández", "rol": "ADMIN", "pass": "admin", "tutorial_completado": True},
    {"num": "KOSTAL-0913", "nombre": "María López", "rol": "OPERADOR", "pass": "1234", "tutorial_completado": True},
    {"num": "KOSTAL-1052", "nombre": "Pedro Picapiedra", "rol": "OPERADOR", "pass": "1234", "tutorial_completado": True}
]

DEFAULT_RECETAS = [
    {
        "nombre": "KSL-001 - Bisel Frontal",
        "pieza": "Bisel Clúster Cuadrante",
        "modelo": "KSL-001",
        "filas": 3,
        "cols": 5,
        "piezas": 15,
        "piezas_por_jig": 15,
        "delta_x": 25.0,
        "delta_y": 30.0,
        "velocidad": 100,
        "cero_guardado_x": 0.0,
        "cero_guardado_y": 0.0,
        "cero_calibrado": True,
        "corridas": 1,
        "imagen": os.path.join(BASE_DIR, "Assets", "840d61ad-a9f0-4cc2-8114-b74643920fd2.jpg")
    },
    {
        "nombre": "KSL-002 - Inserto Palanca",
        "pieza": "Inserto Selector Dirección",
        "modelo": "KSL-002",
        "filas": 2,
        "cols": 4,
        "piezas": 8,
        "piezas_por_jig": 8,
        "delta_x": 30.0,
        "delta_y": 35.0,
        "velocidad": 120,
        "cero_guardado_x": 0.0,
        "cero_guardado_y": 0.0,
        "cero_calibrado": True,
        "corridas": 1,
        "imagen": os.path.join(BASE_DIR, "Assets", "a002db93-f06c-4ee5-97a2-60549ad9a379.jpg")
    },
    {
        "nombre": "KSL-003 - Conector SMC",
        "pieza": "Carcasa Conector 16 Pin",
        "modelo": "KSL-003",
        "filas": 4,
        "cols": 4,
        "piezas": 16,
        "piezas_por_jig": 16,
        "delta_x": 20.0,
        "delta_y": 20.0,
        "velocidad": 110,
        "cero_guardado_x": 0.0,
        "cero_guardado_y": 0.0,
        "cero_calibrado": True,
        "corridas": 1,
        "imagen": os.path.join(BASE_DIR, "Assets", "cd87b1e8-6d7b-48fd-af44-a450125bdfd1.jpg")
    }
]

def obtener_piezas_por_jig_receta(r):
    """
    Obtiene de manera 100% retrocompatible el número de piezas por jig de una receta.
    Si el archivo config_hmi.json proviene de una copia de seguridad que no tiene 'piezas_por_jig',
    toma 'piezas' o 'filas * cols' sin generar errores ni conflictos tras un git pull.
    """
    if not isinstance(r, dict):
        return 1
    try:
        if "piezas_por_jig" in r and r["piezas_por_jig"] is not None and str(r["piezas_por_jig"]).strip() != "":
            return max(1, int(float(str(r["piezas_por_jig"]).strip())))
        if "piezas" in r and r["piezas"] is not None and str(r["piezas"]).strip() != "":
            return max(1, int(float(str(r["piezas"]).strip())))
        fil = max(1, int(r.get("filas", 3)))
        col = max(1, int(r.get("cols", 5)))
        return max(1, fil * col)
    except Exception:
        return 1

config_actual = {
    "cero_guardado_x": 0.0,
    "cero_guardado_y": 0.0,
    "velocidad": 100,
    "paso_jog": 5.0,
    "pasadas_tampo": 2,
    "patron_pintado": "vertical",
    "receta_activa": "KSL-001 - Bisel Frontal",
    "recetas": list(DEFAULT_RECETAS),
    "usuarios": list(DEFAULT_USUARIOS),
    "ajustes_globales": {
        "plc_url": "opc.tcp://192.168.1.69:4840",
        "tiempo_muestreo_ms": 100,
        "estacion_id": "EST-KOSTAL-04"
    }
}

def cargar_configuracion():
    global config_actual
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
                config_actual.update(datos)
                # Si las recetas guardadas son de formato antiguo o incompletas, asegurar campos
                recetas_cargadas = []
                for r in config_actual.get("recetas", []):
                    r_cero_x = limitar_coord_x(float(r.get("cero_guardado_x", 0.0)))
                    r_cero_y = limitar_coord_y(float(r.get("cero_guardado_y", 0.0)))
                    r_corridas = int(r.get("corridas", 0))
                    r_calibrado = bool(r.get("cero_calibrado", False) or r_corridas > 0 or ("cero_guardado_x" in r and "cero_guardado_y" in r))
                    
                    # Para recetas estándar base, si no tenían marca previa de calibración, dejarlas pre-ajustadas
                    if not r_calibrado and r.get("nombre") in ["KSL-001 - Bisel Frontal", "KSL-002 - Inserto Palanca", "KSL-003 - Conector SMC"]:
                        r_calibrado = True
                        r_corridas = max(1, r_corridas)
                        
                    pz_jig = obtener_piezas_por_jig_receta(r)
                    r_norm = {
                        "nombre": r.get("nombre", "Receta"),
                        "pieza": r.get("pieza", "Pieza SMC"),
                        "modelo": r.get("modelo", "KSL-000"),
                        "filas": max(1, int(r.get("filas", 3))),
                        "cols": max(1, int(r.get("cols", 5))),
                        "piezas": pz_jig,
                        "piezas_por_jig": pz_jig,
                        "delta_x": float(r.get("delta_x", r.get("avance_x", 25.0))),
                        "delta_y": float(r.get("delta_y", r.get("avance_y", 30.0))),
                        "velocidad": int(r.get("velocidad", 100)),
                        "imagen": r.get("imagen", ""),
                        "cero_guardado_x": r_cero_x,
                        "cero_guardado_y": r_cero_y,
                        "cero_calibrado": r_calibrado,
                        "corridas": r_corridas
                    }
                    recetas_cargadas.append(r_norm)
                if recetas_cargadas:
                    config_actual["recetas"] = recetas_cargadas
                else:
                    config_actual["recetas"] = list(DEFAULT_RECETAS)
                    
                if "usuarios" not in config_actual or not config_actual["usuarios"]:
                    config_actual["usuarios"] = list(DEFAULT_USUARIOS)
                else:
                    for u in config_actual["usuarios"]:
                        if "tutorial_completado" not in u:
                            u["tutorial_completado"] = True
                    
                print("Configuración persistente, recetas y usuarios cargados desde disco.")
        except Exception as e:
            print(f"Error al cargar configuración: {e}")

def guardar_configuracion():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_actual, f, indent=4, ensure_ascii=False)
        print("Configuración, recetas y usuarios guardados en disco exitosamente.")
    except Exception as e:
        print(f"Error al guardar configuración: {e}")

cargar_configuracion()

REGISTROS_PRODUCCION = [
    {"fecha": "26/08/2026", "hora_login": "07:31:24", "usuario": "Juan Pérez", "rol": "OPERADOR", "receta": "KSL-001 - P01", "total": 328, "ok": 321, "nok": 7, "calidad": "97.8%", "duracion": "08:30:54"},
    {"fecha": "26/08/2026", "hora_login": "07:28:10", "usuario": "María López", "rol": "OPERADOR", "receta": "KSL-001 - P01", "total": 342, "ok": 339, "nok": 3, "calidad": "99.1%", "duracion": "08:30:32"},
    {"fecha": "25/08/2026", "hora_login": "07:30:15", "usuario": "Juan Pérez", "rol": "OPERADOR", "receta": "KSL-001 - P01", "total": 310, "ok": 302, "nok": 8, "calidad": "97.4%", "duracion": "08:30:50"},
    {"fecha": "25/08/2026", "hora_login": "07:29:45", "usuario": "María López", "rol": "OPERADOR", "receta": "KSL-002 - P02", "total": 290, "ok": 285, "nok": 5, "calidad": "98.2%", "duracion": "08:29:27"},
    {"fecha": "24/08/2026", "hora_login": "07:32:00", "usuario": "Juan Pérez", "rol": "OPERADOR", "receta": "KSL-002 - P02", "total": 305, "ok": 298, "nok": 7, "calidad": "97.7%", "duracion": "08:28:20"}
]

# ==============================================================================
# 2. CONEXIÓN Y DEFINICIÓN DE NODOS OPC UA (PRESERVANDO 100% FUNCIONALIDAD ORIGINAL)
# ==============================================================================
url = config_actual["ajustes_globales"].get("plc_url", "opc.tcp://192.168.1.69:4840")
cliente = None
plc_conectado = False

class MockNode:
    """Nodo mock en memoria para permitir simulación cuando el PLC físico no esté en red"""
    def __init__(self, valor_inicial=0):
        self.valor = valor_inicial
    def get_value(self):
        return self.valor
    def set_value(self, valor):
        try:
            self.valor = valor.Value
        except Exception:
            self.valor = valor

ruta_prg = "ns=4;s=|var|WAGO 750-8212 PFC200 G2 2ETH RS.Application.PLC_PRG."
ruta_io = "ns=4;s=|var|WAGO 750-8212 PFC200 G2 2ETH RS.Application.IoConfig_Globals_Mapping."

nodos = {}

def vincular_nodos_plc():
    """Vincula los nodos al PLC real si está conectado o a MockNodes si está en simulación"""
    global nodos, plc_conectado
    if plc_conectado and cliente:
        try:
            nodos = {
                # Control y Modos
                "INICIO": cliente.get_node(ruta_prg + "INICIO"),
                "HOME": cliente.get_node(ruta_prg + "HOME"),
                "STOP": cliente.get_node(ruta_prg + "STOP"),
                "RESET": cliente.get_node(ruta_prg + "RESET"),
                "PINTAR": cliente.get_node(ruta_prg + "PINTAR"),
                "CRUZETA": cliente.get_node(ruta_prg + "CRUZETA"), 
                "MANUAL": cliente.get_node(ruta_prg + "MANUAL"),
                "PINTADO": cliente.get_node(ruta_prg + "PINTADO"),
                "NUEVA_POS": cliente.get_node(ruta_prg + "NUEVA_POS"),
                "NUEVA_POSICION": cliente.get_node(ruta_prg + "NUEVA_POSICION"),
                
                # Coordenadas y Memorias (Numéricos DINT)
                "POS_X": cliente.get_node(ruta_prg + "POS_X"),
                "POS_Y": cliente.get_node(ruta_prg + "POS_Y"),
                "HOME_X": cliente.get_node(ruta_prg + "HOME_X"),
                "HOME_Y": cliente.get_node(ruta_prg + "HOME_Y"),
                
                # Velocidades
                "VELOCIDAD_X": cliente.get_node(ruta_prg + "VEL_X"),
                "VELOCIDAD_Y": cliente.get_node(ruta_prg + "VEL_Y"),
                
                # Estados del PLC
                "LISTO": cliente.get_node(ruta_prg + "LISTO"),
                "NO_LISTO": cliente.get_node(ruta_prg + "NO_LISTO"),
                "M_ENE": cliente.get_node(ruta_prg + "M_ENE"), 
                "M_SET": cliente.get_node(ruta_prg + "M_SET"),
                "M_PINTAR": cliente.get_node(ruta_prg + "M_PINTAR"),
                
                # Hardware (IoConfig)
                "ALARM_X": cliente.get_node(ruta_io + "ALARM_X"),
                "ALARM_Y": cliente.get_node(ruta_io + "ALARM_Y"),
                "PE": cliente.get_node(ruta_io + "PE"),
                "TAMPO": cliente.get_node(ruta_io + "TAMPO"),
                "SENSOR_X": cliente.get_node(ruta_io + "SENSOR_X"),
                "SENSOR_Y": cliente.get_node(ruta_io + "SENSOR_Y"),
                "S_TAMPO": cliente.get_node(ruta_prg + "S_TAMPO")
            }
            try:
                nodos["S_TAMPO"] = cliente.get_node(ruta_prg + "S_TAMPO")
            except Exception:
                try:
                    nodos["S_TAMPO"] = cliente.get_node(ruta_prg + "S.tampo")
                except Exception:
                    try:
                        nodos["S_TAMPO"] = cliente.get_node(ruta_io + "S_TAMPO")
                    except Exception:
                        nodos["S_TAMPO"] = nodos.get("TAMPO", MockNode(False))
            try:
                nodos["M_PINTAR"] = cliente.get_node(ruta_prg + "M_PINTAR")
            except Exception:
                try:
                    nodos["M_PINTAR"] = cliente.get_node(ruta_io + "M_PINTAR")
                except Exception:
                    try:
                        nodos["M_PINTAR"] = cliente.get_node(ruta_prg + "M_pintar")
                    except Exception:
                        nodos["M_PINTAR"] = MockNode(False)
            return True
        except Exception as e:
            print(f"Error vinculando nodos reales: {e}")
            plc_conectado = False

    nodos = {
        "INICIO": MockNode(False),
        "HOME": MockNode(False),
        "STOP": MockNode(False),
        "RESET": MockNode(False),
        "PINTAR": MockNode(False),
        "M_PINTAR": MockNode(False),
        "CRUZETA": MockNode(False),
        "MANUAL": MockNode(False),
        "PINTADO": MockNode(False),
        "NUEVA_POS": MockNode(False),
        "NUEVA_POSICION": MockNode(False),
        "POS_X": MockNode(0),
        "POS_Y": MockNode(0),
        "HOME_X": MockNode(0),
        "HOME_Y": MockNode(0),
        "VELOCIDAD_X": MockNode(100),
        "VELOCIDAD_Y": MockNode(100),
        "LISTO": MockNode(True),
        "NO_LISTO": MockNode(False),
        "M_ENE": MockNode(True),
        "M_SET": MockNode(True),
        "ALARM_X": MockNode(False),
        "ALARM_Y": MockNode(False),
        "PE": MockNode(False),
        "TAMPO": MockNode(False),
        "SENSOR_X": MockNode(False),
        "SENSOR_Y": MockNode(False),
        "S_TAMPO": MockNode(False)
    }
    return False

def _probar_socket_plc(host="192.168.1.69", port=4840, timeout=0.8):
    try:
        import socket
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False

def intentar_conectar_plc(timeout_seg=2.0):
    """Intenta conectar al PLC WAGO y enlazar nodos de telemetría y control de forma rápida"""
    global cliente, plc_conectado
    if MODO_SIM_WINDOWS:
        plc_conectado = False
        vincular_nodos_plc()
        print("Modo Simulación Windows: omitiendo conexión física al PLC.")
        return False
        
    # Verificación no bloqueante de socket (evita bloqueo de 30-60s del kernel TCP si no hay Ethernet)
    if not _probar_socket_plc("192.168.1.69", 4840, timeout=0.8):
        print("Aviso: PLC WAGO no responde al ping de socket (192.168.1.69:4840). Omitiendo intento bloqueante.")
        plc_conectado = False
        vincular_nodos_plc()
        return False
        
    try:
        if cliente:
            try:
                cliente.disconnect()
            except Exception:
                pass
        cliente = Client(url, timeout=timeout_seg)
        cliente.connect()
        plc_conectado = True
        vincular_nodos_plc()
        print("Conexión exitosa con PLC WAGO (192.168.1.69:4840).")
        return True
    except Exception as e:
        print(f"Aviso de conexión con PLC: {e}")
        plc_conectado = False
        vincular_nodos_plc()
        return False

# Inicializar nodos en simulación por defecto hasta que la pantalla de carga pruebe la conexión
vincular_nodos_plc()

# Estados visuales e Internos
estado_pintar = False
estado_stop = False
paro_requiere_reset = False   # Requiere presionar RESET tras liberar el Paro
paro_requiere_inicio = False  # Requiere presionar INICIO tras el RESET post-paro
estado_manual = False
maquina_en_home = False
contador_ciclos_completados = 0
receta_actual_calibrada = config_actual.get("receta_activa", "")

# Coordenadas cargadas y limitadas entre 0.0 y 85.0 mm (X) y 0.0 y 200.0 mm (Y)
_rec_act_ini = next((r for r in config_actual.get("recetas", []) if r.get("nombre") == config_actual.get("receta_activa")), None)
if _rec_act_ini and (_rec_act_ini.get("cero_calibrado", False) or _rec_act_ini.get("corridas", 0) > 0 or ("cero_guardado_x" in _rec_act_ini and "cero_guardado_y" in _rec_act_ini)):
    cero_guardado_x = limitar_coord_x(_rec_act_ini.get("cero_guardado_x", config_actual.get("cero_guardado_x", 0.0)))
    cero_guardado_y = limitar_coord_y(_rec_act_ini.get("cero_guardado_y", config_actual.get("cero_guardado_y", 0.0)))
    cero_ha_sido_seteado = True
else:
    cero_guardado_x = limitar_coord_x(config_actual.get("cero_guardado_x", 0.0))
    cero_guardado_y = limitar_coord_y(config_actual.get("cero_guardado_y", 0.0))
    cero_ha_sido_seteado = False

ultimo_estado_pintado = False
ultimo_estado_sensor_y = False

# ==============================================================================
# GESTOR DE COORDENADAS INTERNAS (REEMPLAZO DE CAJAS VISUALES ABSOLUTAS)
# ==============================================================================
class StateCoord:
    """Contenedor en memoria de coordenadas del robot para mantener 100% compatibilidad"""
    def __init__(self, val_init=0.0, eje="X"):
        self.eje = eje
        self.val = limitar_coord(val_init, eje=self.eje)
    def get(self):
        return f"{self.val:.2f}"
    def delete(self, first=0, last=None):
        pass
    def insert(self, idx, s):
        try:
            self.val = limitar_coord(s, eje=self.eje)
            if 'lbl_jog_coord_x' in globals() and self is caja_pos_x:
                lbl_jog_coord_x.configure(text=f"{self.val:.2f}")
            elif 'lbl_jog_coord_y' in globals() and self is caja_pos_y:
                lbl_jog_coord_y.configure(text=f"{self.val:.2f}")
        except Exception:
            pass

caja_pos_x = StateCoord(0.0, eje="X")
caja_pos_y = StateCoord(0.0, eje="Y")

# ==============================================================================
# VARIABLES Y LÓGICA DE MATRIZ DINÁMICA (HASTA 5x5) Y ESPACIADO
# ==============================================================================
matriz_filas = 3
matriz_cols = 5
matriz_total = matriz_filas * matriz_cols
piezas_por_jig_actual = obtener_piezas_por_jig_receta(_rec_act_ini) if _rec_act_ini else 15
matriz_delta_x = 25.0
matriz_delta_y = 30.0
matriz_dist_diag = round((matriz_delta_x**2 + matriz_delta_y**2)**0.5, 2)
matriz_coordenadas = []
indice_matriz_actual = 0
pasada_actual_pieza = 1  # 1 o 2 pasadas según selección de tampos
ultimo_plc_rx_x = 0.0
ultimo_plc_rx_y = 0.0
ultimo_plc_rx_ok = False

def obtener_piezas_por_jig_actual():
    """Retorna el número de piezas por jig configurado en la receta activa actual (con fallback seguro)"""
    try:
        rec_nom = config_actual.get("receta_activa", "")
        rec_obj = next((r for r in config_actual.get("recetas", []) if r.get("nombre") == rec_nom), None)
        if rec_obj:
            return obtener_piezas_por_jig_receta(rec_obj)
    except Exception:
        pass
    return max(1, int(piezas_por_jig_actual))

def actualizar_coordenadas_matriz():
    """
    Calcula las coordenadas de cada pieza de la matriz para llevar cada espacio al Punto 0 (SET 0).
    REQUERIMIENTO:
    Donde se ubica el 0 es en el número 1 (donde se ubica la máquina de tampografiado).
    Con la misma orientación en el plano cartesiano, la idea es siempre llevar
    cada espacio de la matriz a la coordenada que pusimos en el punto 0.
    
    Dirección física de los ejes en el rango [0.0, 200.0] mm:
    - Si el punto 0 está en el extremo superior (ej. X0=200), el desplazamiento debe restar distancia (-c * delta_x).
    - Si el punto 0 está en el extremo inferior (ej. Y0=0), el desplazamiento debe sumar distancia (+r * delta_y)
      para no quedar truncado en 0 por los límites físicos de la máquina.
    """
    global matriz_coordenadas
    matriz_coordenadas = []
    x0 = cero_guardado_x
    y0 = cero_guardado_y

    dx = abs(matriz_delta_x)
    dy = abs(matriz_delta_y)

    # Determinar dirección viable para X dentro del rango [0.0, 88.0] mm
    recorrido_x = (matriz_cols - 1) * dx
    if (x0 - recorrido_x) < 0.0 and (x0 + recorrido_x) <= COORD_X_MAX:
        dir_x = 1.0
    elif (x0 + recorrido_x) > COORD_X_MAX and (x0 - recorrido_x) >= 0.0:
        dir_x = -1.0
    elif x0 < (COORD_X_MAX / 2.0):
        dir_x = 1.0
    else:
        dir_x = -1.0

    # Determinar dirección viable para Y dentro del rango [0.0, 200.0] mm
    recorrido_y = (matriz_filas - 1) * dy
    if (y0 - recorrido_y) < 0.0 and (y0 + recorrido_y) <= COORD_Y_MAX:
        dir_y = 1.0
    elif (y0 + recorrido_y) > COORD_Y_MAX and (y0 - recorrido_y) >= 0.0:
        dir_y = -1.0
    elif y0 < 100.0:
        dir_y = 1.0
    else:
        dir_y = -1.0

    patron = config_actual.get("patron_pintado", "vertical")
    if patron == "vertical":
        # Recorrido vertical en serpiente continua (siguiendo las flechas del usuario):
        # Columna 0 (par): baja de arriba hacia abajo (↓)
        # En la base se desplaza a la derecha (→)
        # Columna 1 (impar): sube de abajo hacia arriba (↑)
        for c in range(matriz_cols):
            filas_iter = range(matriz_filas) if (c % 2 == 0) else reversed(range(matriz_filas))
            for r in filas_iter:
                x = limitar_coord_x(x0 + dir_x * (c * dx))
                y = limitar_coord_y(y0 + dir_y * (r * dy))
                matriz_coordenadas.append((round(x, 2), round(y, 2)))
    else:
        # Recorrido horizontal: fila por fila (de izquierda a derecha)
        for r in range(matriz_filas):
            for c in range(matriz_cols):
                x = limitar_coord_x(x0 + dir_x * (c * dx))
                y = limitar_coord_y(y0 + dir_y * (r * dy))
                matriz_coordenadas.append((round(x, 2), round(y, 2)))

actualizar_coordenadas_matriz()

def buscar_recurso(nombre_archivo):
    if not nombre_archivo:
        return None
    if os.path.exists(nombre_archivo):
        return nombre_archivo
    base_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    archivo_base = os.path.basename(nombre_archivo)
    posibles_rutas = [
        os.path.join(base_dir, nombre_archivo),
        os.path.join(base_dir, "Assets", archivo_base),
        os.path.join(base_dir, archivo_base),
        os.path.join(r"C:\Users\eliza\Desktop\Interfaz_KOSTAL\Assets", archivo_base),
    ]
    for r in posibles_rutas:
        if os.path.exists(r):
            return r
    return None

def cargar_imagen(nombre_archivo, max_ancho, max_alto):
    ruta = buscar_recurso(nombre_archivo)
    if not ruta:
        return None
    try:
        img = Image.open(ruta)
        img.thumbnail((max_ancho, max_alto), Image.Resampling.LANCZOS)
        return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
    except Exception as e:
        print(f"No se pudo cargar {nombre_archivo}: {e}")
        return None

_img_pieza_cache = None
_fixture_circle_img_cache = {}

def obtener_imagen_circular_pieza(ruta_img, diametro, estado="pendiente"):
    """
    Genera y almacena en caché una imagen circular de alta definición de la pieza
    con máscara antialiasing y filtro según su estado (pendiente, activa, OK, NOK).
    """
    global _fixture_circle_img_cache
    if diametro <= 4:
        return None
    cache_key = (ruta_img, diametro, estado)
    if cache_key in _fixture_circle_img_cache:
        return _fixture_circle_img_cache[cache_key]
        
    real_path = buscar_recurso(ruta_img) if ruta_img else None
    if not real_path or not os.path.exists(real_path):
        return None
        
    try:
        img_orig = Image.open(real_path).convert("RGBA")
        d_render = max(diametro * 2, 72)
        img_sq = ImageOps.fit(img_orig, (d_render, d_render), Image.Resampling.LANCZOS)
        
        # Filtro de color sutil según el estado de la pieza
        if estado == "ok":
            tint = Image.new("RGBA", (d_render, d_render), (16, 185, 129, 45))
            img_sq = Image.alpha_composite(img_sq, tint)
        elif estado == "nok":
            tint = Image.new("RGBA", (d_render, d_render), (239, 68, 68, 55))
            img_sq = Image.alpha_composite(img_sq, tint)
        elif estado == "activa":
            tint = Image.new("RGBA", (d_render, d_render), (56, 189, 248, 45))
            img_sq = Image.alpha_composite(img_sq, tint)
        else: # pendiente
            enh = ImageEnhance.Brightness(img_sq)
            img_sq = enh.enhance(0.95)
            
        # Máscara circular con antialiasing
        mask = Image.new('L', (d_render, d_render), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((1, 1, d_render - 2, d_render - 2), fill=255)
        
        circular = Image.new('RGBA', (d_render, d_render), (0, 0, 0, 0))
        circular.paste(img_sq, (0, 0), mask=mask)
        
        final_img = circular.resize((diametro, diametro), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(final_img)
        _fixture_circle_img_cache[cache_key] = photo
        return photo
    except Exception as e:
        print(f"Error generando imagen circular: {e}")
        return None

def actualizar_imagen_pieza_proceso(ruta_img):
    global _img_pieza_cache, _fixture_circle_img_cache
    _fixture_circle_img_cache.clear()
    if 'lbl_proc_img_pieza' not in globals():
        return
    if ruta_img:
        real_path = buscar_recurso(ruta_img)
        if real_path and os.path.exists(real_path):
            try:
                img_orig = Image.open(real_path).convert("RGBA")
                img_fit = ImageOps.fit(img_orig, (95, 75), Image.Resampling.LANCZOS)
                _img_pieza_cache = ctk.CTkImage(light_image=img_fit, dark_image=img_fit, size=(95, 75))
                lbl_proc_img_pieza.configure(image=_img_pieza_cache, text="")
                return
            except Exception as e:
                print(f"Error cargando thumbnail de pieza: {e}")
    lbl_proc_img_pieza.configure(image=None, text="[Sin imagen]")

def actualizar_ui_matriz():
    """Actualiza los títulos y badges con la dimensión actual y distancias entre puntos"""
    pz_jig = obtener_piezas_por_jig_actual()
    dim_txt = f"{matriz_filas} × {matriz_cols} ({pz_jig} Piezas/Jig)"
    dist_txt = f"X:{matriz_delta_x:.1f} mm | Y:{matriz_delta_y:.1f} mm"
    if 'lbl_proc_matriz_dim' in globals():
        lbl_proc_matriz_dim.configure(text=dim_txt)
    if 'lbl_proc_matriz_dist' in globals():
        lbl_proc_matriz_dist.configure(text=dist_txt)
    if 'lbl_matriz_dim_badge' in globals():
        lbl_matriz_dim_badge.configure(text=f"📐 MATRIZ: {matriz_filas} × {matriz_cols} ({pz_jig} PZ/JIG)")
    if 'lbl_matriz_dist_badge' in globals():
        lbl_matriz_dist_badge.configure(text=f"↔ X: {matriz_delta_x:.1f} mm  |  ↕ Y: {matriz_delta_y:.1f} mm  |  ↗ Diag: {matriz_dist_diag:.1f} mm")
    if 'card_fixture' in globals() and hasattr(card_fixture, '_header_title'):
        card_fixture._header_title.configure(text=f"ESTADO DEL FIXTURE (MATRIZ {matriz_filas} × {matriz_cols})")

def mostrar_alerta_cambio_jig(nombre_receta):
    """
    Aviso modal al cambiar de Jig / Receta:
    Informa que SOLO se requiere el setteo del cero ('Set 0') y el regreso a home ('Ir a home').
    No requiere repetir el tutorial completo.
    """
    try:
        top_jig = ctk.CTkToplevel(ventana)
        top_jig.title("Aviso: Cambio de Jig")
        top_jig.geometry("490x310")
        top_jig.resizable(False, False)
        top_jig.configure(fg_color=BG_MAIN)
        try:
            top_jig.transient(ventana)
        except Exception:
            pass
        
        try:
            top_jig.update_idletasks()
            vx = ventana.winfo_x() + (ventana.winfo_width() // 2) - 245
            vy = ventana.winfo_y() + (ventana.winfo_height() // 2) - 155
            top_jig.geometry(f"+{max(0, vx)}+{max(0, vy)}")
        except Exception:
            pass

        def _grab_top_jig():
            try:
                if top_jig.winfo_exists():
                    top_jig.lift()
                    top_jig.grab_set()
            except Exception:
                pass
        try:
            top_jig.after(80, _grab_top_jig)
        except Exception:
            pass
            
        f_h = ctk.CTkFrame(top_jig, fg_color=COLOR_KOSTAL_NAVY, height=44, corner_radius=0)
        f_h.pack(fill="x")
        ctk.CTkLabel(f_h, text="🔧 CAMBIO DE JIG / RECETA", font=(FONT_MAIN, 11, "bold"), text_color="white").pack(side="left", padx=14, pady=10)
        
        f_body = ctk.CTkFrame(top_jig, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
        f_body.pack(fill="both", expand=True, padx=14, pady=10)
        
        ctk.CTkLabel(
            f_body,
            text=f"Jig de trabajo cambiado a:\n'{nombre_receta}'",
            font=(FONT_MAIN, 12, "bold"),
            text_color=COLOR_KOSTAL_DARK,
            justify="center"
        ).pack(pady=(12, 6))
        
        f_inst = ctk.CTkFrame(f_body, fg_color="#FEF3C7", corner_radius=6, border_width=1, border_color="#FDE68A")
        f_inst.pack(fill="x", padx=14, pady=4)
        
        ctk.CTkLabel(
            f_inst,
            text="Para este nuevo Jig ÚNICAMENTE debe realizar dos pasos:\n\n"
                 "1. Mover el cabezal a la Pieza 01 y presionar  '↩ Set 0'\n"
                 "2. Presionar el botón  '🏠 Ir a home'  para sincronizar\n\n"
                 "(No se requiere volver a realizar el tutorial completo)",
            font=(FONT_MAIN, 9, "bold"),
            text_color="#92400E",
            justify="left",
            padx=10,
            pady=8
        ).pack(fill="x")
        
        def proceder_cambio_jig():
            try:
                top_jig.grab_release()
            except Exception:
                pass
            try:
                top_jig.destroy()
            except Exception:
                pass
            resaltar_temporalmente_set0_y_home(6)

        ctk.CTkButton(
            f_body,
            text="✓ Entendido, proceder con Set 0 y Home",
            font=(FONT_MAIN, 10, "bold"),
            fg_color=COLOR_KOSTAL_NAVY,
            hover_color=COLOR_KOSTAL_ACCENT,
            text_color="white",
            height=32,
            corner_radius=6,
            command=proceder_cambio_jig
        ).pack(pady=(10, 8))
    except Exception as e:
        print(f"Error abriendo modal de cambio de jig: {e}")

# ==============================================================================
# GUÍA Y TUTORIAL INTERACTIVO PASO A PASO EN LA INTERFAZ (HUD + RESALTADO)
# ==============================================================================
btn_ir_home = None
btn_set_home = None
btn_set_cero = None

_hud_tutorial_frame = None
_tutorial_widgets_resaltados = []
_tutorial_anim_id = None
_tutorial_anim_fase = 0
_hud_pos_y = 0.88
_paso_tutorial_actual = 1
_tutorial_activo = False

_fn_mostrar_paso = None
_fn_navegar_paso = None
_fn_concluir_tutorial = None

def _restaurar_widgets_tutorial():
    """Restaura los estilos originales de todos los widgets iluminados durante el tutorial."""
    global _tutorial_widgets_resaltados, _tutorial_anim_id
    if _tutorial_anim_id:
        try:
            ventana.after_cancel(_tutorial_anim_id)
        except Exception:
            pass
        _tutorial_anim_id = None
    
    for item in _tutorial_widgets_resaltados:
        try:
            w = item["widget"]
            cfgs = item["orig"]
            w.configure(**cfgs)
        except Exception:
            pass
    _tutorial_widgets_resaltados = []

def _animar_pulso_tutorial():
    """Genera un efecto pulsante (brillo ámbar/azul vibrante) en los elementos activos del paso."""
    global _tutorial_anim_id, _tutorial_anim_fase
    if not _tutorial_widgets_resaltados:
        return
    _tutorial_anim_fase = 1 - _tutorial_anim_fase
    color_pulso = "#F59E0B" if _tutorial_anim_fase == 0 else "#0284C7"
    bw = 4 if _tutorial_anim_fase == 0 else 5
    for item in _tutorial_widgets_resaltados:
        try:
            w = item["widget"]
            if isinstance(w, ctk.CTkButton):
                w.configure(border_color=color_pulso, border_width=bw)
            elif isinstance(w, ctk.CTkFrame):
                w.configure(border_color=color_pulso, border_width=bw)
            elif isinstance(w, ctk.CTkOptionMenu):
                w.configure(button_color=color_pulso)
        except Exception:
            pass
    _tutorial_anim_id = ventana.after(380, _animar_pulso_tutorial)

def _resaltar_elementos_interfaz(widgets):
    """Guarda estilos previos y activa la iluminación con pulso en la lista de widgets."""
    global _tutorial_widgets_resaltados
    _restaurar_widgets_tutorial()
    for w in widgets:
        if w is None:
            continue
        try:
            orig = {}
            if isinstance(w, (ctk.CTkButton, ctk.CTkFrame)):
                for k in ["border_color", "border_width"]:
                    try:
                        orig[k] = w.cget(k)
                    except Exception:
                        pass
                _tutorial_widgets_resaltados.append({"widget": w, "orig": orig})
                w.configure(border_color="#F59E0B", border_width=4)
            elif isinstance(w, ctk.CTkOptionMenu):
                try:
                    orig["button_color"] = w.cget("button_color")
                except Exception:
                    pass
                _tutorial_widgets_resaltados.append({"widget": w, "orig": orig})
                w.configure(button_color="#F59E0B")
        except Exception:
            pass
    _animar_pulso_tutorial()

def notificar_accion_tutorial(paso_ejecutado, delay_ms=750):
    """
    Si el tutorial interactivo está activo y en el paso esperado,
    muestra una confirmación visual y avanza automáticamente al siguiente paso.
    """
    global _tutorial_activo, _paso_tutorial_actual, _fn_navegar_paso, _fn_concluir_tutorial
    if not _tutorial_activo or _hud_tutorial_frame is None:
        return
    try:
        if not _hud_tutorial_frame.winfo_exists():
            return
    except Exception:
        return

    if _paso_tutorial_actual == paso_ejecutado:
        lbl_estado.configure(
            text=f"✓ Acción del Paso {paso_ejecutado} detectada correctamente. Avanzando automáticamente...",
            text_color=COLOR_OK
        )
        if 'lbl_hud_feedback' in globals() and lbl_hud_feedback.winfo_exists():
            lbl_hud_feedback.configure(
                text="✓ ¡Acción Realizada! Avanzando...",
                fg_color="#10B981",
                text_color="#FFFFFF"
            )

        def _avanzar_automatico():
            if not _tutorial_activo or _paso_tutorial_actual != paso_ejecutado:
                return
            if 'lbl_hud_feedback' in globals() and lbl_hud_feedback.winfo_exists():
                lbl_hud_feedback.configure(
                    text="⚡ Avance Automático Activo",
                    fg_color="#065F46",
                    text_color="#A7F3D0"
                )
            if _paso_tutorial_actual < 5:
                if _fn_navegar_paso:
                    _fn_navegar_paso(1)
            else:
                if _fn_concluir_tutorial:
                    _fn_concluir_tutorial()

        ventana.after(delay_ms, _avanzar_automatico)

def resaltar_temporalmente_set0_y_home(segundos=6):
    """Ilumina de forma temporal 'Set 0' y 'Ir a home' tras un cambio de Jig."""
    if 'abrir_desplegable_calibracion' in globals():
        try:
            abrir_desplegable_calibracion()
        except Exception:
            pass
    widgets = [globals().get("btn_set_cero"), globals().get("btn_ir_home")]
    _resaltar_elementos_interfaz([w for w in widgets if w is not None])
    lbl_estado.configure(
        text="🔧 Cambio de Jig: Por favor fije 'Set 0' y presione 'Ir a home' (botones resaltados en pantalla).",
        text_color=COLOR_KOSTAL_ACCENT
    )
    ventana.after(segundos * 1000, _restaurar_widgets_tutorial)

def obtener_info_paso_tutorial(num_paso):
    """Retorna la información y widgets para cada uno de los pasos del tutorial."""
    global estado_manual, idioma_actual
    es = (idioma_actual == "ES")
    if num_paso == 1:
        return {
            "num": 1,
            "badge": "PASO 1 DE 5: SELECCIÓN DE RECETA Y JIG" if es else "STEP 1 OF 5: RECIPE & JIG SELECTION",
            "titulo": "Seleccionar el Jig y Cargar la Receta" if es else "Select Jig and Load Recipe",
            "icono": "📋",
            "descripcion": "El selector de recetas y la vista previa están ILUMINADOS a la izquierda.\n👉 Elige una receta en el menú para cargar su matriz. Al seleccionarla, avanzarás automáticamente al Paso 2." if es else "The recipe dropdown and preview are HIGHLIGHTED on the left.\n👉 Select a recipe from the menu to load its matrix.",
            "nota": "💡 Tip: En producción normal, al cambiar de Jig solo se requerirá 'Set 0' e 'Ir a home'." if es else "💡 Tip: In normal production, changing Jig only requires 'Set 0' and 'Home'.",
            "widgets": [globals().get("option_receta"), globals().get("f_pieza_preview")]
        }
    elif num_paso == 2:
        if not estado_manual:
            return {
                "num": 2,
                "badge": "PASO 2 DE 5: ACTIVAR MODO MANUAL Y FIJAR SET 0" if es else "STEP 2 OF 5: ENABLE MANUAL MODE & SET 0",
                "titulo": "Paso Previo: Cambiar la Estación a MODO MANUAL" if es else "Pre-requisite: Switch Station to MANUAL MODE",
                "icono": "⚙",
                "descripcion": "⚠️ La estación está en MODO AUTO. Antes de mover la cruceta o fijar el cero, debes cambiar a MODO MANUAL.\n👉 Presiona el botón ILUMINADO '⚙ MODO: AUTO' para activar el control manual del cabezal." if es else "⚠️ Station is in AUTO MODE. Before moving Jog or setting zero, switch to MANUAL.\n👉 Tap HIGHLIGHTED '⚙ MODE: AUTO' to switch to manual control.",
                "nota": "💡 Recuerda: La cruceta JOG y el desplazamiento de ejes solo responden cuando el modo MANUAL está activo." if es else "💡 Reminder: JOG pad and axis motions only respond in MANUAL mode.",
                "widgets": [globals().get("btn_manual")]
            }
        else:
            if 'abrir_desplegable_calibracion' in globals():
                try:
                    abrir_desplegable_calibracion()
                except Exception:
                    pass
            return {
                "num": 2,
                "badge": "PASO 2 DE 5: CALIBRACIÓN DEL PUNTO CERO" if es else "STEP 2 OF 5: ZERO POINT CALIBRATION",
                "titulo": "Centrar Cabezal y Guardar Punto Cero (Set 0)" if es else "Center Head & Save Zero Point (Set 0)",
                "icono": "↩",
                "descripcion": "✓ ¡MODO MANUAL ACTIVO! Usa las flechas de la cruceta JOG para centrar el cabezal sobre la Pieza 01.\n👉 Presiona el botón ILUMINADO '↩ Set 0' para registrar el origen de pintado. El sistema avanzará automáticamente." if es else "✓ MANUAL MODE ACTIVE! Use JOG arrows to center head over Part 01.\n👉 Tap HIGHLIGHTED '↩ Set 0' to register print origin.",
                "nota": "💡 Tip: Este punto cero se mantendrá guardado entre ciclos para toda esta receta." if es else "💡 Tip: This zero point remains saved across cycles for this recipe.",
                "widgets": [globals().get("btn_set_cero"), globals().get("btn_y_mas"), globals().get("btn_y_menos"), globals().get("btn_x_mas"), globals().get("btn_x_menos")]
            }
    elif num_paso == 3:
        return {
            "num": 3,
            "badge": "PASO 3 DE 5: REFERENCIA DE SEGURIDAD" if es else "STEP 3 OF 5: SAFETY REFERENCE",
            "titulo": "Referenciar la Máquina (Ir a Home)" if es else "Reference Machine (Go to Home)",
            "icono": "🏠",
            "descripcion": "El botón '🏠 Home' está ILUMINADO en los controles de referencia a la derecha.\n👉 Presiona este botón para enviar el cabezal al origen seguro de reposo. El sistema avanzará automáticamente al Paso 4." if es else "The '🏠 Home' button is HIGHLIGHTED in reference controls.\n👉 Tap this button to send the head to safe rest position.",
            "nota": "💡 Tip: Ambos orígenes (Set 0 y Home) protegen el herramental contra colisiones antes de pintar." if es else "💡 Tip: Both origins (Set 0 & Home) protect tooling against collisions.",
            "widgets": [globals().get("btn_ir_home")]
        }
    elif num_paso == 4:
        return {
            "num": 4,
            "badge": "PASO 4 DE 5: CICLO AUTOMÁTICO Y GATILLO" if es else "STEP 4 OF 5: AUTOMATIC CYCLE & TRIGGER",
            "titulo": "Iniciar Pintado y Disparo por Sensor Y" if es else "Start Printing & Sensor Y Trigger",
            "icono": "🎨",
            "descripcion": "El botón '🎨 INICIO DE PINTADO' está ILUMINADO a la izquierda.\n👉 Presiona el botón para armar el ciclo automático de pintado." if es else "The '🎨 START PRINTING' button is HIGHLIGHTED on the left.\n👉 Tap the button to arm the automatic printing cycle.",
            "nota": "💡 Tip: En producción el tampógrafo avanzará las piezas al recibir el pulso del sensor." if es else "💡 Tip: In production the pad printer advances parts upon sensor pulse.",
            "widgets": [globals().get("btn_pintar")]
        }
    elif num_paso == 5:
        return {
            "num": 5,
            "badge": "PASO 5 DE 5: FIN DE CICLO, RESET Y PARO" if es else "STEP 5 OF 5: CYCLE END, RESET & E-STOP",
            "titulo": "Fin de Ciclo Automático, Reset y Emergencias" if es else "Auto Cycle End, Reset & Emergency Controls",
            "icono": "🏁",
            "descripcion": "Los botones 'RESET' y 'PARO' están ILUMINADOS a la derecha.\n👉 Al terminar la matriz se reinicia automáticamente sin perder el Set 0. Presiona '↻ RESET' o '✅ Finalizar Inducción'." if es else "The 'RESET' and 'E-STOP' buttons are HIGHLIGHTED.\n👉 Tap '↻ RESET' or '✅ Finish Induction'.",
            "nota": "💡 ¡Excelente! Has completado el recorrido guiado por toda la interfaz." if es else "💡 Excellent! You have completed the guided interface walkthrough.",
            "widgets": [globals().get("btn_reset"), globals().get("btn_stop")]
        }
    return {}
    return {}

def iniciar_tutorial_interactivo(obligatorio=False, usuario_obj=None):
    """
    Guía interactiva paso a paso en la propia interfaz con iluminación de controles en vivo.
    - Tipografía grande y de alta legibilidad.
    - Avance automático al realizar en vivo la acción de cada paso.
    - Guía de cambio de MODO AUTO a MODO MANUAL previa a la calibración de Set 0.
    """
    global _hud_tutorial_frame, _paso_tutorial_actual, _hud_pos_y, _tutorial_activo
    global _fn_mostrar_paso, _fn_navegar_paso, _fn_concluir_tutorial

    try:
        # Asegurar pantalla del operador visible
        if 'frame_admin' in globals() and frame_admin.winfo_viewable():
            volver_a_panel_operador()
        elif 'frame_login' in globals() and frame_login.winfo_viewable():
            frame_login.pack_forget()
            frame_operador.pack(fill="both", expand=True)

        if _hud_tutorial_frame is not None and _hud_tutorial_frame.winfo_exists():
            try:
                _hud_tutorial_frame.destroy()
            except Exception:
                pass
            _restaurar_widgets_tutorial()

        _tutorial_activo = True
        _paso_tutorial_actual = 1
        _hud_pos_y = 0.88

        # Marco HUD con altura mayor (170 px) para acomodar texto grande y muy legible
        hud = ctk.CTkFrame(
            frame_operador,
            height=170,
            fg_color="#0F172A",
            corner_radius=12,
            border_width=3,
            border_color="#F59E0B"
        )
        _hud_tutorial_frame = hud
        hud.place(relx=0.5, rely=_hud_pos_y, anchor="center", relwidth=0.92)
        hud.lift()

        # Fila Superior: Badge + Título + Feedback Automático + Mover HUD + Salir/Obligatorio
        f_hud_top = ctk.CTkFrame(hud, fg_color="transparent")
        f_hud_top.pack(fill="x", padx=16, pady=(10, 4))

        lbl_hud_badge = ctk.CTkLabel(
            f_hud_top,
            text="",
            font=(FONT_MAIN, 11, "bold"),
            fg_color="#F59E0B",
            text_color="#0F172A",
            corner_radius=5,
            padx=10,
            pady=3
        )
        lbl_hud_badge.pack(side="left", padx=(0, 10))

        lbl_hud_title = ctk.CTkLabel(
            f_hud_top,
            text="",
            font=(FONT_MAIN, 15, "bold"),
            text_color="#FFFFFF"
        )
        lbl_hud_title.pack(side="left")

        global lbl_hud_feedback
        lbl_hud_feedback = ctk.CTkLabel(
            f_hud_top,
            text="⚡ Avance Automático Activo",
            font=(FONT_MAIN, 10, "bold"),
            fg_color="#065F46",
            text_color="#A7F3D0",
            corner_radius=5,
            padx=10,
            pady=3
        )
        lbl_hud_feedback.pack(side="left", padx=(14, 0))

        def toggle_pos():
            global _hud_pos_y
            _hud_pos_y = 0.17 if _hud_pos_y >= 0.5 else 0.88
            hud.place_configure(rely=_hud_pos_y)
            btn_hud_move.configure(text="⬇ Mover Abajo" if _hud_pos_y < 0.5 else "⬆ Mover Arriba")

        btn_hud_move = ctk.CTkButton(
            f_hud_top,
            text="⬆ Mover Arriba",
            font=(FONT_MAIN, 10, "bold"),
            width=110,
            height=28,
            fg_color="#334155",
            hover_color="#475569",
            text_color="#E2E8F0",
            corner_radius=5,
            command=toggle_pos
        )
        btn_hud_move.pack(side="right", padx=(6, 0))

        if obligatorio:
            lbl_hud_oblg = ctk.CTkLabel(
                f_hud_top,
                text="🔒 INDUCCIÓN OBLIGATORIA",
                font=(FONT_MAIN, 10, "bold"),
                fg_color="#DC2626",
                text_color="#FFFFFF",
                corner_radius=5,
                padx=10,
                pady=3
            )
            lbl_hud_oblg.pack(side="right", padx=(4, 6))
        else:
            btn_hud_close = ctk.CTkButton(
                f_hud_top,
                text="✕ Salir Guía",
                font=(FONT_MAIN, 10, "bold"),
                width=95,
                height=28,
                fg_color="#1E293B",
                hover_color="#EF4444",
                text_color="#94A3B8",
                corner_radius=5,
                command=lambda: cerrar_guia_voluntaria()
            )
            btn_hud_close.pack(side="right", padx=(4, 6))

        # Fila Central: Ícono grande + Descripción en letra grande y legible + Nota Tip
        f_hud_mid = ctk.CTkFrame(hud, fg_color="transparent")
        f_hud_mid.pack(fill="both", expand=True, padx=16, pady=4)

        lbl_hud_ico = ctk.CTkLabel(f_hud_mid, text="", font=(FONT_MAIN, 32))
        lbl_hud_ico.pack(side="left", padx=(4, 14))

        f_hud_txt = ctk.CTkFrame(f_hud_mid, fg_color="transparent")
        f_hud_txt.pack(side="left", fill="both", expand=True)

        lbl_hud_desc = ctk.CTkLabel(
            f_hud_txt,
            text="",
            font=(FONT_MAIN, 13, "bold"),
            text_color="#FFFFFF",
            justify="left",
            anchor="w",
            wraplength=950
        )
        lbl_hud_desc.pack(fill="x", anchor="w")

        lbl_hud_tip = ctk.CTkLabel(
            f_hud_txt,
            text="",
            font=(FONT_MAIN, 11, "bold"),
            text_color="#FDE047",
            justify="left",
            anchor="w",
            wraplength=950
        )
        lbl_hud_tip.pack(fill="x", anchor="w", pady=(4, 0))

        # Fila Inferior: Indicador de pasos + Botones Anterior / Siguiente
        f_hud_bot = ctk.CTkFrame(hud, fg_color="transparent")
        f_hud_bot.pack(fill="x", padx=16, pady=(4, 10))

        lbl_hud_dots = ctk.CTkLabel(
            f_hud_bot,
            text="",
            font=(FONT_MAIN, 11, "bold"),
            text_color="#94A3B8"
        )
        lbl_hud_dots.pack(side="left", padx=4)

        btn_hud_ant = ctk.CTkButton(
            f_hud_bot,
            text="◀ Anterior",
            font=(FONT_MAIN, 11, "bold"),
            width=100,
            height=32,
            fg_color="#334155",
            hover_color="#475569",
            text_color="#FFFFFF",
            corner_radius=6,
            command=lambda: navegar_paso(-1)
        )
        btn_hud_ant.pack(side="right", padx=6)

        btn_hud_sig = ctk.CTkButton(
            f_hud_bot,
            text="Siguiente ▶",
            font=(FONT_MAIN, 11, "bold"),
            width=130,
            height=32,
            fg_color=COLOR_KOSTAL_ACCENT,
            hover_color=COLOR_KOSTAL_LIGHT,
            text_color="#FFFFFF",
            corner_radius=6,
            command=lambda: avanzar_o_terminar()
        )
        btn_hud_sig.pack(side="right", padx=6)

        def mostrar_paso_actual():
            p_info = obtener_info_paso_tutorial(_paso_tutorial_actual)

            lbl_hud_badge.configure(text=p_info["badge"])
            lbl_hud_title.configure(text=p_info["titulo"])
            lbl_hud_ico.configure(text=p_info["icono"])
            lbl_hud_desc.configure(text=p_info["descripcion"])
            lbl_hud_tip.configure(text=p_info["nota"])

            dots_str = "     ".join([
                f"● {i}. {nom}" if i == _paso_tutorial_actual else f"○ {i}. {nom}"
                for i, nom in enumerate(["Receta", "Modo/Set 0", "Home", "Pintado", "Fin/Reset"], 1)
            ])
            lbl_hud_dots.configure(text=dots_str)

            btn_hud_ant.configure(state="normal" if _paso_tutorial_actual > 1 else "disabled")
            if _paso_tutorial_actual == 5:
                btn_hud_sig.configure(
                    text="✅ Finalizar Inducción",
                    fg_color=COLOR_OK,
                    hover_color=COLOR_OK_HOVER,
                    width=160
                )
            else:
                btn_hud_sig.configure(
                    text="Siguiente ▶",
                    fg_color=COLOR_KOSTAL_ACCENT,
                    hover_color=COLOR_KOSTAL_LIGHT,
                    width=130
                )

            widgets_a_iluminar = [w for w in p_info.get("widgets", []) if w is not None]
            _resaltar_elementos_interfaz(widgets_a_iluminar)
            lbl_estado.configure(
                text=f"🎓 Tutorial Interactivo: {_paso_tutorial_actual}/5 - {p_info['titulo']} (Realiza la acción para avanzar automáticamente)",
                text_color="#F59E0B"
            )

        def avanzar_o_terminar():
            global _paso_tutorial_actual
            if _paso_tutorial_actual < 5:
                _paso_tutorial_actual += 1
                mostrar_paso_actual()
            else:
                concluir_tutorial()

        def navegar_paso(delta):
            global _paso_tutorial_actual
            nuevo = _paso_tutorial_actual + delta
            if 1 <= nuevo <= 5:
                _paso_tutorial_actual = nuevo
                mostrar_paso_actual()

        def cerrar_guia_voluntaria():
            global _tutorial_activo
            if obligatorio:
                messagebox.showwarning(
                    "Inducción Obligatoria",
                    "Como nuevo usuario en el sistema, debes recorrer los 5 pasos del tutorial para finalizar tu inducción."
                )
                return
            _tutorial_activo = False
            _restaurar_widgets_tutorial()
            if hud.winfo_exists():
                hud.destroy()
            lbl_estado.configure(text="Tutorial interactivo cerrado.", text_color=TEXT_MUTED)

        def concluir_tutorial():
            global _tutorial_activo
            _tutorial_activo = False
            _restaurar_widgets_tutorial()
            if hud.winfo_exists():
                hud.destroy()

            if usuario_obj:
                usuario_obj["tutorial_completado"] = True
                for u in USUARIOS_SISTEMA:
                    if u.get("nombre") == usuario_obj.get("nombre"):
                        u["tutorial_completado"] = True
                        break
                config_actual["usuarios"] = list(USUARIOS_SISTEMA)
                guardar_configuracion()
                lbl_estado.configure(
                    text=f"✓ Inducción completada con éxito por {usuario_obj.get('nombre')}. Operación 100% autorizada.",
                    text_color=COLOR_OK
                )
                messagebox.showinfo(
                    "¡Inducción Completada!",
                    f"¡Felicidades {usuario_obj.get('nombre')}!\n\n"
                    "Has completado satisfactoriamente el recorrido interactivo en la HMI KOSTAL:\n"
                    "• 1. Selección de Receta y Jig\n"
                    "• 2. Modo Manual y Calibración de Set 0 con Cruceta JOG\n"
                    "• 3. Referenciado seguro a Home\n"
                    "• 4. Ciclo automático y disparo por Gatillo\n"
                    "• 5. Fin de Ciclo, Reset y Paro de Emergencia\n\n"
                    "La estación se encuentra autorizada para operar."
                )
            else:
                lbl_estado.configure(text="✓ Tutorial interactivo finalizado con éxito.", text_color=COLOR_OK)

        _fn_mostrar_paso = mostrar_paso_actual
        _fn_navegar_paso = navegar_paso
        _fn_concluir_tutorial = concluir_tutorial

        mostrar_paso_actual()
    except Exception as e:
        print(f"Error iniciando tutorial interactivo: {e}")

abrir_tutorial_proceso = iniciar_tutorial_interactivo

def cargar_receta_por_nombre(nombre_receta, mostrar_modal_aviso=True):
    """
    Carga la receta seleccionada:
    - Actualiza tamaño de matriz (filas, columnas <= 5x5)
    - Actualiza distancia de centro a centro en X y Y
    - Actualiza coordenadas de matriz partiendo de SET 0
    - Muestra nombre, modelo, nombre de pieza e imagen en el panel principal
    - Redibuja el fixture y actualiza KPIs
    """
    global matriz_filas, matriz_cols, matriz_total, piezas_por_jig_actual, matriz_delta_x, matriz_delta_y, matriz_dist_diag
    global fixture_piezas_estado, indice_matriz_actual, cero_ha_sido_seteado, maquina_en_home
    global receta_actual_calibrada, cero_guardado_x, cero_guardado_y
    
    receta = None
    for r in config_actual.get("recetas", []):
        if r.get("nombre") == nombre_receta:
            receta = r
            break
            
    if not receta:
        if config_actual.get("recetas"):
            receta = config_actual["recetas"][0]
            nombre_receta = receta["nombre"]
        else:
            return

    cambio_de_receta = False
    if 'receta_actual_calibrada' in globals() and receta_actual_calibrada:
        if receta_actual_calibrada != nombre_receta:
            cambio_de_receta = True
    else:
        receta_actual_calibrada = nombre_receta

    config_actual["receta_activa"] = nombre_receta
    receta_actual_calibrada = nombre_receta
    guardar_configuracion()
    
    # REQUERIMIENTO: Si la receta ya se corrió al menos una vez o ya tiene Set 0 calibrado,
    # se mantiene ajustado su Set 0 como predeterminado y queda listo para pintar.
    ya_calibrada = bool(
        receta.get("cero_calibrado", False) 
        or receta.get("corridas", 0) > 0 
        or ("cero_guardado_x" in receta and "cero_guardado_y" in receta)
    )
    
    if ya_calibrada:
        cero_guardado_x = limitar_coord_x(float(receta.get("cero_guardado_x", 0.0)))
        cero_guardado_y = limitar_coord_y(float(receta.get("cero_guardado_y", 0.0)))
        config_actual["cero_guardado_x"] = cero_guardado_x
        config_actual["cero_guardado_y"] = cero_guardado_y
        cero_ha_sido_seteado = True
        maquina_en_home = False
        
        if 'caja_pos_x' in globals():
            caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{cero_guardado_x:.2f}")
        if 'caja_pos_y' in globals():
            caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{cero_guardado_y:.2f}")
        if 'lbl_jog_coord_x' in globals():
            lbl_jog_coord_x.configure(text=f"{cero_guardado_x:.2f}")
        if 'lbl_jog_coord_y' in globals():
            lbl_jog_coord_y.configure(text=f"{cero_guardado_y:.2f}")
            
        if 'lbl_indicador_set0' in globals() and lbl_indicador_set0:
            lbl_indicador_set0.configure(
                text=f"✓ CERO PREDETERMINADO: X={cero_guardado_x:.2f}  Y={cero_guardado_y:.2f} mm",
                text_color="#065F46"
            )
        if 'f_cero_ind' in globals() and f_cero_ind:
            f_cero_ind.configure(fg_color="#ECFDF5", border_color="#A7F3D0")
        if 'lbl_cero_jog_val' in globals() and lbl_cero_jog_val:
            lbl_cero_jog_val.configure(text=f"X: {cero_guardado_x:.2f} mm  |  Y: {cero_guardado_y:.2f} mm")
    else:
        cero_ha_sido_seteado = False
        maquina_en_home = False
        if 'lbl_indicador_set0' in globals() and lbl_indicador_set0:
            lbl_indicador_set0.configure(
                text="⚠️ SET 0 PENDIENTE: Calibre origen con JOG y presione 'Set 0'",
                text_color="#B45309"
            )
        if 'f_cero_ind' in globals() and f_cero_ind:
            f_cero_ind.configure(fg_color="#FEF3C7", border_color="#FDE68A")
    
    # 1. Geometría de matriz (configurable arbitrariamente) y piezas por jig independientes
    matriz_filas = max(1, int(receta.get("filas", 3)))
    matriz_cols = max(1, int(receta.get("cols", 5)))
    matriz_total = matriz_filas * matriz_cols
    piezas_por_jig_actual = obtener_piezas_por_jig_receta(receta)
    matriz_delta_x = float(receta.get("delta_x", 25.0))
    matriz_delta_y = float(receta.get("delta_y", 30.0))
    matriz_dist_diag = round((matriz_delta_x**2 + matriz_delta_y**2)**0.5, 2)
    
    # 2. Velocidad si aplica
    if "velocidad" in receta and 'caja_vel' in globals():
        try:
            caja_vel.delete(0, tk.END)
            caja_vel.insert(0, str(receta["velocidad"]))
        except Exception:
            pass
            
    # 3. Coordenadas físicas desde SET 0
    actualizar_coordenadas_matriz()
    indice_matriz_actual = 0
    fixture_piezas_estado = ["pendiente"] * matriz_total
    
    # 4. Actualizar selector del operador
    if 'option_receta' in globals():
        try:
            option_receta.set(nombre_receta)
        except Exception:
            pass
            
    # 5. Textos informativos de pieza y modelo
    if 'lbl_proc_pieza' in globals():
        lbl_proc_pieza.configure(text=receta.get("pieza", "Pieza SMC"))
    if 'lbl_proc_modelo' in globals():
        lbl_proc_modelo.configure(text=receta.get("modelo", "KSL-001"))
        
    # 6. Imagen de la pieza
    actualizar_imagen_pieza_proceso(receta.get("imagen", ""))
    
    # 7. Actualización visual
    actualizar_ui_matriz()
    if 'canvas_fixture' in globals():
        dibujar_fixture_moderno()
    actualizar_kpis_fixture()
    
    if 'lbl_estado' in globals():
        if cambio_de_receta:
            if ya_calibrada:
                lbl_estado.configure(
                    text=f"✓ Jig/Receta '{nombre_receta}' cargada con su Cero predeterminado (X:{cero_guardado_x:.2f} Y:{cero_guardado_y:.2f} mm). Listo para iniciar pintado.",
                    text_color=COLOR_OK
                )
            else:
                lbl_estado.configure(
                    text=f"⚠️ Cambio a Jig '{nombre_receta}'. Debe calibrar 'Set 0' con la Cruceta JOG antes de iniciar el pintado.",
                    text_color=COLOR_WARN
                )
                # Durante el Paso 1 del tutorial guiado o si viene de confirmación explícita, no desplegar modal intrusivo
                if mostrar_modal_aviso and not ('_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 1):
                    ventana.after(300, lambda nr=nombre_receta: mostrar_alerta_cambio_jig(nr))
        else:
            if ya_calibrada:
                lbl_estado.configure(
                    text=f"✓ Receta '{nombre_receta}' lista | Matriz: {matriz_filas}×{matriz_cols} | Cero predeterminado: X={cero_guardado_x:.2f} Y={cero_guardado_y:.2f} mm.",
                    text_color=COLOR_OK
                )
            else:
                lbl_estado.configure(
                    text=f"⚠️ Receta '{nombre_receta}' cargada | Matriz: {matriz_filas}×{matriz_cols} | Set 0 PENDIENTE de calibrar.",
                    text_color=COLOR_WARN
                )

    # Si el tutorial guiado está activo en el Paso 1, avanzar automáticamente al Paso 2
    if '_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 1:
        notificar_accion_tutorial(1, delay_ms=750)

def resetear_variables_inicio(forzar_reseteo_referencias=False):
    """
    Resetea todas las variables del sistema y del PLC a su estado inicial.
    Set 0 y Home se conservan a menos que se cambie la receta.
    """
    global estado_pintar, estado_stop, estado_manual, cero_ha_sido_seteado, maquina_en_home
    global indice_matriz_actual, ultimo_estado_pintado, ultimo_estado_sensor_y
    global fixture_piezas_estado, segundos_sesion, pasada_actual_pieza
    global contador_ciclos_completados, total_piezas_sesion_acum
    global kpis_fin_hora_registrados, piezas_ok_registradas, piezas_nok_registradas, calidad_registrada
    
    # 1. Estados internos y contadores en 0
    estado_pintar = False
    estado_stop = False
    estado_manual = False
    if forzar_reseteo_referencias:
        cero_ha_sido_seteado = False
        maquina_en_home = False
    indice_matriz_actual = 0
    pasada_actual_pieza = 1
    ultimo_estado_pintado = False
    ultimo_estado_sensor_y = False
    segundos_sesion = 0
    contador_ciclos_completados = 0
    total_piezas_sesion_acum = 0
    kpis_fin_hora_registrados = False
    piezas_ok_registradas = 0
    piezas_nok_registradas = 0
    calidad_registrada = 100.0
    if 'lbl_ciclo_badge' in globals() and lbl_ciclo_badge:
        lbl_ciclo_badge.configure(text="🏁 CICLOS: 0")
    
    # 2. Coordenadas a 0.0 mm
    if 'caja_pos_x' in globals():
        caja_pos_x.delete(0, tk.END)
        caja_pos_x.insert(0, "0.0")
    if 'caja_pos_y' in globals():
        caja_pos_y.delete(0, tk.END)
        caja_pos_y.insert(0, "0.0")
        
    if 'lbl_jog_coord_x' in globals():
        lbl_jog_coord_x.configure(text="0.00")
    if 'lbl_jog_coord_y' in globals():
        lbl_jog_coord_y.configure(text="0.00")
        
    # 3. Reseteo de nodos del PLC a 0 / False (sin sobreescribir ejes ni pulso de reset)
    vars_a_cero = {
        "INICIO": False,
        "HOME": False,
        "STOP": False,
        "PINTAR": False,
        "CRUZETA": False,
        "MANUAL": False,
        "PINTADO": False,
        "NUEVA_POS": False,
        "NUEVA_POSICION": False,
        "SENSOR_X": False,
        "SENSOR_Y": False
    }
    
    for var_nom, val_cero in vars_a_cero.items():
        try:
            if var_nom in nodos:
                if plc_conectado:
                    if isinstance(val_cero, bool):
                        nodos[var_nom].set_value(ua.Variant(val_cero, ua.VariantType.Boolean))
                    elif isinstance(val_cero, int):
                        nodos[var_nom].set_value(ua.Variant(val_cero, ua.VariantType.Int32))
                else:
                    nodos[var_nom].set_value(val_cero)
        except Exception:
            pass
            
    # 4. Reseteo de matriz fixture: todas las piezas a estado pendiente (0 realizadas)
    fixture_piezas_estado = ["pendiente"] * matriz_total
    
    # 5. Reseteo de KPIs visuales en 0
    if 'lbl_kpi_total' in globals() and lbl_kpi_total:
        lbl_kpi_total.configure(text="0")
    if 'lbl_kpi_ok' in globals() and lbl_kpi_ok:
        lbl_kpi_ok.configure(text="0")
    if 'lbl_kpi_nok' in globals() and lbl_kpi_nok:
        lbl_kpi_nok.configure(text="0")
    if 'lbl_kpi_calidad' in globals() and lbl_kpi_calidad:
        lbl_kpi_calidad.configure(text="100.0%")
    if 'lbl_piezas_realizadas' in globals():
        lbl_piezas_realizadas.configure(text=f"00 / {matriz_total:02d}")
        
    # 6. Botones de estado visual
    if 'btn_pintar' in globals():
        btn_pintar.configure(text="🎨\nINICIO DE\nPINTADO\n[OFF]", fg_color=COLOR_KOSTAL_NAVY, text_color="white", border_color="#94A3B8")
    if 'btn_manual' in globals():
        btn_manual.configure(text="⚙ MODO: AUTO", fg_color=COLOR_KOSTAL_NAVY, text_color="white")
    if 'btn_stop' in globals():
        btn_stop.configure(text="🛑 PARO", fg_color=COLOR_NOK)
        
    # 7. Redibujo del canvas del fixture
    if 'canvas_fixture' in globals():
        try:
            dibujar_fixture_moderno()
        except Exception:
            pass
            
    if 'lbl_estado' in globals():
        lbl_estado.configure(text="✓ Variables inicializadas en 0.", text_color=COLOR_OK)

# Variables de sesión y usuarios
usuario_actual = {"nombre": "Juan Pérez", "rol": "OPERADOR", "num": "KOSTAL-0842"}
segundos_sesion = 0
temporizador_sesion_id = None
col_der_visible = True
mostrar_pass = False

USUARIOS_SISTEMA = config_actual.get("usuarios", DEFAULT_USUARIOS)

# ==============================================================================
# 3. FUNCIONES DE CONTROL
# ==============================================================================
def enviar_pulso(nombre_variable, duracion_ms=350):
    try:
        if plc_conectado:
            nodos[nombre_variable].set_value(ua.Variant(True, ua.VariantType.Boolean))
            ventana.after(duracion_ms, lambda: _resetear_pulso(nombre_variable))
        else:
            nodos[nombre_variable].set_value(True)
            ventana.after(duracion_ms, lambda: nodos[nombre_variable].set_value(False))
        lbl_estado.configure(text=f"✓ Pulso {nombre_variable} enviado ({duracion_ms}ms)", text_color=COLOR_OK)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error en pulso {nombre_variable}: {e}", text_color=COLOR_NOK)

def _resetear_pulso(nombre_variable):
    try:
        if plc_conectado:
            nodos[nombre_variable].set_value(ua.Variant(False, ua.VariantType.Boolean))
        else:
            nodos[nombre_variable].set_value(False)
    except Exception:
        pass

def activar_nueva_posicion(duracion_ms=500):
    """
    Garantiza la activación certera de la señal de nueva posición en el PLC WAGO.
    1. Activa tanto 'NUEVA_POS' como 'NUEVA_POSICION' para asegurar 100% compatibilidad
       con la variable declarada en CODESYS.
    2. Utiliza una duración de 500 ms para que el ciclo de escaneo del PLC la lea sin fallos.
    3. Maneja excepciones individualmente para que no se bloquee si una variable no existe.
    """
    activado_ok = False
    for tag in ["NUEVA_POS", "NUEVA_POSICION"]:
        if tag in nodos:
            try:
                if plc_conectado:
                    nodos[tag].set_value(ua.Variant(True, ua.VariantType.Boolean))
                else:
                    nodos[tag].set_value(True)
                activado_ok = True
            except Exception:
                pass
                
    def _apagar_senales():
        for tag in ["NUEVA_POS", "NUEVA_POSICION"]:
            if tag in nodos:
                try:
                    if plc_conectado:
                        nodos[tag].set_value(ua.Variant(False, ua.VariantType.Boolean))
                    else:
                        nodos[tag].set_value(False)
                except Exception:
                    pass
                    
    ventana.after(duracion_ms, _apagar_senales)
    if activado_ok:
        lbl_estado.configure(text=f"✓ Señal NUEVA_POS activada ({duracion_ms}ms) en PLC", text_color=COLOR_OK)

# ==============================================================================
# ACTIVACIÓN MANUAL DE TAMPOGRAFÍA (MARCA S.tampo / S_TAMPO EN PLC)
# ==============================================================================
_tampo_en_ejecucion = False

def activar_tampo_manual():
    """
    Activa la marca M_PINTAR / S_TAMPO / TAMPO en el PLC WAGO durante 2.0 segundos exactos (True).
    Al concluir los 2.0 segundos, se desactiva (pasa a False) y el botón se desajusta/restaura.
    Disponible en estado de espera (cuando no hay ciclo activo de pintado) o en Modo Manual.
    """
    global _tampo_en_ejecucion
    if estado_pintar:
        lbl_estado.configure(
            text="⚠️ 'Activar Tampo' no está disponible mientras el ciclo de pintado esté en ejecución.",
            text_color=COLOR_WARN
        )
        return

    if _tampo_en_ejecucion:
        return
    _tampo_en_ejecucion = True

    # 1. Poner M_PINTAR, S_TAMPO y TAMPO en True en el PLC WAGO
    for tag in ["M_PINTAR", "S_TAMPO", "TAMPO"]:
        if tag in nodos:
            try:
                if plc_conectado:
                    nodos[tag].set_value(ua.Variant(True, ua.VariantType.Boolean))
                else:
                    nodos[tag].set_value(True)
            except Exception:
                pass

    # 2. Feedback visual en el botón durante los 2 segundos
    if 'btn_activar_tampo' in globals():
        try:
            btn_activar_tampo.configure(
                text="⏳ TAMPO ACTIVO (2s)" if idioma_actual == "ES" else "⏳ PAD ACTIVE (2s)",
                fg_color="#D97706",
                hover_color="#B45309",
                text_color="white",
                state="disabled"
            )
        except Exception:
            pass

    lbl_estado.configure(
        text="⚡ Tampo activado (M_PINTAR=ON): ciclo de 2.0 segundos en ejecución..." if idioma_actual == "ES" else "⚡ Pad activated (M_PINTAR=ON): 2.0 second cycle running...",
        text_color=COLOR_WARN
    )

    # Actualizar texto a 1s cuando haya transcurrido 1 segundo
    def _actualizar_segundo():
        if _tampo_en_ejecucion and 'btn_activar_tampo' in globals():
            try:
                btn_activar_tampo.configure(
                    text="⏳ TAMPO ACTIVO (1s)" if idioma_actual == "ES" else "⏳ PAD ACTIVE (1s)"
                )
            except Exception:
                pass

    ventana.after(1000, _actualizar_segundo)

    # 3. Al concluir los 2000 ms, desactivar en el PLC (poner en False) y restaurar botón
    def _desactivar_tampo():
        global _tampo_en_ejecucion
        for tag in ["M_PINTAR", "S_TAMPO", "TAMPO"]:
            if tag in nodos:
                try:
                    if plc_conectado:
                        nodos[tag].set_value(ua.Variant(False, ua.VariantType.Boolean))
                    else:
                        nodos[tag].set_value(False)
                except Exception:
                    pass

        _tampo_en_ejecucion = False
        if 'btn_activar_tampo' in globals():
            try:
                btn_activar_tampo.configure(
                    text=DICCIONARIO_IDIOMAS[idioma_actual]["activar_tampo"],
                    fg_color="#FFFFFF",
                    hover_color="#F1F5F9",
                    text_color=COLOR_KOSTAL_ACCENT,
                    state="normal"
                )
            except Exception:
                pass

        lbl_estado.configure(
            text="✓ Tampo completó ciclo de 2 segundos y se desactivó (M_PINTAR=OFF)." if idioma_actual == "ES" else "✓ Pad completed 2-second cycle and turned off (M_PINTAR=OFF).",
            text_color=COLOR_OK
        )

    ventana.after(2000, _desactivar_tampo)

# ==============================================================================
# GESTIÓN Y CAMBIO DE IDIOMA DINÁMICO (ESPAÑOL <-> INGLÉS)
# ==============================================================================
idioma_actual = "ES"

DICCIONARIO_IDIOMAS = {
    "ES": {
        "titulo_proceso": "INFORMACIÓN DE PROCESO",
        "receta_label": "Receta / Jig Activo:",
        "pieza": "Pieza:",
        "modelo": "Modelo:",
        "matriz_fixture": "Matriz Fixture:",
        "dist_cc": "Dist. C-C:",
        "fin_hora": "⏱️  Registrar Fin de Hora",
        "ctrl_pintado": "CONTROL PRINCIPAL DE PINTADO",
        "btn_pintar_off": "🎨\nINICIO DE\nPINTADO\n[OFF]",
        "btn_pintar_on": "🎨\nPINTANDO\nACTIVO\n[ON]",
        "lbl_pintar_sub": "Toque para alternar ciclo automático",
        "estado_fixture": "ESTADO DEL FIXTURE",
        "recetario_admin": "🛠️ Recetario Admin",
        "coords": "Coords",
        "probar_mov": "⚡ Probar Movimiento",
        "diag_plc": "DIAGNÓSTICO & SEÑALES DEL PLC WAGO",
        "op_jog": "OPERACIÓN & JOG",
        "plegar": "▲ Plegar",
        "desplegar": "▼ Desplegar",
        "operacion": "⚡ OPERACIÓN",
        "modo_auto": "⚙ MODO: AUTO",
        "modo_manual": "⚙ MODO: MANUAL",
        "velocidad": "Velocidad:",
        "jog_manual": "🕹️ JOG MANUAL",
        "paso_jog": "Paso JOG:",
        "coord_box": "COORD (0-200)",
        "cero_registrado": "🔒 CERO REGISTRADO:",
        "origen": "ORIGEN",
        "home": "🏠 Home",
        "set_home": "⚙ Set Home",
        "set_0": "↩ Set 0",
        "activar_tampo": "🏷️ Activar Tampo",
        "mandos_control": "MANDOS DE CONTROL",
        "inicio": "▶  INICIO",
        "reset": "↻  RESET",
        "paro": "🛑  PARO",
        "cerrar_sesion": "⏻ Cerrar Sesión",
        "tutorial": "📖 Tutorial",
        "pantalla": "⛶ Pantalla",
        "minimizar": "🗕 Minimizar",
        "usuario": "Usuario / Perfil",
        "contrasena": "Contraseña",
        "iniciar_sesion": "Iniciar Sesión  ➜",
        "sistema_hmi": "SISTEMA HMI DE INSPECCIÓN",
        "linea_ensamblado": "Línea de Ensamblado & Calidad KOSTAL",
        "msg_init": "Sistema HMI inicializado correctamente. Esperando autenticación...",
        "status_listo": "ESTADO: LISTO",
        "status_esperando": "ESPERANDO PINTADO...",
        "status_energ": "ENERGIZADOS",
        "status_origenes": "ORÍGENES LISTOS"
    },
    "EN": {
        "titulo_proceso": "PROCESS INFORMATION",
        "receta_label": "Active Recipe / Jig:",
        "pieza": "Part:",
        "modelo": "Model:",
        "matriz_fixture": "Fixture Matrix:",
        "dist_cc": "Center Dist:",
        "fin_hora": "⏱️  Register Hourly Output",
        "ctrl_pintado": "MAIN PRINTING CONTROL",
        "btn_pintar_off": "🎨\nSTART\nPRINTING\n[OFF]",
        "btn_pintar_on": "🎨\nPRINTING\nACTIVE\n[ON]",
        "lbl_pintar_sub": "Tap to toggle automatic cycle",
        "estado_fixture": "FIXTURE STATUS",
        "recetario_admin": "🛠️ Admin Recipes",
        "coords": "Coords",
        "probar_mov": "⚡ Test Motion",
        "diag_plc": "PLC DIAGNOSTICS & SIGNALS",
        "op_jog": "OPERATION & JOG",
        "plegar": "▲ Fold",
        "desplegar": "▼ Expand",
        "operacion": "⚡ OPERATION",
        "modo_auto": "⚙ MODE: AUTO",
        "modo_manual": "⚙ MODE: MANUAL",
        "velocidad": "Speed:",
        "jog_manual": "🕹️ MANUAL JOG",
        "paso_jog": "JOG Step:",
        "coord_box": "COORD (0-200)",
        "cero_registrado": "🔒 SAVED ZERO:",
        "origen": "ORIGIN",
        "home": "🏠 Home",
        "set_home": "⚙ Set Home",
        "set_0": "↩ Set 0",
        "activar_tampo": "🏷️ Trigger Pad",
        "mandos_control": "CONTROL COMMANDS",
        "inicio": "▶  START",
        "reset": "↻  RESET",
        "paro": "🛑  E-STOP",
        "cerrar_sesion": "⏻ Log Out",
        "tutorial": "📖 Tutorial",
        "pantalla": "⛶ Fullscreen",
        "minimizar": "🗕 Minimize",
        "usuario": "User / Profile",
        "contrasena": "Password",
        "iniciar_sesion": "Sign In  ➜",
        "sistema_hmi": "HMI INSPECTION SYSTEM",
        "linea_ensamblado": "KOSTAL Assembly & Quality Line",
        "msg_init": "HMI System initialized successfully. Waiting for authentication...",
        "status_listo": "STATUS: READY",
        "status_esperando": "WAITING FOR PRINT...",
        "status_energ": "ENERGIZED",
        "status_origenes": "ORIGINS READY"
    }
}

def cambiar_idioma(nuevo_idioma):
    global idioma_actual
    if nuevo_idioma not in DICCIONARIO_IDIOMAS:
        return
    idioma_actual = nuevo_idioma
    dic = DICCIONARIO_IDIOMAS[nuevo_idioma]

    # Actualizar estado visual de los botones de idioma
    if 'btn_lang_es' in globals() and 'btn_lang_en' in globals():
        if nuevo_idioma == "ES":
            btn_lang_es.configure(fg_color=COLOR_KOSTAL_NAVY, text_color="white")
            btn_lang_en.configure(fg_color="transparent", text_color=TEXT_MUTED)
        else:
            btn_lang_en.configure(fg_color=COLOR_KOSTAL_NAVY, text_color="white")
            btn_lang_es.configure(fg_color="transparent", text_color=TEXT_MUTED)

    if 'btn_adm_lang_es' in globals() and 'btn_adm_lang_en' in globals():
        if nuevo_idioma == "ES":
            btn_adm_lang_es.configure(fg_color="#004F9F", text_color="white")
            btn_adm_lang_en.configure(fg_color="transparent", text_color="#94A3B8")
        else:
            btn_adm_lang_en.configure(fg_color="#004F9F", text_color="white")
            btn_adm_lang_es.configure(fg_color="transparent", text_color="#94A3B8")

    # Títulos de Tarjetas
    if 'card_proc' in globals() and hasattr(card_proc, '_header_title'):
        card_proc._header_title.configure(text=dic["titulo_proceso"])
    if 'lbl_rec_title' in globals():
        lbl_rec_title.configure(text=dic["receta_label"])
    if 'card_marcha' in globals() and hasattr(card_marcha, '_header_title'):
        card_marcha._header_title.configure(text=dic["ctrl_pintado"])
    if 'card_fixture' in globals() and hasattr(card_fixture, '_header_title'):
        card_fixture._header_title.configure(text=f"{dic['estado_fixture']} ({matriz_filas} × {matriz_cols})")
    if 'card_diag' in globals() and hasattr(card_diag, '_header_title'):
        card_diag._header_title.configure(text=dic["diag_plc"])
    if 'card_jog' in globals() and hasattr(card_jog, '_header_title'):
        card_jog._header_title.configure(text=dic["op_jog"])
    if 'card_mandos_der' in globals() and hasattr(card_mandos_der, '_header_title'):
        card_mandos_der._header_title.configure(text=dic["mandos_control"])

    # Botones principales
    if 'btn_manual' in globals():
        btn_manual.configure(text=dic["modo_manual"] if estado_manual else dic["modo_auto"])
    if 'btn_pintar' in globals():
        btn_pintar.configure(text=dic["btn_pintar_on"] if estado_pintar else dic["btn_pintar_off"])
    if 'lbl_pintar_sub' in globals():
        lbl_pintar_sub.configure(text=dic["lbl_pintar_sub"])
    if 'btn_inicio' in globals():
        btn_inicio.configure(text=dic["inicio"])
    if 'btn_reset' in globals():
        btn_reset.configure(text=dic["reset"])
    if 'btn_stop' in globals():
        btn_stop.configure(text=dic["paro"])
    if 'btn_fin_de_hora' in globals():
        btn_fin_de_hora.configure(text=dic["fin_hora"])
    if 'btn_ir_home' in globals():
        btn_ir_home.configure(text=dic["home"])
    if 'btn_set_home' in globals():
        btn_set_home.configure(text=dic["set_home"])
    if 'btn_set_cero' in globals():
        btn_set_cero.configure(text=dic["set_0"])
    if 'btn_activar_tampo' in globals():
        btn_activar_tampo.configure(text=dic["activar_tampo"])
    if 'btn_logout_top' in globals():
        btn_logout_top.configure(text=dic["cerrar_sesion"])
    if 'btn_tutorial' in globals():
        btn_tutorial.configure(text=dic["tutorial"])

    # Botones de ventana
    for b in ['btn_login_fs', 'btn_sb_fs', 'btn_fs_top']:
        if b in globals():
            globals()[b].configure(text=dic["pantalla"])
    for b in ['btn_login_min', 'btn_sb_min', 'btn_min_top']:
        if b in globals():
            globals()[b].configure(text=dic["minimizar"])

    # Login
    if 'lbl_login_user' in globals():
        lbl_login_user.configure(text=dic["usuario"])
    if 'lbl_login_pass' in globals():
        lbl_login_pass.configure(text=dic["contrasena"])
    if 'btn_iniciar_sesion' in globals():
        btn_iniciar_sesion.configure(text=dic["iniciar_sesion"])
    if 'lbl_login_sys' in globals():
        lbl_login_sys.configure(text=dic["sistema_hmi"])
    if 'lbl_login_sub' in globals():
        lbl_login_sub.configure(text=dic["linea_ensamblado"])

    lbl_estado.configure(
        text=f"Idioma cambiado a {'Español' if nuevo_idioma == 'ES' else 'English'}.",
        text_color=COLOR_OK
    )

def ejecutar_home_general():
    global maquina_en_home
    try:
        home_x = limitar_coord(nodos["HOME_X"].get_value() / 100.0)
        home_y = limitar_coord(nodos["HOME_Y"].get_value() / 100.0)
        caja_pos_x.delete(0, tk.END)
        caja_pos_x.insert(0, f"{home_x:.2f}")
        caja_pos_y.delete(0, tk.END)
        caja_pos_y.insert(0, f"{home_y:.2f}")
        if 'lbl_jog_coord_x' in globals():
            lbl_jog_coord_x.configure(text=f"{home_x:.2f}")
        if 'lbl_jog_coord_y' in globals():
            lbl_jog_coord_y.configure(text=f"{home_y:.2f}")
        pos_ok = enviar_datos(verificar_eco=True)
        if pos_ok or not plc_conectado:
            enviar_pulso("HOME")
            maquina_en_home = True
            lbl_estado.configure(text="✓ Home ejecutado. Máquina en origen HOME sincronizada.", text_color=COLOR_OK)
        else:
            def _reintentar_home():
                if enviar_datos(verificar_eco=True) or not plc_conectado:
                    enviar_pulso("HOME")
                    maquina_en_home = True
            ventana.after(60, _reintentar_home)
            lbl_estado.configure(text="⚠️ Sincronizando coordenadas HOME con PLC antes de mover...", text_color=COLOR_WARN)
        if '_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 3:
            notificar_accion_tutorial(3, delay_ms=800)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al ir a Home: {e}", text_color=COLOR_NOK)

def ejecutar_ir_a_cero():
    """Ejecuta internamente el viaje a las coordenadas de Cero de Pintado"""
    global cero_guardado_x, cero_guardado_y
    try:
        caja_pos_x.delete(0, tk.END)
        caja_pos_x.insert(0, f"{cero_guardado_x:.2f}")
        caja_pos_y.delete(0, tk.END)
        caja_pos_y.insert(0, f"{cero_guardado_y:.2f}")
        pos_ok = enviar_datos(verificar_eco=True)
        if pos_ok or not plc_conectado:
            enviar_pulso("CRUZETA")
            lbl_estado.configure(text=f"✓ Viajando a CERO de Pintado (X:{cero_guardado_x:.2f} Y:{cero_guardado_y:.2f}).", text_color=COLOR_OK)
        else:
            def _reintentar_cero():
                if enviar_datos(verificar_eco=True) or not plc_conectado:
                    enviar_pulso("CRUZETA")
            ventana.after(60, _reintentar_cero)
            lbl_estado.configure(text=f"⚠️ Confirmando coordenadas de CERO con PLC...", text_color=COLOR_WARN)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al ir a Cero: {e}", text_color=COLOR_NOK)

def iniciar_ciclo_general():
    """
    Al presionar INICIO:
    Activa únicamente la variable INICIO del PLC (no activa la cruzeta ni viaja a cero).
    REQUERIMIENTO: Si se activó y desactivó STOP, valida que se haya presionado RESET antes de permitir INICIO.
    """
    global paro_requiere_reset, paro_requiere_inicio
    
    if estado_stop:
        lbl_estado.configure(text="🛑 Paro activado: Desactive el PARO primero.", text_color=COLOR_NOK)
        messagebox.showwarning("Paro Activo", "El PARO de emergencia está activado. Debe liberarlo primero, luego presionar RESET e INICIO.")
        return
        
    if paro_requiere_reset:
        lbl_estado.configure(
            text="⚠️ ACCIÓN REQUERIDA TRAS PARO: Presione primero 'RESET' antes de 'INICIO'.",
            text_color=COLOR_WARN
        )
        messagebox.showwarning(
            "Secuencia Post-Paro Requerida",
            "⚠️ El PARO fue desactivado.\n\n"
            "1. Presione primero el botón [RESET].\n"
            "2. Posteriormente presione el botón [INICIO]."
        )
        return
        
    if paro_requiere_inicio:
        paro_requiere_inicio = False
        lbl_estado.configure(text="✓ Secuencia post-paro completada: Señal INICIO enviada al PLC.", text_color=COLOR_OK)
    else:
        lbl_estado.configure(text="▶ INICIO: Señal INICIO enviada al PLC (sin activar cruzeta).", text_color=COLOR_OK)
        
    enviar_pulso("INICIO")

def ejecutar_reset_general():
    """
    Ejecuta la secuencia de RESET:
    1. Envía el pulso RESET al PLC manteniéndolo activo 350 ms para que CODESYS lo detecte con certeza.
    2. Cancela el pintado activo (PINTAR=False) y apaga el botón de pintar.
    3. Desactiva el PARO (STOP=False) si estaba enclavado.
    4. Satisface el paso 1 tras paro, requiriendo ahora presionar INICIO.
    5. Reinicia la matriz de piezas (índice 0, todas 'pendiente').
    6. Redibuja el fixture y actualiza los KPIs a 0.
    7. Mantiene Set 0 y Home válidos.
    """
    global estado_pintar, estado_stop, indice_matriz_actual, fixture_piezas_estado
    global paro_requiere_reset, paro_requiere_inicio
    try:
        # Transición tras paro: al presionar RESET, pasa a requerir INICIO
        if paro_requiere_reset:
            paro_requiere_reset = False
            paro_requiere_inicio = True
            lbl_estado.configure(
                text="✓ RESET completado tras paro. Ahora presione 'INICIO' para continuar el ciclo.",
                text_color=COLOR_OK
            )
            
        # 1. Pulso limpio de RESET al PLC (350 ms para garantizar lectura por el ciclo de CODESYS)
        if plc_conectado:
            nodos["RESET"].set_value(ua.Variant(True, ua.VariantType.Boolean))
            ventana.after(350, lambda: nodos["RESET"].set_value(ua.Variant(False, ua.VariantType.Boolean)))
        else:
            nodos["RESET"].set_value(True)
            ventana.after(350, lambda: nodos["RESET"].set_value(False))
            
        # 2. Desactivar PINTAR e INICIO
        estado_pintar = False
        try:
            if plc_conectado:
                nodos["PINTAR"].set_value(ua.Variant(False, ua.VariantType.Boolean))
                nodos["INICIO"].set_value(ua.Variant(False, ua.VariantType.Boolean))
            else:
                nodos["PINTAR"].set_value(False)
                nodos["INICIO"].set_value(False)
        except Exception:
            pass
            
        if 'btn_pintar' in globals():
            btn_pintar.configure(text="🎨\nINICIO DE\nPINTADO\n[OFF]", fg_color=COLOR_KOSTAL_NAVY, text_color="white", border_color="#94A3B8")
            
        # 3. Desactivar PARO si estaba activo
        estado_stop = False
        try:
            if plc_conectado:
                nodos["STOP"].set_value(ua.Variant(False, ua.VariantType.Boolean))
            else:
                nodos["STOP"].set_value(False)
        except Exception:
            pass
        if 'btn_stop' in globals():
            btn_stop.configure(text="🛑  PARO", fg_color=COLOR_NOK)
            
        # 4. Reiniciar estado de las piezas de la matriz
        indice_matriz_actual = 0
        fixture_piezas_estado = ["pendiente"] * matriz_total
        if 'canvas_fixture' in globals():
            dibujar_fixture_moderno()
        actualizar_kpis_fixture()
        if 'lbl_piezas_realizadas' in globals():
            lbl_piezas_realizadas.configure(text=f"00 / {matriz_total:02d}")
            
        # 5. Sincronizar posición actual en pantalla desde el PLC si está conectado
        try:
            if plc_conectado:
                px = limitar_coord(nodos["POS_X"].get_value() / 100.0)
                py = limitar_coord(nodos["POS_Y"].get_value() / 100.0)
                caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{px:.2f}")
                caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{py:.2f}")
                if 'lbl_jog_coord_x' in globals():
                    lbl_jog_coord_x.configure(text=f"{px:.2f}")
                if 'lbl_jog_coord_y' in globals():
                    lbl_jog_coord_y.configure(text=f"{py:.2f}")
        except Exception:
            pass
            
        lbl_estado.configure(text="✓ Pulso RESET enviado al PLC. Ciclo, alarmas y matriz reiniciados.", text_color=COLOR_OK)
        if '_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 5:
            notificar_accion_tutorial(5, delay_ms=900)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al ejecutar RESET: {e}", text_color=COLOR_NOK)

def guardar_posicion_como_home():
    global maquina_en_home
    try:
        val_x = limitar_coord_x(caja_pos_x.get())
        val_y = limitar_coord_y(caja_pos_y.get())
        caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{val_x:.2f}")
        caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{val_y:.2f}")
        valor_x_mm = val_x * 100
        valor_y_mm = val_y * 100
        if plc_conectado:
            nodos["HOME_X"].set_value(ua.Variant(int(valor_x_mm), ua.VariantType.Int32))
            nodos["HOME_Y"].set_value(ua.Variant(int(valor_y_mm), ua.VariantType.Int32))
        else:
            nodos["HOME_X"].set_value(int(valor_x_mm))
            nodos["HOME_Y"].set_value(int(valor_y_mm))
        maquina_en_home = True
        lbl_estado.configure(text=f"✓ Set Home: Origen fijado en X:{val_x:.2f} Y:{val_y:.2f}.", text_color=COLOR_OK)
    except ValueError:
        lbl_estado.configure(text="✗ Error numérico al guardar Set Home.", text_color=COLOR_NOK)

def guardar_posicion_como_cero():
    """Guarda la posición actual de la máquina como CERO permanente (X: 0 a 88 mm | Y: 0 a 200 mm)"""
    global cero_guardado_x, cero_guardado_y, cero_ha_sido_seteado
    try:
        if plc_conectado:
            try:
                val_x = limitar_coord_x(nodos["POS_X"].get_value() / 100.0)
                val_y = limitar_coord_y(nodos["POS_Y"].get_value() / 100.0)
            except Exception:
                val_x = limitar_coord_x(caja_pos_x.get())
                val_y = limitar_coord_y(caja_pos_y.get())
        else:
            val_x = limitar_coord_x(caja_pos_x.get())
            val_y = limitar_coord_y(caja_pos_y.get())
            
        cero_guardado_x = val_x
        cero_guardado_y = val_y
        caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{cero_guardado_x:.2f}")
        caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{cero_guardado_y:.2f}")
        
        config_actual["cero_guardado_x"] = cero_guardado_x
        config_actual["cero_guardado_y"] = cero_guardado_y
        
        # REQUERIMIENTO: Guardar el 0 en la receta activa como su CERO PREDETERMINADO
        rec_activa_nom = config_actual.get("receta_activa", "")
        for r in config_actual.get("recetas", []):
            if r.get("nombre") == rec_activa_nom:
                r["cero_guardado_x"] = cero_guardado_x
                r["cero_guardado_y"] = cero_guardado_y
                r["cero_calibrado"] = True
                break
                
        guardar_configuracion()
        
        actualizar_coordenadas_matriz()
        cero_ha_sido_seteado = True
        
        if 'lbl_indicador_set0' in globals() and lbl_indicador_set0:
            lbl_indicador_set0.configure(text=f"✓ CERO LISTO: X={cero_guardado_x:.2f}  Y={cero_guardado_y:.2f} mm", text_color="#065F46")
        if 'f_cero_ind' in globals() and f_cero_ind:
            f_cero_ind.configure(fg_color="#ECFDF5", border_color="#A7F3D0")
        if 'lbl_cero_jog_val' in globals() and lbl_cero_jog_val:
            lbl_cero_jog_val.configure(text=f"X: {cero_guardado_x:.2f} mm  |  Y: {cero_guardado_y:.2f} mm")
        lbl_estado.configure(text=f"✓ Set 0 calibrado y fijado como predeterminado para '{rec_activa_nom}' en X:{cero_guardado_x:.2f} Y:{cero_guardado_y:.2f} mm. Listo para pintar.", text_color=COLOR_OK)
        if '_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 2:
            notificar_accion_tutorial(2, delay_ms=800)
    except ValueError:
        lbl_estado.configure(text="✗ Error numérico al guardar Set 0.", text_color=COLOR_NOK)

def alternar_stop():
    global estado_stop, paro_requiere_reset, paro_requiere_inicio
    try:
        estado_stop = not estado_stop 
        if plc_conectado:
            nodos["STOP"].set_value(ua.Variant(estado_stop, ua.VariantType.Boolean))
        else:
            nodos["STOP"].set_value(estado_stop)
            
        if estado_stop:
            paro_requiere_reset = False
            paro_requiere_inicio = False
            btn_stop.configure(text="🛑 PARO ACTIVADO", fg_color="#991B1B")
            lbl_estado.configure(text="🛑 PARO ACTIVADO EN EL SISTEMA", text_color=COLOR_NOK)
        else:
            # REQUERIMIENTO: Al desactivar el stop, pedir presionar reset y luego inicio
            paro_requiere_reset = True
            paro_requiere_inicio = False
            btn_stop.configure(text="🛑 PARO", fg_color=COLOR_NOK)
            lbl_estado.configure(
                text="⚠️ PARO DESACTIVADO: Requiere presionar 'RESET' y después 'INICIO'.",
                text_color=COLOR_WARN
            )
            messagebox.showwarning(
                "Paro Desactivado",
                "⚠️ El PARO de emergencia ha sido desactivado.\n\n"
                "Para reanudar la operación:\n"
                "1. Presione el botón [RESET].\n"
                "2. Posteriormente presione el botón [INICIO]."
            )
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al alternar PARO: {e}", text_color=COLOR_NOK)

# ==============================================================================
# GESTIÓN SEGURA DE PASO JOG Y CICLOS DE PASADAS DE TAMPOGRAFÍA
# ==============================================================================
_btns_paso_presets = {}
btn_tampo_1 = None
btn_tampo_2 = None
lbl_pasada_badge = None

def obtener_paso_jog():
    """Obtiene el valor de paso JOG de forma segura y a prueba de fallos."""
    try:
        if 'caja_paso' in globals() and caja_paso:
            txt = str(caja_paso.get()).strip().replace(",", ".")
            if txt:
                v = float(txt)
                if 0.01 <= v <= 100.0:
                    config_actual["paso_jog"] = v
                    return v
    except Exception:
        pass
    return float(config_actual.get("paso_jog", 5.0))

def fijar_paso_jog(nuevo_paso):
    """Fija un nuevo paso JOG numérico, actualiza la caja, la configuración y el estilo de botones."""
    global config_actual
    try:
        val = max(0.01, min(200.0, float(nuevo_paso)))
        config_actual["paso_jog"] = val
        if 'caja_paso' in globals() and caja_paso:
            caja_paso.delete(0, tk.END)
            caja_paso.insert(0, f"{val:g}")
        guardar_configuracion()
        actualizar_botones_paso_preset(val)
        if 'lbl_estado' in globals() and lbl_estado:
            lbl_estado.configure(
                text=f"✓ Paso JOG configurado en {val:g} mm.",
                text_color=COLOR_OK
            )
    except Exception as err:
        print(f"Error fijando paso JOG: {err}")

def actualizar_botones_paso_preset(val_actual=None):
    if val_actual is None:
        val_actual = float(config_actual.get("paso_jog", 5.0))
    for p_val, btn in _btns_paso_presets.items():
        try:
            if abs(p_val - val_actual) < 0.01:
                btn.configure(
                    fg_color=COLOR_KOSTAL_NAVY,
                    text_color="#FFFFFF",
                    border_color=COLOR_KOSTAL_NAVY
                )
            else:
                btn.configure(
                    fg_color="#FFFFFF",
                    text_color="#334155",
                    border_color=BORDER_LIGHT
                )
        except Exception:
            pass

def paso_jog_siguiente(direccion=1):
    """Aumenta (+1) o reduce (-1) el paso entre los presets estándar [0.5, 1.0, 2.0, 5.0, 10.0]"""
    presets = [0.5, 1.0, 2.0, 5.0, 10.0]
    cur = obtener_paso_jog()
    if direccion > 0:
        sig = next((p for p in presets if p > cur + 0.01), min(100.0, cur + 5.0))
    else:
        sig = next((p for p in reversed(presets) if p < cur - 0.01), max(0.1, cur / 2.0))
    fijar_paso_jog(sig)

def abrir_teclado_numerico_paso():
    """Abre un teclado numérico táctil industrial moderno para ajustar el paso JOG sin trabas"""
    dlg = ctk.CTkToplevel(ventana)
    dlg.title("Ajustar Paso JOG")
    dlg.geometry("340x440")
    dlg.resizable(False, False)
    try:
        dlg.transient(ventana)
    except Exception:
        pass
    
    try:
        x = ventana.winfo_x() + (ventana.winfo_width() // 2) - 170
        y = ventana.winfo_y() + (ventana.winfo_height() // 2) - 220
        dlg.geometry(f"+{x}+{y}")
    except Exception:
        pass

    def _grab_dlg_paso():
        try:
            if dlg.winfo_exists():
                dlg.lift()
                dlg.grab_set()
        except Exception:
            pass
    try:
        dlg.after(80, _grab_dlg_paso)
    except Exception:
        pass
        
    f_num = ctk.CTkFrame(dlg, fg_color=BG_MAIN, corner_radius=12)
    f_num.pack(fill="both", expand=True, padx=12, pady=12)
    
    ctk.CTkLabel(
        f_num,
        text="AJUSTE DE PASO JOG (mm)",
        font=(FONT_MAIN, 13, "bold"),
        text_color=COLOR_KOSTAL_NAVY
    ).pack(pady=(10, 4))
    
    entry_display = ctk.CTkEntry(
        f_num,
        height=46,
        font=(FONT_MONO, 22, "bold"),
        justify="center",
        corner_radius=8,
        border_width=2,
        border_color=COLOR_KOSTAL_NAVY
    )
    entry_display._no_teclado_auto = True
    val_actual = str(config_actual.get("paso_jog", "5.0"))
    entry_display.insert(0, val_actual)
    entry_display.pack(fill="x", padx=14, pady=(4, 8))
    
    f_pres = ctk.CTkFrame(f_num, fg_color="transparent")
    f_pres.pack(fill="x", padx=10, pady=(0, 8))
    for p_val in [0.5, 1.0, 2.0, 5.0, 10.0]:
        ctk.CTkButton(
            f_pres,
            text=f"{p_val:g}",
            font=(FONT_MAIN, 10, "bold"),
            width=48,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#E2E8F0",
            text_color=COLOR_KOSTAL_NAVY,
            border_width=1,
            border_color=BORDER_LIGHT,
            corner_radius=6,
            command=lambda v=p_val: (entry_display.delete(0, tk.END), entry_display.insert(0, str(v)))
        ).pack(side="left", expand=True, padx=2)
        
    f_grid = ctk.CTkFrame(f_num, fg_color="transparent")
    f_grid.pack(padx=10, pady=2)
    
    def _add_caracter(char):
        cur = entry_display.get()
        if char == "." and "." in cur:
            return
        entry_display.insert(tk.END, char)
        
    def _borrar_uno():
        cur = entry_display.get()
        if cur:
            entry_display.delete(len(cur)-1, tk.END)
            
    def _limpiar():
        entry_display.delete(0, tk.END)
        
    layout = [
        ["7", "8", "9"],
        ["4", "5", "6"],
        ["1", "2", "3"],
        ["C", "0", "."],
    ]
    for r_idx, row in enumerate(layout):
        for c_idx, key in enumerate(row):
            if key == "C":
                cmd = _limpiar
                bg_k = "#FEE2E2"
                tx_k = "#DC2626"
            elif key == ".":
                cmd = lambda: _add_caracter(".")
                bg_k = "#FFFFFF"
                tx_k = "#1E293B"
            else:
                cmd = lambda k=key: _add_caracter(k)
                bg_k = "#FFFFFF"
                tx_k = "#1E293B"
                
            ctk.CTkButton(
                f_grid,
                text=key,
                font=(FONT_MAIN, 16, "bold"),
                width=80,
                height=40,
                fg_color=bg_k,
                hover_color="#F1F5F9",
                text_color=tx_k,
                border_width=1,
                border_color=BORDER_LIGHT,
                corner_radius=8,
                command=cmd
            ).grid(row=r_idx, column=c_idx, padx=4, pady=3)
            
    f_act = ctk.CTkFrame(f_num, fg_color="transparent")
    f_act.pack(fill="x", padx=12, pady=(10, 8))
    
    ctk.CTkButton(
        f_act,
        text="⌫ Borrar",
        font=(FONT_MAIN, 11, "bold"),
        height=36,
        fg_color="#F1F5F9",
        hover_color="#E2E8F0",
        text_color="#475569",
        corner_radius=8,
        command=_borrar_uno
    ).pack(side="left", fill="x", expand=True, padx=(0, 4))
    
    def _aplicar():
        txt = entry_display.get().strip().replace(",", ".")
        try:
            val = float(txt)
            if 0.01 <= val <= 200.0:
                fijar_paso_jog(val)
                dlg.destroy()
            else:
                messagebox.showwarning("Rango", "El paso debe estar entre 0.01 y 200 mm.", parent=dlg)
        except ValueError:
            messagebox.showwarning("Valor Inválido", "Por favor ingrese un número decimal válido para el paso (ej. 1.0, 5.0, 14.5).", parent=dlg)
            
    ctk.CTkButton(
        f_act,
        text="✓ Aceptar",
        font=(FONT_MAIN, 12, "bold"),
        height=36,
        fg_color=COLOR_KOSTAL_NAVY,
        hover_color=COLOR_KOSTAL_ACCENT,
        text_color="white",
        corner_radius=8,
        command=_aplicar
    ).pack(side="right", fill="x", expand=True, padx=(4, 0))

    entry_display.bind("<Return>", lambda e: (ocultar_teclado_nativo(), _aplicar(), "break")[2])
    entry_display.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), _aplicar(), "break")[2])
    dlg.bind("<Return>", lambda e: (ocultar_teclado_nativo(), _aplicar(), "break")[2])
    dlg.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), _aplicar(), "break")[2])

def fijar_pasadas_tampo(num_pasadas):
    """Configura si el ciclo de tampografiado realiza 1 pasada o 2 pasadas por coordenada/pieza."""
    global config_actual
    num = 2 if int(num_pasadas) == 2 else 1
    config_actual["pasadas_tampo"] = num
    guardar_configuracion()
    
    if 'btn_tampo_1' in globals() and btn_tampo_1 and 'btn_tampo_2' in globals() and btn_tampo_2:
        try:
            if num == 1:
                btn_tampo_1.configure(
                    fg_color=COLOR_KOSTAL_NAVY,
                    hover_color=COLOR_KOSTAL_ACCENT,
                    text_color="#FFFFFF",
                    border_color=COLOR_KOSTAL_NAVY
                )
                btn_tampo_2.configure(
                    fg_color="#FFFFFF",
                    hover_color="#F1F5F9",
                    text_color="#475569",
                    border_color=BORDER_LIGHT
                )
                if 'lbl_pasada_badge' in globals() and lbl_pasada_badge:
                    lbl_pasada_badge.configure(
                        text="1 PASADA",
                        fg_color="#EFF6FF",
                        text_color="#1E40AF"
                    )
            else:
                btn_tampo_2.configure(
                    fg_color=COLOR_KOSTAL_NAVY,
                    hover_color=COLOR_KOSTAL_ACCENT,
                    text_color="#FFFFFF",
                    border_color=COLOR_KOSTAL_NAVY
                )
                btn_tampo_1.configure(
                    fg_color="#FFFFFF",
                    hover_color="#F1F5F9",
                    text_color="#475569",
                    border_color=BORDER_LIGHT
                )
                if 'lbl_pasada_badge' in globals() and lbl_pasada_badge:
                    lbl_pasada_badge.configure(
                        text="2 PASADAS",
                        fg_color="#ECFDF5",
                        text_color="#065F46"
                    )
        except Exception:
            pass
            
    if 'lbl_estado' in globals() and lbl_estado:
        lbl_estado.configure(
            text=f"✓ Modo de tampografiado: {num} pasada(s) por coordenada/pieza.",
            text_color=COLOR_OK
        )

def avanzar_siguiente_pieza_matriz():
    """
    Lógica CODESYS / Pintado:
    Al activarse sensor / señal PINTADO del PLC:
    - Si pasadas_tampo == 2 y apenas se completó pasada 1:
      Repite la misma coordenada (segunda pasada de tampografiado en la misma pieza).
    - Si se completaron todas las pasadas (1 o 2):
      Marca la pieza actual como OK y se envía a la siguiente coordenada de la matriz.
    - Al completarse todas las piezas, marca el final de ciclo, retorna a Home y reinicia la matriz.
    """
    global indice_matriz_actual, estado_pintar, maquina_en_home, contador_ciclos_completados, pasada_actual_pieza
    global total_piezas_sesion_acum, piezas_ok_registradas
    try:
        num_pasadas = int(config_actual.get("pasadas_tampo", 2))
        
        # VERIFICACIÓN DE PASADAS POR PIEZA (1 o 2 Tampos)
        if num_pasadas > 1 and pasada_actual_pieza < num_pasadas:
            pasada_actual_pieza += 1
            # Repetir la misma coordenada para la pasada 2
            if 0 <= indice_matriz_actual < len(matriz_coordenadas):
                px, py = matriz_coordenadas[indice_matriz_actual]
            else:
                px, py = cero_guardado_x, cero_guardado_y
                
            caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{px:.2f}")
            caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{py:.2f}")
            pos_ok = enviar_datos(verificar_eco=True)
            
            # Paso 3: Disparar solo si el PLC confirmó haber recibido y memorizado las coordenadas
            if pos_ok or not plc_conectado:
                activar_nueva_posicion(duracion_ms=500)
                enviar_pulso("CRUZETA", duracion_ms=350)
            else:
                def _reintentar_disparo_p2():
                    if enviar_datos(verificar_eco=True) or not plc_conectado:
                        activar_nueva_posicion(duracion_ms=500)
                        enviar_pulso("CRUZETA", duracion_ms=350)
                ventana.after(60, _reintentar_disparo_p2)
            
            dibujar_fixture_moderno()
            actualizar_kpis_fixture()
            if 'actualizar_telemetria_plc' in globals():
                actualizar_telemetria_plc(px, py)
                
            lbl_estado.configure(
                text=f"🔁 Señal PINTADO recibida -> Repitiendo pasada {pasada_actual_pieza}/{num_pasadas} en Pieza {indice_matriz_actual + 1}/{len(matriz_coordenadas)}: X={px:.2f} Y={py:.2f} mm | NUEVA_POSICION activada ✓",
                text_color=COLOR_WARN
            )
            return

        # Pasadas completadas para esta pieza -> resetear contador de pasada y avanzar
        pasada_actual_pieza = 1

        # 1. Marcar pieza actual como procesada OK
        if indice_matriz_actual < len(fixture_piezas_estado):
            fixture_piezas_estado[indice_matriz_actual] = "ok"
            
        # 2. Incrementar índice de pieza
        indice_matriz_actual += 1
        
        # 3. Verificar si quedan más piezas en la matriz
        if indice_matriz_actual < len(matriz_coordenadas):
            if indice_matriz_actual < len(fixture_piezas_estado):
                fixture_piezas_estado[indice_matriz_actual] = "activa"
                
            sig_x, sig_y = matriz_coordenadas[indice_matriz_actual]
            caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{sig_x:.2f}")
            caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{sig_y:.2f}")
            pos_ok = enviar_datos(verificar_eco=True)
            
            # Paso 3: Disparar solo si el PLC confirmó haber recibido y memorizado las coordenadas
            if pos_ok or not plc_conectado:
                activar_nueva_posicion(duracion_ms=500)
                enviar_pulso("CRUZETA", duracion_ms=350)
            else:
                lbl_estado.configure(
                    text=f"⚠️ Señal PINTADO -> Pieza {indice_matriz_actual + 1}: Discrepancia temporal en PLC. Reintentando confirmación...",
                    text_color=COLOR_WARN
                )
                def _reintentar_disparo_sig():
                    if enviar_datos(verificar_eco=True) or not plc_conectado:
                        activar_nueva_posicion(duracion_ms=500)
                        enviar_pulso("CRUZETA", duracion_ms=350)
                ventana.after(60, _reintentar_disparo_sig)
            
            dibujar_fixture_moderno()
            actualizar_kpis_fixture()
            if 'actualizar_telemetria_plc' in globals():
                actualizar_telemetria_plc(sig_x, sig_y)
                
            rx_ok = ultimo_plc_rx_ok if 'ultimo_plc_rx_ok' in globals() else True
            if rx_ok:
                lbl_estado.configure(
                    text=f"🎯 Señal PINTADO recibida -> Avanzando a pieza {indice_matriz_actual + 1}/{len(matriz_coordenadas)} (Pasada 1/{num_pasadas}): X={sig_x:.2f} Y={sig_y:.2f} mm | NUEVA_POSICION activada ✓",
                    text_color=COLOR_OK
                )
            else:
                lbl_estado.configure(
                    text=f"⚠️ Señal PINTADO -> Pieza {indice_matriz_actual + 1}: Enviado X={sig_x:.2f} Y={sig_y:.2f} mm | NUEVA_POSICION activada | ⚠️ PLC reporta discrepancia.",
                    text_color=COLOR_WARN
                )
        else:
            # Fin del ciclo de la matriz completa:
            # Mostrar la última pieza completada en verde
            dibujar_fixture_moderno()
            actualizar_kpis_fixture()
            
            # Desactivar pintado
            estado_pintar = False
            if plc_conectado:
                nodos["PINTAR"].set_value(ua.Variant(False, ua.VariantType.Boolean))
            else:
                nodos["PINTAR"].set_value(False)
            btn_pintar.configure(text="🎨\nINICIO DE\nPINTADO\n[OFF]", fg_color=COLOR_KOSTAL_NAVY, text_color="white", border_color="#CBD5E1")
            
            # Incrementar y marcar final de un ciclo sumando las piezas por jig configuradas en la receta
            contador_ciclos_completados += 1
            pz_jig_ciclo = obtener_piezas_por_jig_actual()
            total_piezas_sesion_acum += pz_jig_ciclo
            if kpis_fin_hora_registrados:
                piezas_ok_registradas = max(0, total_piezas_sesion_acum - piezas_nok_registradas)
            actualizar_kpis_fixture()
            if 'lbl_ciclo_badge' in globals():
                lbl_ciclo_badge.configure(text=f"🏁 CICLOS: {contador_ciclos_completados}")
                
            lbl_estado.configure(
                text=f"🏁 FINAL DE UN CICLO #{contador_ciclos_completados}: +{pz_jig_ciclo} piezas sumadas (Total: {total_piezas_sesion_acum}). Retornando a Home y reiniciando matriz...",
                text_color=COLOR_OK
            )
            
            # Retornar cabezal a HOME de manera automática para el siguiente ciclo
            ejecutar_home_general()
            maquina_en_home = True
            
            # Reiniciar la matriz para el siguiente ciclo (manteniendo Set 0 y Home intactos)
            def _reiniciar_matriz_post_ciclo():
                global indice_matriz_actual, fixture_piezas_estado, pasada_actual_pieza
                indice_matriz_actual = 0
                pasada_actual_pieza = 1
                fixture_piezas_estado = ["pendiente"] * matriz_total
                dibujar_fixture_moderno()
                actualizar_kpis_fixture()
                lbl_estado.configure(
                    text=f"🏁 FINAL DE CICLO #{contador_ciclos_completados} COMPLETADO: Matriz reiniciada (00/{matriz_total:02d}). Lista para siguiente ciclo. Set 0 y Home vigentes.",
                    text_color=COLOR_OK
                )
            ventana.after(1200, _reiniciar_matriz_post_ciclo)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al avanzar matriz: {e}", text_color=COLOR_NOK)

def alternar_pintar():
    """
    Alterna 'Inicio de pintado':
    REQUISITO PREVIO OBLIGATORIO:
    1. Haber realizado y calibrado SET 0 (cero_ha_sido_seteado)
    Al cumplirse, viaja a SET 0 y comienza la secuencia.
    Cada activación de PLC (variable PINTADO) avanzará a la siguiente coordenada de la matriz.
    """
    global estado_pintar, indice_matriz_actual, maquina_en_home
    try:
        if not estado_pintar:
            if estado_stop:
                messagebox.showwarning("Paro Activo", "El PARO de emergencia está activado.")
                return
            if 'paro_requiere_reset' in globals() and paro_requiere_reset:
                messagebox.showwarning("Secuencia Requerida Tras Paro", "El PARO fue desactivado. Debe presionar primero [RESET] y después [INICIO].")
                return
            if 'paro_requiere_inicio' in globals() and paro_requiere_inicio:
                messagebox.showwarning("Secuencia Requerida Tras Paro", "Debe presionar [INICIO] tras el RESET para reanudar.")
                return
                
            # 1. VERIFICAR QUE SE HAYA CALIBRADO EL SET 0
            if not cero_ha_sido_seteado:
                lbl_estado.configure(
                    text="⚠️ ACCIÓN REQUERIDA: Debe calibrar el 'Set 0' con la cruceta JOG antes de poder iniciar el pintado.",
                    text_color=COLOR_WARN
                )
                messagebox.showwarning(
                    "Calibración de Set 0 Requerida",
                    "⚠️ No se puede iniciar el pintado sin calibrar el Set 0.\n\n"
                    "1. Posicione la máquina con la Cruceta JOG en el origen de pintado (Pieza 01).\n"
                    "2. Presione el botón '↩ Set 0' en el panel de control.\n"
                    "3. Posteriormente presione '🎨 INICIO DE PINTADO'."
                )
                return
                
            # Requisito cumplido -> Iniciar pintado
            global pasada_actual_pieza
            estado_pintar = True
            maquina_en_home = False
            pasada_actual_pieza = 1
            num_p = int(config_actual.get("pasadas_tampo", 2))
            btn_pintar.configure(text="🎨\nPINTANDO\nACTIVO\n[ON]", fg_color=COLOR_WARN, text_color="#1E293B", border_color="#B45309")
            
            # REQUERIMIENTO: Si la receta ya se corrió una vez, se queda ajustado el 0 como su predeterminado
            rec_activa_nom = config_actual.get("receta_activa", "")
            for r in config_actual.get("recetas", []):
                if r.get("nombre") == rec_activa_nom:
                    r["corridas"] = r.get("corridas", 0) + 1
                    r["cero_calibrado"] = True
                    r["cero_guardado_x"] = cero_guardado_x
                    r["cero_guardado_y"] = cero_guardado_y
                    break
            guardar_configuracion()
            
            # Requisito: Cerrar automáticamente el modo manual al activar pintar
            global estado_manual
            if estado_manual:
                estado_manual = False
                if plc_conectado:
                    try:
                        nodos["MANUAL"].set_value(ua.Variant(False, ua.VariantType.Boolean))
                    except Exception:
                        pass
                if 'btn_manual' in globals():
                    btn_manual.configure(text="⚙ MODO: AUTO", fg_color=COLOR_KOSTAL_NAVY, text_color="white")
                lbl_estado.configure(text="ℹ MODO MANUAL cerrado automáticamente para producción.", text_color=COLOR_KOSTAL_ACCENT)
            
            # Asegurar coordenadas generadas
            actualizar_coordenadas_matriz()
            indice_matriz_actual = 0
            
            # Resetear fixture a pendiente y pieza 0 a activa
            fixture_piezas_estado.clear()
            for i in range(matriz_total):
                fixture_piezas_estado.append("activa" if i == 0 else "pendiente")
            dibujar_fixture_moderno()
            actualizar_kpis_fixture()
            
            # Al activar inicio de pintado, viajar a la posición establecida como seteo en 0
            caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{cero_guardado_x:.2f}")
            caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{cero_guardado_y:.2f}")
            pos_ok = enviar_datos(verificar_eco=True)
            if pos_ok or not plc_conectado:
                activar_nueva_posicion(duracion_ms=500)
                enviar_pulso("CRUZETA", duracion_ms=350)
            else:
                def _reintentar_inicio_pintar():
                    if enviar_datos(verificar_eco=True) or not plc_conectado:
                        activar_nueva_posicion(duracion_ms=500)
                        enviar_pulso("CRUZETA", duracion_ms=350)
                ventana.after(60, _reintentar_inicio_pintar)
            
            # Activar variable PINTAR en el PLC
            if plc_conectado:
                nodos["PINTAR"].set_value(ua.Variant(True, ua.VariantType.Boolean))
            else:
                nodos["PINTAR"].set_value(True)
                
            if 'actualizar_telemetria_plc' in globals():
                actualizar_telemetria_plc(cero_guardado_x, cero_guardado_y)
                
            lbl_estado.configure(
                text=f"🎨 INICIO DE PINTADO ACTIVADO: Posicionado en SET 0 (X:{cero_guardado_x:.2f} Y:{cero_guardado_y:.2f} mm) | Pieza 1/{matriz_total} (Pasada 1/{num_p}). Esperando señal PINTADO del PLC...",
                text_color=COLOR_OK
            )
            if '_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 4:
                notificar_accion_tutorial(4, delay_ms=850)
        else:
            estado_pintar = False
            btn_pintar.configure(text="🎨\nINICIO DE\nPINTADO\n[OFF]", fg_color=COLOR_KOSTAL_NAVY, text_color="white", border_color="#94A3B8")
            if plc_conectado:
                nodos["PINTAR"].set_value(ua.Variant(False, ua.VariantType.Boolean))
            else:
                nodos["PINTAR"].set_value(False)
            lbl_estado.configure(text="Inicio de pintado Desactivado.", text_color=TEXT_MUTED)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error en PINTAR: {e}", text_color=COLOR_NOK)

def alternar_manual():
    global estado_manual
    try:
        estado_manual = not estado_manual 
        if plc_conectado:
            nodos["MANUAL"].set_value(ua.Variant(estado_manual, ua.VariantType.Boolean))
        else:
            nodos["MANUAL"].set_value(estado_manual)
        
        if estado_manual:
            btn_manual.configure(text="⚡ MODO: MANUAL", fg_color=COLOR_WARN, text_color="black")
            pos_x_actual = limitar_coord(nodos["POS_X"].get_value() / 100.0)
            pos_y_actual = limitar_coord(nodos["POS_Y"].get_value() / 100.0)
            caja_pos_x.delete(0, tk.END)
            caja_pos_x.insert(0, f"{pos_x_actual:.2f}")
            caja_pos_y.delete(0, tk.END)
            caja_pos_y.insert(0, f"{pos_y_actual:.2f}")
            lbl_estado.configure(text="✓ MODO MANUAL ACTIVADO (Cruzeta Habilitada).", text_color=COLOR_OK)
        else:
            btn_manual.configure(text="⚙ MODO: AUTO", fg_color=COLOR_KOSTAL_NAVY, text_color="white")
            lbl_estado.configure(text="Modo Auto activado. Cruzeta deshabilitada.", text_color=COLOR_KOSTAL_NAVY)

        # Si el tutorial interactivo está en el Paso 2, refrescar la guía e iluminar cruceta y Set 0
        if '_tutorial_activo' in globals() and _tutorial_activo and _paso_tutorial_actual == 2:
            if '_fn_mostrar_paso' in globals() and _fn_mostrar_paso:
                _fn_mostrar_paso()
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error en modo: {e}", text_color=COLOR_NOK)

def enviar_datos(verificar_eco=True):
    """
    Patrón Read-Back Handshake (Escribir, Leer y Confirmar):
    Paso 1 (Escribir): Envía las coordenadas en centésimas de mm (DINT) a POS_X y POS_Y del PLC.
    Paso 2 (Leer/Eco): Inmediatamente lee de vuelta los registros del PLC para verificar que
                       recibió exactamente el mismo número. Si hay fallo o discrepancia, reintenta
                       la escritura para evitar que un retraso de red sea interpretado como 0.
    Paso 3 (Confirmar): Retorna True solo si el PLC memorizó exitosamente la posición.
    """
    try:
        val_x = limitar_coord_x(caja_pos_x.get())
        val_y = limitar_coord_y(caja_pos_y.get())
        
        caja_pos_x.delete(0, tk.END)
        caja_pos_x.insert(0, f"{val_x:.2f}")
        caja_pos_y.delete(0, tk.END)
        caja_pos_y.insert(0, f"{val_y:.2f}")
        
        target_px = int(round(float(val_x) * 100))
        target_py = int(round(float(val_y) * 100))
        try:
            velocidad_general = int(float(caja_vel.get())) if ('caja_vel' in globals() and caja_vel.get().strip()) else 100
        except Exception:
            velocidad_general = 100
            
        confirmado = False
        max_reintentos = 3 if (plc_conectado and verificar_eco) else 1
        
        for intento in range(max_reintentos):
            if plc_conectado:
                try:
                    # 1. Escribir
                    nodos["POS_X"].set_value(ua.Variant(target_px, ua.VariantType.Int32))
                    nodos["POS_Y"].set_value(ua.Variant(target_py, ua.VariantType.Int32))
                    nodos["VELOCIDAD_X"].set_value(ua.Variant(velocidad_general, ua.VariantType.UInt16))
                    nodos["VELOCIDAD_Y"].set_value(ua.Variant(velocidad_general, ua.VariantType.UInt16))
                except Exception as e_w:
                    print(f"Error escritura PLC (Intento {intento+1}): {e_w}")
                    time.sleep(0.015)
                    continue

                if not verificar_eco:
                    confirmado = True
                    break

                # 2. Leer (Eco) para verificar que el PLC memorizó exactamente los valores
                try:
                    time.sleep(0.012) # Pausa mínima para ciclo de scan del PLC
                    eco_x = nodos["POS_X"].get_value()
                    eco_y = nodos["POS_Y"].get_value()
                    if eco_x == target_px and eco_y == target_py:
                        confirmado = True
                        break
                    else:
                        print(f"⚠️ Discrepancia en eco PLC ({intento+1}/{max_reintentos}): Esperado=(X:{target_px}, Y:{target_py}) vs Leído=(X:{eco_x}, Y:{eco_y}). Reintentando...")
                        time.sleep(0.02)
                except Exception as e_r:
                    print(f"Error lectura eco PLC (Intento {intento+1}): {e_r}")
                    time.sleep(0.015)
            else:
                nodos["POS_X"].set_value(target_px)
                nodos["POS_Y"].set_value(target_py)
                nodos["VELOCIDAD_X"].set_value(velocidad_general)
                nodos["VELOCIDAD_Y"].set_value(velocidad_general)
                confirmado = True
                break
        
        if 'lbl_jog_coord_x' in globals():
            lbl_jog_coord_x.configure(text=f"{val_x:.2f}")
        if 'lbl_jog_coord_y' in globals():
            lbl_jog_coord_y.configure(text=f"{val_y:.2f}")
            
        if 'actualizar_telemetria_plc' in globals():
            actualizar_telemetria_plc(val_x, val_y)
            
        return confirmado
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al enviar datos: {e}", text_color=COLOR_NOK)
        return False

def mover_cruzeta(eje, direccion):
    """
    Mueve el eje por cruzeta:
    Toma la ubicación donde ya estaba ubicada la máquina y le suma (paso * dir_efectiva),
    evitando saltos abruptos. Limita X hasta 88.0 mm e Y hasta 200.0 mm.
    REQUERIMIENTO: Sentido invertido para el eje X y el eje Y.
    """
    global maquina_en_home
    if not estado_manual:
        lbl_estado.configure(text="⚠️ ATENCIÓN: Activa el MODO MANUAL primero.", text_color=COLOR_WARN)
        return
        
    if estado_stop:
        lbl_estado.configure(text="🛑 PARO ACTIVO: No se puede mover los ejes.", text_color=COLOR_NOK)
        return
        
    if 'paro_requiere_reset' in globals() and paro_requiere_reset:
        lbl_estado.configure(text="⚠️ Paro liberado: Debe presionar primero 'RESET' y después 'INICIO'.", text_color=COLOR_WARN)
        return
        
    try:
        paso = obtener_paso_jog()
        
        # Leer la ubicación real donde la máquina YA ESTÁ UBICADA
        if plc_conectado:
            try:
                ubicacion_x_actual = limitar_coord_x(nodos["POS_X"].get_value() / 100.0)
                ubicacion_y_actual = limitar_coord_y(nodos["POS_Y"].get_value() / 100.0)
            except Exception:
                ubicacion_x_actual = limitar_coord_x(float(caja_pos_x.get()))
                ubicacion_y_actual = limitar_coord_y(float(caja_pos_y.get()))
        else:
            ubicacion_x_actual = limitar_coord_x(float(caja_pos_x.get()))
            ubicacion_y_actual = limitar_coord_y(float(caja_pos_y.get()))
            
        # REQUERIMIENTO: Invertir el sentido que dan las crucetas para el eje X y eje Y
        dir_efectiva = -direccion
            
        if eje == 'X':
            # Sumar el paso de la cruzeta al valor donde YA ESTABA ubicado (limitado a 88.0 mm máx)
            nuevo_x = limitar_coord_x(ubicacion_x_actual + (paso * dir_efectiva))
            nuevo_y = ubicacion_y_actual
        elif eje == 'Y':
            nuevo_x = ubicacion_x_actual
            # Sumar el paso de la cruzeta al valor donde YA ESTABA ubicado (limitado a 200.0 mm máx)
            nuevo_y = limitar_coord_y(ubicacion_y_actual + (paso * dir_efectiva))
        else:
            return
            
        # Sincronizar cajas internas y displays con la nueva posición calculada
        caja_pos_x.delete(0, tk.END)
        caja_pos_x.insert(0, f"{nuevo_x:.2f}")
        caja_pos_y.delete(0, tk.END)
        caja_pos_y.insert(0, f"{nuevo_y:.2f}")
        
        if 'lbl_jog_coord_x' in globals():
            lbl_jog_coord_x.configure(text=f"{nuevo_x:.2f}")
        if 'lbl_jog_coord_y' in globals():
            lbl_jog_coord_y.configure(text=f"{nuevo_y:.2f}")
            
        maquina_en_home = False # Al mover manualmente, sale de HOME
            
        pos_ok = enviar_datos(verificar_eco=True)
        if pos_ok or not plc_conectado:
            enviar_pulso("CRUZETA")
            lbl_estado.configure(text=f"✓ Cruzeta {eje} movida {paso*direccion:+.2f} mm | Posición: X:{nuevo_x:.2f} Y:{nuevo_y:.2f}", text_color=COLOR_OK)
        else:
            lbl_estado.configure(text=f"⚠️ Reintentando confirmación de posición con PLC...", text_color=COLOR_WARN)
    except Exception as e:
        lbl_estado.configure(text=f"✗ Error al mover cruzeta: {e}", text_color=COLOR_NOK)

def leer_nodo(nombre_variable, default=False):
    """Lee de forma segura el valor de un nodo OPC UA sin tumbar el ciclo de monitoreo"""
    try:
        if nombre_variable in nodos:
            return nodos[nombre_variable].get_value()
    except Exception:
        pass
    return default

def actualizar_luz(label_widget, valor, texto_true, texto_false, color_true, color_false="#F8FAFC", text_color_true="white", text_color_false="#334155"):
    """Actualiza las alarmas e indicadores con iluminación y colores fieles al código original"""
    val_bool = bool(valor)
    if getattr(label_widget, "_ultimo_estado_luz", None) == val_bool:
        return
    label_widget._ultimo_estado_luz = val_bool
    if val_bool:
        label_widget.configure(text=texto_true, fg_color=color_true, text_color=text_color_true)
    else:
        label_widget.configure(text=texto_false, fg_color=color_false, text_color=text_color_false)

def actualizar_telemetria_plc(dest_x=None, dest_y=None):
    """
    Lee en tiempo real los registros del PLC (POS_X, POS_Y) y compara con la coordenada de destino
    para verificar fehacientemente si el PLC la recibió.
    También diagnostica interlocks de movimiento (MANUAL, LISTO, M_ENE, STOP, ALARMAS).
    """
    global ultimo_plc_rx_x, ultimo_plc_rx_y, ultimo_plc_rx_ok
    try:
        if dest_x is None:
            dest_x = float(caja_pos_x.get())
        if dest_y is None:
            dest_y = float(caja_pos_y.get())
            
        num_pz = (indice_matriz_actual + 1) if ('indice_matriz_actual' in globals() and 0 <= indice_matriz_actual < len(matriz_coordenadas)) else 1
        
        if plc_conectado:
            rx_px = nodos["POS_X"].get_value()
            rx_py = nodos["POS_Y"].get_value()
            plc_x = round(rx_px / 100.0, 2)
            plc_y = round(rx_py / 100.0, 2)
            
            diff_x = abs(dest_x - plc_x)
            diff_y = abs(dest_y - plc_y)
            coincide = (diff_x <= 0.05) and (diff_y <= 0.05)
            
            ultimo_plc_rx_x = plc_x
            ultimo_plc_rx_y = plc_y
            ultimo_plc_rx_ok = coincide
            
            # Revisión de interlocks del PLC
            is_manual = bool(leer_nodo("MANUAL", default=False))
            is_listo = bool(leer_nodo("LISTO", default=False))
            is_ene = bool(leer_nodo("M_ENE", default=False))
            is_set = bool(leer_nodo("M_SET", default=False))
            is_stop = bool(leer_nodo("STOP", default=False)) or bool(leer_nodo("PE", default=False))
            alarm_x = bool(leer_nodo("ALARM_X", default=False))
            alarm_y = bool(leer_nodo("ALARM_Y", default=False))
            
            if 'lbl_tel_destino' in globals():
                lbl_tel_destino.configure(
                    text=f"🎯 DESTINO P{num_pz:02d}: X={dest_x:.2f} mm | Y={dest_y:.2f} mm"
                )
            if 'lbl_tel_plc_rx' in globals():
                lbl_tel_plc_rx.configure(
                    text=f"📡 PLC RECIBIÓ: POS_X={plc_x:.2f} mm | POS_Y={plc_y:.2f} mm"
                )
            if 'lbl_tel_status' in globals():
                if coincide:
                    lbl_tel_status.configure(
                        text="🟢 PLC RECIBIÓ COORDENADAS (COINCIDE ✓)",
                        fg_color=COLOR_OK_SOFT,
                        text_color=COLOR_OK_TEXT
                    )
                else:
                    lbl_tel_status.configure(
                        text=f"⚠️ DISCREPANCIA (ΔX={diff_x:.1f}, ΔY={diff_y:.1f} mm)",
                        fg_color=COLOR_WARN_SOFT,
                        text_color=COLOR_WARN_TEXT
                    )
            if 'lbl_tel_interlock' in globals():
                motivos_bloqueo = []
                if is_stop:
                    motivos_bloqueo.append("🛑 PARO ACTIVO")
                if not is_ene:
                    motivos_bloqueo.append("⚡ SIN ENERGÍA (M_ENE OFF)")
                if not is_set:
                    motivos_bloqueo.append("⚠️ FALTA HOME (M_SET OFF)")
                if not is_listo:
                    motivos_bloqueo.append("⏳ PLC NO LISTO")
                if alarm_x:
                    motivos_bloqueo.append("🚨 ALARMA EJE X")
                if alarm_y:
                    motivos_bloqueo.append("🚨 ALARMA EJE Y")
                if not is_manual:
                    motivos_bloqueo.append("⚙ MODO AUTO (Cruzeta bloqueada por PLC)")
                    
                if motivos_bloqueo:
                    txt_itlk = " | ".join(motivos_bloqueo[:3])
                    if len(motivos_bloqueo) > 3:
                        txt_itlk += f" (+{len(motivos_bloqueo)-3})"
                    c_itlk = "#DC2626"
                else:
                    txt_itlk = "✓ Interlocks OK: Energizado, Listo, Modo Cruzeta Habilitado"
                    c_itlk = "#059669"
                lbl_tel_interlock.configure(text=txt_itlk, text_color=c_itlk)
        else:
            ultimo_plc_rx_x = dest_x
            ultimo_plc_rx_y = dest_y
            ultimo_plc_rx_ok = True
            if 'lbl_tel_destino' in globals():
                lbl_tel_destino.configure(
                    text=f"🎯 DESTINO P{num_pz:02d}: X={dest_x:.2f} mm | Y={dest_y:.2f} mm"
                )
            if 'lbl_tel_plc_rx' in globals():
                lbl_tel_plc_rx.configure(
                    text=f"📡 SIMULADO: X={dest_x:.2f} mm | Y={dest_y:.2f} mm"
                )
            if 'lbl_tel_status' in globals():
                lbl_tel_status.configure(
                    text="🔵 MODO SIMULACIÓN (Sin PLC físico)",
                    fg_color="#EFF6FF",
                    text_color="#1E40AF"
                )
            if 'lbl_tel_interlock' in globals():
                lbl_tel_interlock.configure(
                    text="Interlocks: Modo Simulación Activo",
                    text_color=TEXT_MUTED
                )
    except Exception as e:
        if 'lbl_tel_status' in globals():
            lbl_tel_status.configure(
                text=f"✗ Error Telemetría: {e}",
                fg_color=COLOR_NOK_SOFT,
                text_color=COLOR_NOK_TEXT
            )

def probar_enviar_pieza_especifica(idx):
    """Permite probar y enviar individualmente cualquier pieza de la matriz directamente al PLC (solo en modo manual)"""
    global indice_matriz_actual
    if idx < 0 or idx >= len(matriz_coordenadas):
        return
    if not estado_manual:
        lbl_estado.configure(
            text="⚠️ MODO MANUAL REQUERIDO: Active el 'MODO MANUAL' para enviar piezas al PLC.",
            text_color=COLOR_WARN
        )
        return
    indice_matriz_actual = idx
    for k in range(len(fixture_piezas_estado)):
        if k < idx:
            fixture_piezas_estado[k] = "ok"
        elif k == idx:
            fixture_piezas_estado[k] = "activa"
        else:
            fixture_piezas_estado[k] = "pendiente"
            
    px, py = matriz_coordenadas[idx]
    caja_pos_x.delete(0, tk.END); caja_pos_x.insert(0, f"{px:.2f}")
    caja_pos_y.delete(0, tk.END); caja_pos_y.insert(0, f"{py:.2f}")
    pos_ok = enviar_datos(verificar_eco=True)
    if pos_ok or not plc_conectado:
        activar_nueva_posicion(duracion_ms=500)
        enviar_pulso("CRUZETA", duracion_ms=350)
    else:
        def _reintentar_prueba_pieza():
            if enviar_datos(verificar_eco=True) or not plc_conectado:
                activar_nueva_posicion(duracion_ms=500)
                enviar_pulso("CRUZETA", duracion_ms=350)
        ventana.after(60, _reintentar_prueba_pieza)
    
    dibujar_fixture_moderno()
    actualizar_kpis_fixture()
    actualizar_telemetria_plc(px, py)
    
    lbl_estado.configure(
        text=f"🧪 Prueba Pieza {idx+1}/{matriz_total}: Enviando X={px:.2f} Y={py:.2f} mm a PLC... Pulso CRUZETA emitido.",
        text_color=COLOR_OK
    )

_contador_ciclo_telemetria = 0
def monitoreo_ciclico():
    global ultimo_estado_pintado, ultimo_estado_sensor_y, _contador_ciclo_telemetria
    try:
        if plc_conectado:
            # OPTIMIZACIÓN INDUSTRIAL: Leer todos los nodos de estado en un solo paquete TCP (10-15 ms)
            # Evita retrasos de red que causaban pérdida aleatoria del pulso de PINTADO
            try:
                nodos_mon = [
                    nodos["PINTADO"], nodos["M_ENE"], nodos["M_SET"], nodos["LISTO"],
                    nodos["NO_LISTO"], nodos["ALARM_X"], nodos["ALARM_Y"], nodos["PE"],
                    nodos["SENSOR_X"], nodos["SENSOR_Y"], nodos["TAMPO"]
                ]
                vals = cliente.get_values(nodos_mon)
                pintado_actual = bool(vals[0])
                m_ene_val      = vals[1]
                m_set_val      = vals[2]
                listo_val      = vals[3]
                no_listo_val   = vals[4]
                alarm_x_val    = vals[5]
                alarm_y_val    = vals[6]
                pe_val         = vals[7]
                sensor_x_val   = bool(vals[8])
                sensor_y_val   = bool(vals[9])
                tampo_val      = vals[10]
            except Exception:
                pintado_actual = bool(leer_nodo("PINTADO", default=False))
                m_ene_val      = leer_nodo("M_ENE")
                m_set_val      = leer_nodo("M_SET")
                listo_val      = leer_nodo("LISTO")
                no_listo_val   = leer_nodo("NO_LISTO")
                alarm_x_val    = leer_nodo("ALARM_X")
                alarm_y_val    = leer_nodo("ALARM_Y")
                pe_val         = leer_nodo("PE")
                sensor_x_val   = bool(leer_nodo("SENSOR_X", default=False))
                sensor_y_val   = bool(leer_nodo("SENSOR_Y", default=False))
                tampo_val      = leer_nodo("TAMPO")
        else:
            pintado_actual = bool(nodos["PINTADO"].get_value())
            m_ene_val      = nodos["M_ENE"].get_value()
            m_set_val      = nodos["M_SET"].get_value()
            listo_val      = nodos["LISTO"].get_value()
            no_listo_val   = nodos["NO_LISTO"].get_value()
            alarm_x_val    = nodos["ALARM_X"].get_value()
            alarm_y_val    = nodos["ALARM_Y"].get_value()
            pe_val         = nodos["PE"].get_value()
            sensor_x_val   = bool(nodos["SENSOR_X"].get_value())
            sensor_y_val   = bool(nodos["SENSOR_Y"].get_value())
            tampo_val      = nodos["TAMPO"].get_value()

        # Actualizar indicadores visuales
        actualizar_luz(lbl_pintado, pintado_actual, "TINTA APLICADA", "ESPERANDO PINTADO...", COLOR_OK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_m_ene, m_ene_val, "ENERGIZADOS", "SIN ENERGÍA", COLOR_OK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_m_set, m_set_val, "ORÍGENES LISTOS", "FALTA HOME", COLOR_OK, COLOR_WARN, "white", "black")
        actualizar_luz(lbl_listo, listo_val, "ESTADO: LISTO", "ESPERANDO", COLOR_OK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_no_listo, no_listo_val, "ESTADO: NO LISTO", "---", COLOR_NOK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_alarm_x, alarm_x_val, "ALARMA X!", "X: Normal", COLOR_NOK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_alarm_y, alarm_y_val, "ALARMA Y!", "Y: Normal", COLOR_NOK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_pe, pe_val, "PE: ACTIVADO", "PE: OK", COLOR_NOK, "#F8FAFC", "white", "#334155")
        actualizar_luz(lbl_sensor_x, sensor_x_val, "SENSOR X: ON", "SENSOR X: OFF", COLOR_WARN, "#F8FAFC", "black", "#334155")
        actualizar_luz(lbl_sensor_y, sensor_y_val, "SENSOR Y: ON", "SENSOR Y: OFF", COLOR_WARN, "#F8FAFC", "black", "#334155")
        actualizar_luz(lbl_tampo, tampo_val, "TAMPOGRAFIADO: ACTIVO", "TAMPOGRAFIADO: INACTIVO", COLOR_KOSTAL_ACCENT, "#F8FAFC", "white", "#334155")

        # DETECCIÓN DE FLANCO: Cuando PINTADO se activa, avanza a la siguiente coordenada
        if pintado_actual and not ultimo_estado_pintado and not estado_stop:
            avanzar_siguiente_pieza_matriz()
            # Handshake de confirmación en el PLC si corresponde
            if plc_conectado:
                try:
                    nodos["PINTADO"].set_value(ua.Variant(False, ua.VariantType.Boolean))
                except Exception:
                    pass
        # En modo simulación Windows: avance automático opcional para pruebas
        if MODO_SIM_WINDOWS and _auto_sim_activo and estado_pintar and not estado_stop:
            global _ticks_auto_sim
            _ticks_auto_sim = _ticks_auto_sim + 1
            if _ticks_auto_sim >= 18:  # ~1.4 segundos por pieza
                _ticks_auto_sim = 0
                avanzar_siguiente_pieza_matriz()

        ultimo_estado_pintado = pintado_actual
        ultimo_estado_sensor_y = sensor_y_val
        
        # Telemetría cada ~400ms (5 ciclos)
        _contador_ciclo_telemetria = (_contador_ciclo_telemetria + 1) % 5
        if _contador_ciclo_telemetria == 0:
            actualizar_telemetria_plc()
    except Exception:
        pass
    ventana.after(80, monitoreo_ciclico)

_auto_sim_activo = False
_ticks_auto_sim = 0

def alternar_auto_sim():
    global _auto_sim_activo, _ticks_auto_sim
    _auto_sim_activo = not _auto_sim_activo
    _ticks_auto_sim = 0
    if _auto_sim_activo:
        if 'btn_sim_auto' in globals():
            btn_sim_auto.configure(text="⏸ Detener Auto-Sim", fg_color="#DC2626", hover_color="#B91C1C")
        lbl_estado.configure(text="▶ Auto-Simulador activado: Avanzando piezas automáticamente cada ~1.4s.", text_color=COLOR_OK)
    else:
        if 'btn_sim_auto' in globals():
            btn_sim_auto.configure(text="▶ Auto-Sim: OFF", fg_color="#0284C7", hover_color="#0369A1")
        lbl_estado.configure(text="⏸ Auto-Simulador pausado.", text_color=TEXT_MUTED)

def forzar_disparo_simulado():
    if not estado_pintar:
        lbl_estado.configure(text="⚠️ Presione primero 'INICIO DE PINTADO' para iniciar el ciclo.", text_color=COLOR_WARN)
        return
    avanzar_siguiente_pieza_matriz()
    lbl_estado.configure(text="⚡ Disparo manual simulado del PLC ejecutado con éxito.", text_color=COLOR_OK)

def cerrar_aplicacion():
    # 1. Ocultar y cerrar teclado en pantalla inmediatamente
    try:
        ocultar_teclado_nativo(forzar=True)
    except Exception:
        pass
    # 2. Desconectar PLC en hilo secundario daemon para evitar demoras de red
    try:
        if plc_conectado:
            import threading
            threading.Thread(target=cliente.disconnect, daemon=True).start()
    except Exception:
        pass
    # 3. Destruir ventana principal
    try:
        ventana.destroy()
    except Exception:
        pass
    # 4. Salida inmediata del proceso sin esperas
    import os
    os._exit(0)

def minimizar_ventana():
    """Minimiza la ventana asegurando compatibilidad con Linux/X11 y pantalla completa"""
    try:
        if ventana.attributes("-fullscreen"):
            ventana.attributes("-fullscreen", False)
        ventana.iconify()
    except Exception as e:
        print(f"Error al minimizar: {e}")

def alternar_pantalla_completa():
    """Alterna entre pantalla completa y modo ventana normal"""
    try:
        actual = bool(ventana.attributes("-fullscreen"))
        ventana.attributes("-fullscreen", not actual)
    except Exception as e:
        print(f"Error alternando pantalla completa: {e}")

# ==============================================================================
# 4. LÓGICA DE LOGIN Y DISPARO CONDICIONAL DE INICIO
# ==============================================================================
def buscar_recurso(nombre_archivo):
    if not nombre_archivo:
        return None
    if os.path.exists(nombre_archivo):
        return nombre_archivo
    base_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    archivo_base = os.path.basename(nombre_archivo)
    posibles_rutas = [
        os.path.join(base_dir, "Assets", archivo_base),
        os.path.join(base_dir, archivo_base),
        os.path.join(base_dir, "Assets", nombre_archivo),
        os.path.join(base_dir, nombre_archivo)
    ]
    for r in posibles_rutas:
        if os.path.exists(r):
            return r
    return None

def cargar_imagen(nombre_archivo, max_ancho, max_alto):
    ruta = buscar_recurso(nombre_archivo)
    if not ruta:
        return None
    try:
        img = Image.open(ruta)
        img.thumbnail((max_ancho, max_alto), Image.Resampling.LANCZOS)
        return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
    except Exception as e:
        print(f"No se pudo cargar {nombre_archivo}: {e}")
        return None

def alternar_ver_contrasena():
    global mostrar_pass
    mostrar_pass = not mostrar_pass
    if mostrar_pass:
        entry_pass.configure(show="")
        btn_ver_pass.configure(
            text="👁",
            fg_color="#DBEAFE",
            hover_color="#BFDBFE",
            text_color="#1D4ED8"
        )
    else:
        entry_pass.configure(show="•")
        btn_ver_pass.configure(
            text="👁",
            fg_color=BG_INPUT,
            hover_color=BORDER_LIGHT,
            text_color=TEXT_PRIMARY
        )

def alternar_columna_derecha():
    pass

def centrar_ventana(win, ancho, alto):
    """Centra una ventana CTkToplevel en la pantalla de forma segura"""
    try:
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, int((sw - ancho) / 2))
        y = max(0, int((sh - alto) / 2))
        win.geometry(f"{ancho}x{alto}+{x}+{y}")
    except Exception:
        try:
            win.geometry(f"{ancho}x{alto}")
        except Exception:
            pass

_top_dialogo_jig = None

def abrir_dialogo_seleccion_jig():
    """
    Diálogo para seleccionar el Jig/Receta al iniciar sesión.
    No permite pintar hasta que el operador calibre y ajuste el Set 0 en el panel principal.
    """
    global _top_dialogo_jig
    ocultar_teclado_nativo()
    if _top_dialogo_jig is not None and _top_dialogo_jig.winfo_exists():
        try:
            _top_dialogo_jig.lift()
            _top_dialogo_jig.focus_force()
        except Exception:
            pass
        return

    try:
        top_jig = ctk.CTkToplevel(ventana)
        _top_dialogo_jig = top_jig
        top_jig.title("Selección de Jig — KOSTAL HMI")
        top_jig.geometry("560x430")
        top_jig.resizable(False, False)
        top_jig.configure(fg_color=BG_MAIN)
        top_jig.transient(ventana)
        
        centrar_ventana(top_jig, 560, 430)
        try:
            top_jig.lift()
            top_jig.focus_force()
        except Exception:
            pass
        def _grab_sel_jig():
            try:
                if top_jig.winfo_exists():
                    top_jig.grab_set()
            except Exception:
                pass
        try:
            top_jig.grab_set()
        except Exception:
            try:
                top_jig.after(80, _grab_sel_jig)
            except Exception:
                pass
        
        # Encabezado corporativo KOSTAL
        f_top_hdr = ctk.CTkFrame(top_jig, fg_color=COLOR_KOSTAL_NAVY, height=48, corner_radius=0)
        f_top_hdr.pack(fill="x")
        
        ctk.CTkLabel(
            f_top_hdr, 
            text="🏭 CONFIGURACIÓN DE JIG DE PRODUCCIÓN", 
            font=(FONT_MAIN, 13, "bold"), 
            text_color="white"
        ).pack(side="left", padx=16, pady=10)

        f_jig_body = ctk.CTkFrame(top_jig, fg_color="transparent")
        f_jig_body.pack(fill="both", expand=True, padx=16, pady=12)

        recetas_disp = config_actual.get("recetas", [])
        nombres_recetas = [r.get("nombre", "") for r in recetas_disp] or ["Receta Base"]
        receta_actual_nom = config_actual.get("receta_activa", nombres_recetas[0])
        var_sel_rec = ctk.StringVar(value=receta_actual_nom)

        ctk.CTkLabel(
            f_jig_body,
            text="📋 Seleccione el Jig / Receta para este turno de producción:",
            font=(FONT_MAIN, 12, "bold"),
            text_color=COLOR_KOSTAL_DARK,
            anchor="w"
        ).pack(fill="x", pady=(0, 8))

        card_rec_prev = ctk.CTkFrame(f_jig_body, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
        card_rec_prev.pack(fill="both", expand=True, pady=(0, 10))

        f_sel_row = ctk.CTkFrame(card_rec_prev, fg_color="transparent")
        f_sel_row.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(f_sel_row, text="Jig Seleccionado:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_PRIMARY).pack(side="left", padx=(0, 10))

        f_detalles_rec = ctk.CTkFrame(card_rec_prev, fg_color="#F8FAFC", corner_radius=6, border_width=1, border_color="#E2E8F0")
        f_detalles_rec.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        f_det_txt = ctk.CTkFrame(f_detalles_rec, fg_color="transparent")
        f_det_txt.pack(side="left", fill="both", expand=True, padx=12, pady=10)

        lbl_det_nom = ctk.CTkLabel(f_det_txt, text="Jig: -", font=(FONT_MAIN, 12, "bold"), text_color=COLOR_KOSTAL_NAVY, anchor="w")
        lbl_det_nom.pack(fill="x", pady=2)
        lbl_det_pieza = ctk.CTkLabel(f_det_txt, text="Pieza: -", font=(FONT_MAIN, 11), text_color=TEXT_PRIMARY, anchor="w")
        lbl_det_pieza.pack(fill="x", pady=1)
        lbl_det_mod = ctk.CTkLabel(f_det_txt, text="Modelo: -", font=(FONT_MAIN, 11), text_color=TEXT_SECONDARY, anchor="w")
        lbl_det_mod.pack(fill="x", pady=1)
        lbl_det_mat = ctk.CTkLabel(f_det_txt, text="Matriz Fixture: -", font=(FONT_MONO, 11, "bold"), text_color="#1E3A8A", anchor="w")
        lbl_det_mat.pack(fill="x", pady=2)
        lbl_det_dist = ctk.CTkLabel(f_det_txt, text="Dist. C-C: -", font=(FONT_MONO, 10), text_color=TEXT_MUTED, anchor="w")
        lbl_det_dist.pack(fill="x", pady=1)
        lbl_det_cero = ctk.CTkLabel(f_det_txt, text="Set 0: -", font=(FONT_MONO, 10, "bold"), text_color="#065F46", anchor="w")
        lbl_det_cero.pack(fill="x", pady=1)

        f_img_box = ctk.CTkFrame(f_detalles_rec, fg_color="#FFFFFF", corner_radius=6, border_width=1, border_color="#CBD5E1", width=125, height=105)
        f_img_box.pack(side="right", padx=12, pady=10)
        f_img_box.pack_propagate(False)

        lbl_img_prev = ctk.CTkLabel(f_img_box, text="[Sin imagen]", font=(FONT_MAIN, 9), text_color=TEXT_MUTED)
        lbl_img_prev.pack(expand=True, fill="both")

        def actualizar_preview_receta(nombre_rec):
            try:
                rec_obj = next((r for r in recetas_disp if r.get("nombre") == nombre_rec), recetas_disp[0] if recetas_disp else {})
                if rec_obj:
                    lbl_det_nom.configure(text=f"Jig: {rec_obj.get('nombre', '')}")
                    lbl_det_pieza.configure(text=f"Pieza: {rec_obj.get('pieza', '')}")
                    lbl_det_mod.configure(text=f"Modelo: {rec_obj.get('modelo', '')}")
                    fil = rec_obj.get("filas", 1)
                    col = rec_obj.get("cols", 1)
                    tot = obtener_piezas_por_jig_receta(rec_obj)
                    lbl_det_mat.configure(text=f"Matriz Fixture: {fil} × {col} ({tot} Piezas/Jig)")
                    lbl_det_dist.configure(text=f"Dist. C-C: ΔX={rec_obj.get('delta_x', 0):.1f} mm | ΔY={rec_obj.get('delta_y', 0):.1f} mm")
                    
                    ya_cal = bool(rec_obj.get("cero_calibrado", False) or rec_obj.get("corridas", 0) > 0 or ("cero_guardado_x" in rec_obj and "cero_guardado_y" in rec_obj))
                    gx = float(rec_obj.get("cero_guardado_x", 0.0))
                    gy = float(rec_obj.get("cero_guardado_y", 0.0))
                    if ya_cal:
                        lbl_det_cero.configure(text=f"✓ Cero Predeterminado: X={gx:.2f} | Y={gy:.2f} mm", text_color="#065F46")
                    else:
                        lbl_det_cero.configure(text="⚠️ Cero: Pendiente de calibrar con JOG", text_color="#B45309")

                    img_path = rec_obj.get("imagen", "")
                    ctk_img = cargar_imagen(img_path, 115, 95)
                    if ctk_img:
                        lbl_img_prev.configure(image=ctk_img, text="")
                    else:
                        lbl_img_prev.configure(image=None, text="[Vista Jig]")
            except Exception as ep:
                print(f"Error actualizando preview de receta: {ep}")

        opt_rec = ctk.CTkOptionMenu(
            f_sel_row,
            values=nombres_recetas,
            variable=var_sel_rec,
            command=actualizar_preview_receta,
            width=280,
            height=34,
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color="#1E3A8A",
            dropdown_fg_color="#FFFFFF",
            dropdown_text_color="#1E293B",
            font=(FONT_MAIN, 11, "bold")
        )
        opt_rec.pack(side="left")

        actualizar_preview_receta(var_sel_rec.get())

        def confirmar_jig():
            global cero_ha_sido_seteado, _top_dialogo_jig
            try:
                rec_elegida = var_sel_rec.get()
                cargar_receta_por_nombre(rec_elegida, mostrar_modal_aviso=False)
                if 'option_receta' in globals() and option_receta:
                    try:
                        option_receta.set(rec_elegida)
                    except Exception:
                        pass
                    
                # Si la receta ya se corrió al menos una vez o ya tiene Set 0 calibrado,
                # cargar_receta_por_nombre ya fijó cero_ha_sido_seteado = True con sus coordenadas predeterminadas
                if cero_ha_sido_seteado:
                    if 'lbl_estado' in globals() and lbl_estado:
                        try:
                            lbl_estado.configure(
                                text=f"✓ Jig '{rec_elegida}' confirmado con Cero predeterminado (X:{cero_guardado_x:.2f} Y:{cero_guardado_y:.2f} mm). Listo para pintar.",
                                text_color=COLOR_OK
                            )
                        except Exception:
                            pass
                else:
                    if 'lbl_indicador_set0' in globals() and lbl_indicador_set0:
                        try:
                            lbl_indicador_set0.configure(
                                text=f"⚠️ SET 0 PENDIENTE: Calibre origen para '{rec_elegida}' con JOG y presione '↩ Set 0'",
                                text_color="#B45309"
                            )
                        except Exception:
                            pass
                    if 'f_cero_ind' in globals() and f_cero_ind:
                        try:
                            f_cero_ind.configure(fg_color="#FEF3C7", border_color="#FDE68A")
                        except Exception:
                            pass
                    if 'lbl_estado' in globals() and lbl_estado:
                        try:
                            lbl_estado.configure(
                                text=f"✓ Jig '{rec_elegida}' confirmado. Calibre Set 0 con la Cruceta JOG antes de iniciar el pintado.",
                                text_color=COLOR_WARN
                            )
                        except Exception:
                            pass
            except Exception as err:
                print(f"Error procesando confirmación de Jig: {err}")
            finally:
                ocultar_teclado_nativo()
                try:
                    top_jig.grab_release()
                except Exception:
                    pass
                try:
                    top_jig.destroy()
                except Exception:
                    pass
                _top_dialogo_jig = None

        top_jig.protocol("WM_DELETE_WINDOW", confirmar_jig)

        btn_confirmar_jig = ctk.CTkButton(
            f_jig_body,
            text="✓  Confirmar Jig y Entrar al Panel",
            font=(FONT_MAIN, 13, "bold"),
            fg_color=COLOR_KOSTAL_NAVY,
            hover_color=COLOR_KOSTAL_ACCENT,
            text_color="white",
            height=44,
            corner_radius=6,
            command=confirmar_jig
        )
        btn_confirmar_jig.pack(fill="x", pady=4)

    except Exception as e:
        print(f"Error al abrir diálogo de selección de jig: {e}")

abrir_asistente_inicio_sesion = abrir_dialogo_seleccion_jig

def iniciar_sesion(usuario_seleccionado, rol="OPERADOR"):
    """
    Inicia sesión.
    REQUERIMIENTO: Solo se detona la variable INICIO si el login es correcto.
    """
    global usuario_actual, segundos_sesion
    ocultar_teclado_nativo()
    usuario_actual = {"nombre": usuario_seleccionado, "rol": rol}
    
    # DETONAR PULSO DE VARIABLE INICIO AL VALIDAR CORRECTAMENTE
    enviar_pulso("INICIO")
    
    lbl_hdr_user_name.configure(text=usuario_seleccionado)
    if rol == "ADMIN":
        lbl_hdr_role_badge.configure(text="ADMINISTRADOR", fg_color=COLOR_PURPLE_SOFT, text_color=COLOR_PURPLE_TEXT)
        btn_ir_admin.pack(side="left", padx=6)
    else:
        lbl_hdr_role_badge.configure(text="OPERADOR", fg_color=COLOR_OK_SOFT, text_color=COLOR_OK_TEXT)
        btn_ir_admin.pack_forget()
        
    segundos_sesion = 0
    actualizar_temporizador_sesion()
    
    frame_login.pack_forget()
    frame_admin.pack_forget()
    frame_operador.pack(fill="both", expand=True)
    
    global cero_ha_sido_seteado, cero_guardado_x, cero_guardado_y
    # Comprobar si la receta activa actual ya tiene su Set 0 predeterminado ajustado
    rec_activa_nom = config_actual.get("receta_activa", "")
    rec_obj = next((r for r in config_actual.get("recetas", []) if r.get("nombre") == rec_activa_nom), None)
    if rec_obj and (rec_obj.get("cero_calibrado", False) or rec_obj.get("corridas", 0) > 0 or ("cero_guardado_x" in rec_obj and "cero_guardado_y" in rec_obj)):
        cero_guardado_x = limitar_coord_x(float(rec_obj.get("cero_guardado_x", config_actual.get("cero_guardado_x", 0.0))))
        cero_guardado_y = limitar_coord_y(float(rec_obj.get("cero_guardado_y", config_actual.get("cero_guardado_y", 0.0))))
        cero_ha_sido_seteado = True
        if 'lbl_indicador_set0' in globals() and lbl_indicador_set0:
            lbl_indicador_set0.configure(
                text=f"✓ CERO PREDETERMINADO: X={cero_guardado_x:.2f}  Y={cero_guardado_y:.2f} mm",
                text_color="#065F46"
            )
        if 'f_cero_ind' in globals() and f_cero_ind:
            f_cero_ind.configure(fg_color="#ECFDF5", border_color="#A7F3D0")
        if 'lbl_cero_jog_val' in globals() and lbl_cero_jog_val:
            lbl_cero_jog_val.configure(text=f"X: {cero_guardado_x:.2f} mm  |  Y: {cero_guardado_y:.2f} mm")
    else:
        cero_ha_sido_seteado = False
        if 'lbl_indicador_set0' in globals() and lbl_indicador_set0:
            lbl_indicador_set0.configure(
                text="⚠️ SET 0 PENDIENTE: Calibre con Cruceta JOG y presione '↩ Set 0'",
                text_color="#B45309"
            )
        if 'f_cero_ind' in globals() and f_cero_ind:
            f_cero_ind.configure(fg_color="#FEF3C7", border_color="#FDE68A")
        
    lbl_estado.configure(
        text=f"✓ Sesión iniciada: {usuario_seleccionado} ({rol}) | Receta activa: '{rec_activa_nom}'.",
        text_color=COLOR_OK
    )
    
    # REQUERIMIENTO: Solo que aparezca para seleccionar el jig al abrir sesión
    ventana.after(350, abrir_dialogo_seleccion_jig)


def procesar_login_formulario():
    """Valida usuario y contraseña. Solo detona INICIO si las credenciales son válidas"""
    ocultar_teclado_nativo()
    usuario_val = option_usuario.get()
    pass_val = entry_pass.get()
    
    match = None
    for u in USUARIOS_SISTEMA:
        texto_combo = f"{u['nombre']} ({u['rol']})"
        if texto_combo == usuario_val or u['nombre'] == usuario_val:
            match = u
            break
            
    if not match:
        messagebox.showerror("Error de Autenticación", "Usuario no encontrado en el sistema.")
        return
        
    # VALIDACIÓN DE CONTRASEÑA
    if pass_val != match["pass"]:
        messagebox.showerror("Error de Autenticación", "Contraseña incorrecta. Verifique sus credenciales.")
        lbl_estado.configure(text="✗ Login fallido: Contraseña incorrecta. Variable INICIO no detonada.", text_color=COLOR_NOK)
        return
        
    # LOGIN CORRECTO -> DETONA INICIO
    iniciar_sesion(match['nombre'], match['rol'])

def cerrar_sesion():
    global temporizador_sesion_id, _top_dialogo_jig
    ocultar_teclado_nativo()
    if temporizador_sesion_id:
        ventana.after_cancel(temporizador_sesion_id)
        temporizador_sesion_id = None
        
    if _top_dialogo_jig is not None and _top_dialogo_jig.winfo_exists():
        try:
            _top_dialogo_jig.grab_release()
        except Exception:
            pass
        try:
            _top_dialogo_jig.destroy()
        except Exception:
            pass
        _top_dialogo_jig = None

    frame_operador.pack_forget()
    frame_admin.pack_forget()
    frame_login.pack(fill="both", expand=True)
    entry_pass.delete(0, tk.END)
    lbl_estado.configure(text="Sesión cerrada. Esperando autenticación...", text_color=TEXT_MUTED)

def ir_a_panel_admin():
    ocultar_teclado_nativo()
    frame_operador.pack_forget()
    frame_admin.pack(fill="both", expand=True)
    lbl_estado.configure(text="Módulo de Administración abierto (Recetas, Exportar Excel, Usuarios).", text_color=COLOR_KOSTAL_NAVY)

def volver_a_panel_operador():
    ocultar_teclado_nativo()
    frame_admin.pack_forget()
    frame_operador.pack(fill="both", expand=True)
    lbl_estado.configure(text="Retorno a Panel de Operador.", text_color=COLOR_OK)

def actualizar_reloj_y_fecha():
    ahora = datetime.datetime.now()
    if 'lbl_hdr_time' in globals():
        lbl_hdr_time.configure(text=ahora.strftime("%H:%M:%S"))
    if 'lbl_hdr_date' in globals():
        lbl_hdr_date.configure(text=ahora.strftime("%d/%m/%Y"))
    if 'lbl_adm_time' in globals():
        lbl_adm_time.configure(text=ahora.strftime("%H:%M:%S"))
    if 'lbl_adm_date' in globals():
        lbl_adm_date.configure(text=ahora.strftime("%d/%m/%Y"))
    ventana.after(1000, actualizar_reloj_y_fecha)

def actualizar_temporizador_sesion():
    global segundos_sesion, temporizador_sesion_id
    segundos_sesion += 1
    hrs = segundos_sesion // 3600
    mins = (segundos_sesion % 3600) // 60
    secs = segundos_sesion % 60
    lbl_hdr_session_timer.configure(text=f"{hrs:02d}:{mins:02d}:{secs:02d}")
    lbl_op_session_timer.configure(text=f"{hrs:02d}:{mins:02d}:{secs:02d}")
    temporizador_sesion_id = ventana.after(1000, actualizar_temporizador_sesion)

# ==============================================================================
# 5. VENTANA PRINCIPAL (CUSTOMTKINTER)
# ==============================================================================
ventana = ctk.CTk()
if MODO_SIM_WINDOWS:
    ventana.title("KOSTAL Industrial HMI — Modo Simulación Windows (Pruebas)")
    ventana.geometry("1400x860+60+20")
    ventana.minsize(1180, 700)
else:
    ventana.title("KOSTAL Industrial HMI - Estación de Inserción y Pintado SMC")
    ventana.geometry("1920x1080")
    ventana.minsize(1280, 720)

ventana.configure(fg_color=BG_MAIN)
if not MODO_SIM_WINDOWS:
    try:
        ventana.config(cursor="none")
    except Exception:
        pass
ventana.protocol("WM_DELETE_WINDOW", cerrar_aplicacion)

if not MODO_SIM_WINDOWS:
    # Modo Pantalla Completa nativa (Resolución objetivo 1920x1080)
    try:
        ventana.attributes("-fullscreen", True)
    except Exception:
        try:
            if sys.platform.startswith("linux"):
                ventana.attributes("-zoomed", True)
            else:
                ventana.state("zoomed")
        except Exception:
            pass

# Traer la interfaz al frente de la terminal y asegurar foco sin atrapar capas
ventana.lift()
ventana.focus_force()

# Atajos para alternar pantalla completa o salir con teclado
ventana.bind("<F11>", lambda e: alternar_pantalla_completa())
ventana.bind("<Escape>", lambda e: ventana.attributes("-fullscreen", False))

# Detección táctil inteligente: verificar si un widget o sus contenedores pertenecen a un campo de texto
def _es_widget_de_texto(w):
    try:
        cur = w
        for _ in range(5):
            if cur is None:
                break
            if isinstance(cur, (ctk.CTkEntry, tk.Entry, ttk.Entry, tk.Text)):
                return True
            c = ""
            try:
                c = cur.winfo_class()
            except Exception:
                pass
            if c in ("Entry", "TEntry", "Text"):
                return True
            master = getattr(cur, "master", None)
            if isinstance(master, ctk.CTkEntry):
                return True
            cur = master
    except Exception:
        pass
    return False

# Detección táctil global: al tocar cualquier área libre que NO sea entrada de texto, ocultar teclado
def _al_tocar_pantalla(event):
    try:
        w = event.widget
        if _es_widget_de_texto(w):
            return
        # Si el widget actual con foco es un campo de texto y el toque ocurrió dentro de un diálogo modal, no cerrar
        cur_focus = None
        try:
            cur_focus = ventana.focus_get()
        except Exception:
            pass
        if cur_focus is not None and _es_widget_de_texto(cur_focus):
            nombre_w = str(w).lower()
            if "toplevel" in nombre_w or "dialog" in nombre_w or "num" in nombre_w:
                return
        ocultar_teclado_nativo()
    except Exception:
        pass

ventana.bind_all("<Button-1>", _al_tocar_pantalla, add="+")

img_logo_kostal_login = cargar_imagen("LogoKostal.webp", 290, 60)
img_logo_baluarte_login = cargar_imagen("LOGO_BALUARTE.png", 220, 52)
img_logo_kostal_hdr = cargar_imagen("LogoKostal.webp", 150, 30)
img_logo_baluarte_hdr = cargar_imagen("LOGO_BALUARTE.png", 145, 36)
img_logo_baluarte_panel = cargar_imagen("LOGO_BALUARTE.png", 270, 68)

# ------------------------------------------------------------------------------
# 5.0. PANTALLA DE CARGA INDUSTRIAL (SPLASH SCREEN KOSTAL & BALUARTE)
# ------------------------------------------------------------------------------
frame_splash = ctk.CTkFrame(ventana, fg_color=BG_MAIN, corner_radius=0)
try:
    frame_splash.configure(cursor="none")
except Exception:
    pass
frame_splash.pack(fill="both", expand=True)

# Barra superior con botones de ventana en Splash
f_sp_top_bar = ctk.CTkFrame(frame_splash, fg_color="transparent", height=40)
f_sp_top_bar.pack(fill="x", side="top", padx=16, pady=12)

btn_sp_fs = ctk.CTkButton(
    f_sp_top_bar,
    text="⛶ Pantalla",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#E2E8F0",
    hover_color="#CBD5E1",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=90,
    height=30,
    command=alternar_pantalla_completa
)
btn_sp_fs.pack(side="right", padx=(6, 0))

btn_sp_min = ctk.CTkButton(
    f_sp_top_bar,
    text="🗕 Minimizar",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#E2E8F0",
    hover_color="#CBD5E1",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=95,
    height=30,
    command=minimizar_ventana
)
btn_sp_min.pack(side="right")

card_splash = ctk.CTkFrame(
    frame_splash,
    fg_color=BG_CARD,
    corner_radius=16,
    border_width=1,
    border_color=BORDER_CARD,
    width=740
)
card_splash.place(relx=0.5, rely=0.5, anchor="center")

f_sp_inner = ctk.CTkFrame(card_splash, fg_color="transparent")
f_sp_inner.pack(padx=38, pady=32, fill="both")

# Contenedor de Logos Corporativos (KOSTAL & Baluarte)
f_sp_logos = ctk.CTkFrame(f_sp_inner, fg_color="transparent")
f_sp_logos.pack(pady=(0, 16))

if img_logo_kostal_login:
    ctk.CTkLabel(f_sp_logos, image=img_logo_kostal_login, text="").pack(side="left", padx=8)
else:
    ctk.CTkLabel(f_sp_logos, text="KOSTAL", font=(FONT_MAIN, 28, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left", padx=8)

if img_logo_baluarte_login:
    ctk.CTkFrame(f_sp_logos, fg_color="#E2E8F0", width=2, height=48).pack(side="left", padx=16)
    ctk.CTkLabel(f_sp_logos, image=img_logo_baluarte_login, text="").pack(side="left", padx=8)

lbl_splash_title = ctk.CTkLabel(
    f_sp_inner,
    text="SMC — ESTACIÓN DE INSERCIÓN Y TAMPOGRAFÍA",
    font=(FONT_MAIN, 18, "bold"),
    text_color=COLOR_KOSTAL_NAVY
)
lbl_splash_title.pack(pady=(4, 2))

lbl_splash_subtitle = ctk.CTkLabel(
    f_sp_inner,
    text="KOSTAL AUTOMOTIVE ELECTRICAL SYSTEMS | PROYECTOS DE INTEGRACION BALUARTE",
    font=(FONT_MAIN, 11, "bold"),
    text_color=TEXT_SECONDARY
)
lbl_splash_subtitle.pack(pady=(0, 22))

progress_splash = ctk.CTkProgressBar(
    f_sp_inner,
    width=540,
    height=10,
    corner_radius=5,
    fg_color="#E2E8F0",
    progress_color=COLOR_KOSTAL_NAVY
)
progress_splash.set(0.0)
progress_splash.pack(pady=(6, 10))

f_sp_info = ctk.CTkFrame(f_sp_inner, fg_color="transparent", width=540)
f_sp_info.pack(fill="x", pady=(0, 8))

lbl_splash_status = ctk.CTkLabel(
    f_sp_info,
    text="Iniciando subsistemas HMI...",
    font=(FONT_MAIN, 11, "bold"),
    text_color=COLOR_KOSTAL_NAVY
)
lbl_splash_status.pack(side="left")

lbl_splash_pct = ctk.CTkLabel(
    f_sp_info,
    text="0%",
    font=(FONT_MONO, 12, "bold"),
    text_color=COLOR_KOSTAL_ACCENT
)
lbl_splash_pct.pack(side="right")

f_sp_badge = ctk.CTkFrame(f_sp_inner, fg_color="#F8FAFC", corner_radius=10, border_width=1, border_color="#E2E8F0")
f_sp_badge.pack(pady=(16, 0))

ctk.CTkLabel(
    f_sp_badge,
    text="VERSIÓN INDUSTRIAL 1.0.4 • 1920x1080 TOUCH OPTIMIZED",
    font=(FONT_MAIN, 9, "bold"),
    text_color="#64748B",
    padx=14,
    pady=4
).pack()

f_splash_actions = ctk.CTkFrame(f_sp_inner, fg_color="transparent")

def _iniciar_conexion_en_splash():
    if MODO_SIM_WINDOWS:
        progress_splash.set(0.60)
        lbl_splash_status.configure(text="⚡ Modo Simulación activado para pruebas en Windows...", text_color=COLOR_KOSTAL_ACCENT)
        lbl_splash_pct.configure(text="60%", text_color=COLOR_KOSTAL_ACCENT)
        def _paso_final_sim():
            progress_splash.configure(progress_color="#10B981")
            progress_splash.set(1.0)
            lbl_splash_status.configure(text="✓ Sistema Listo en MODO SIMULACIÓN.", text_color="#10B981")
            lbl_splash_pct.configure(text="100%", text_color="#10B981")
            ventana.after(100, _mostrar_login)
        ventana.after(80, _paso_final_sim)
        return

    progress_splash.set(0.20)
    lbl_splash_status.configure(text="Iniciando subsistemas HMI y registros de memoria...", text_color=COLOR_KOSTAL_NAVY)
    lbl_splash_pct.configure(text="20%", text_color=COLOR_KOSTAL_ACCENT)
    
    def _paso_conectar():
        progress_splash.set(0.45)
        lbl_splash_status.configure(text="Comprobando comunicación con PLC WAGO (192.168.1.69:4840)...", text_color=COLOR_KOSTAL_NAVY)
        lbl_splash_pct.configure(text="45%", text_color=COLOR_KOSTAL_ACCENT)
        
        res = [None]
        def _thread_con():
            res[0] = intentar_conectar_plc(timeout_seg=2.0)
        
        import threading
        t = threading.Thread(target=_thread_con, daemon=True)
        t.start()
        
        def _verificar_hilo(intentos=0):
            if t.is_alive():
                p_actual = min(0.75, 0.45 + (intentos * 0.04))
                progress_splash.set(p_actual)
                lbl_splash_pct.configure(text=f"{int(p_actual*100)}%", text_color=COLOR_KOSTAL_ACCENT)
                ventana.after(60, lambda: _verificar_hilo(intentos + 1))
            else:
                if res[0] is True:
                    progress_splash.configure(progress_color="#10B981")
                    progress_splash.set(1.0)
                    lbl_splash_status.configure(text="✓ Conexión establecida con PLC WAGO exitosamente.", text_color="#10B981")
                    lbl_splash_pct.configure(text="100%", text_color="#10B981")
                    ventana.after(120, _mostrar_login)
                else:
                    progress_splash.configure(progress_color="#F59E0B")
                    lbl_splash_status.configure(
                        text="⚠️ Sin comunicación con PLC WAGO (192.168.1.69:4840)",
                        text_color="#D97706"
                    )
                    lbl_splash_pct.configure(text="PAUSA", text_color="#D97706")
                    _mostrar_opciones_sin_plc()
                    
        ventana.after(60, _verificar_hilo)
        
    ventana.after(60, _paso_conectar)

def _mostrar_login():
    try:
        frame_splash.pack_forget()
        frame_login.pack(fill="both", expand=True)
    except Exception:
        pass

def _mostrar_opciones_sin_plc():
    for child in f_splash_actions.winfo_children():
        child.destroy()
    f_splash_actions.pack(pady=(16, 0), fill="x")
    
    ctk.CTkLabel(
        f_splash_actions,
        text="El PLC no respondió en la red. Verifique cable Ethernet o seleccione cómo proceder:",
        font=(FONT_MAIN, 11),
        text_color=TEXT_PRIMARY
    ).pack(pady=(0, 10))
    
    f_sp_btns = ctk.CTkFrame(f_splash_actions, fg_color="transparent")
    f_sp_btns.pack()
    
    btn_sp_reintentar = ctk.CTkButton(
        f_sp_btns,
        text="🔄 Reintentar Conexión al PLC",
        font=(FONT_MAIN, 11, "bold"),
        fg_color=COLOR_KOSTAL_NAVY,
        hover_color=COLOR_KOSTAL_LIGHT,
        text_color="white",
        height=38,
        width=210,
        corner_radius=8,
        command=_reintentar_desde_splash
    )
    btn_sp_reintentar.pack(side="left", padx=6)
    
    btn_sp_simulacion = ctk.CTkButton(
        f_sp_btns,
        text="⚡ Iniciar en Modo Simulación",
        font=(FONT_MAIN, 11, "bold"),
        fg_color="#FFFFFF",
        hover_color="#F1F5F9",
        text_color=COLOR_KOSTAL_NAVY,
        border_width=1,
        border_color="#CBD5E1",
        height=38,
        width=210,
        corner_radius=8,
        command=_continuar_simulacion_desde_splash
    )
    btn_sp_simulacion.pack(side="left", padx=6)

def _reintentar_desde_splash():
    f_splash_actions.pack_forget()
    progress_splash.configure(progress_color=COLOR_KOSTAL_NAVY)
    _iniciar_conexion_en_splash()

def _continuar_simulacion_desde_splash():
    f_splash_actions.pack_forget()
    progress_splash.configure(progress_color="#0284C7")
    progress_splash.set(1.0)
    lbl_splash_status.configure(text="Iniciando en MODO SIMULACIÓN (controles activos)...", text_color=COLOR_KOSTAL_ACCENT)
    lbl_splash_pct.configure(text="100%", text_color=COLOR_KOSTAL_ACCENT)
    ventana.after(200, _mostrar_login)

ventana.after(50, _iniciar_conexion_en_splash)

# ------------------------------------------------------------------------------
# 5.1. PANTALLA 1: LOGIN LIMPIO CON VER/OCULTAR CONTRASEÑA
# ------------------------------------------------------------------------------
frame_login = ctk.CTkFrame(ventana, fg_color=BG_MAIN, corner_radius=0)
try:
    frame_login.configure(cursor="none")
except Exception:
    pass

# Barra superior con botones de control de ventana y teclado en Login
f_login_top_bar = ctk.CTkFrame(frame_login, fg_color="transparent", height=40)
f_login_top_bar.pack(fill="x", side="top", padx=16, pady=12)

btn_login_fs = ctk.CTkButton(
    f_login_top_bar,
    text="⛶ Pantalla",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#E2E8F0",
    hover_color="#CBD5E1",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=90,
    height=30,
    command=alternar_pantalla_completa
)
btn_login_fs.pack(side="right", padx=(6, 0))

btn_login_min = ctk.CTkButton(
    f_login_top_bar,
    text="🗕 Minimizar",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#E2E8F0",
    hover_color="#CBD5E1",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=95,
    height=30,
    command=minimizar_ventana
)
btn_login_min.pack(side="right", padx=(6, 0))

btn_login_kb = ctk.CTkButton(
    f_login_top_bar,
    text="⌨ Teclado",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#334155",
    hover_color="#475569",
    text_color="#FFFFFF",
    corner_radius=6,
    width=90,
    height=30,
    command=alternar_teclado_tactil
)
btn_login_kb.pack(side="right")

card_login = ctk.CTkFrame(
    frame_login, 
    fg_color=BG_CARD, 
    corner_radius=16, 
    border_width=1, 
    border_color=BORDER_CARD,
    width=680
)
card_login.place(relx=0.5, rely=0.5, anchor="center")

f_login_inner = ctk.CTkFrame(card_login, fg_color="transparent")
f_login_inner.pack(padx=42, pady=32, fill="both")

if img_logo_kostal_login:
    ctk.CTkLabel(f_login_inner, image=img_logo_kostal_login, text="").pack(pady=(0, 6))
else:
    ctk.CTkLabel(f_login_inner, text="KOSTAL", font=(FONT_MAIN, 28, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(pady=(0, 4))

lbl_login_sys = ctk.CTkLabel(
    f_login_inner, 
    text="SISTEMA HMI DE INSPECCIÓN", 
    font=(FONT_MAIN, 20, "bold"), 
    text_color=COLOR_KOSTAL_NAVY
)
lbl_login_sys.pack(pady=(0, 2))

lbl_login_sub = ctk.CTkLabel(
    f_login_inner, 
    text="Línea de Ensamblado & Calidad KOSTAL", 
    font=(FONT_MAIN, 13), 
    text_color=TEXT_SECONDARY
)
lbl_login_sub.pack(pady=(0, 16))

lbl_login_user = ctk.CTkLabel(f_login_inner, text="Usuario / Perfil", font=(FONT_MAIN, 14, "bold"), text_color=TEXT_PRIMARY, anchor="w")
lbl_login_user.pack(fill="x", pady=(2, 4))

option_usuario = ctk.CTkOptionMenu(
    f_login_inner,
    values=[f"{u['nombre']} ({u['rol']})" for u in USUARIOS_SISTEMA],
    fg_color=BG_INPUT,
    button_color=BORDER_CARD,
    button_hover_color=BORDER_LIGHT,
    text_color=TEXT_PRIMARY,
    dropdown_fg_color=BG_CARD,
    dropdown_text_color=TEXT_PRIMARY,
    corner_radius=8,
    height=52,
    font=(FONT_MAIN, 15, "bold"),
    dropdown_font=(FONT_MAIN, 14)
)
option_usuario.set(f"{USUARIOS_SISTEMA[0]['nombre']} ({USUARIOS_SISTEMA[0]['rol']})")
option_usuario.pack(fill="x", pady=(0, 14))

lbl_login_pass = ctk.CTkLabel(f_login_inner, text="Contraseña", font=(FONT_MAIN, 14, "bold"), text_color=TEXT_PRIMARY, anchor="w")
lbl_login_pass.pack(fill="x", pady=(2, 4))

f_pass_row = ctk.CTkFrame(f_login_inner, fg_color="transparent")
f_pass_row.pack(fill="x", pady=(0, 14))

entry_pass = ctk.CTkEntry(
    f_pass_row,
    show="•",
    fg_color=BG_INPUT,
    border_color=BORDER_CARD,
    border_width=1,
    text_color=TEXT_PRIMARY,
    corner_radius=8,
    height=52,
    font=(FONT_MAIN, 16),
    placeholder_text="Ingrese su contraseña"
)
entry_pass.pack(side="left", fill="x", expand=True)

btn_ver_pass = ctk.CTkButton(
    f_pass_row,
    text="👁",
    width=52,
    height=52,
    font=(FONT_MAIN, 18),
    fg_color=BG_INPUT,
    hover_color=BORDER_LIGHT,
    text_color=TEXT_PRIMARY,
    border_width=1,
    border_color=BORDER_CARD,
    corner_radius=8,
    command=alternar_ver_contrasena
)
btn_ver_pass.pack(side="left", padx=(8, 0))

# Enfocar automáticamente el campo de contraseña para permitir escritura inmediata
ventana.after(300, lambda: entry_pass.focus_force())

btn_iniciar_sesion = ctk.CTkButton(
    f_login_inner,
    text="Iniciar Sesión  ➜",
    font=(FONT_MAIN, 16, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    corner_radius=8,
    height=56,
    command=procesar_login_formulario
)
btn_iniciar_sesion.pack(fill="x", pady=(14, 10))

# Detección de tecla Enter en el campo de contraseña y pantalla de Login
def _al_presionar_enter_login(e=None):
    try:
        ocultar_teclado_nativo()
    except Exception:
        pass
    procesar_login_formulario()
    return "break"

entry_pass.bind("<Return>", _al_presionar_enter_login)
entry_pass.bind("<KP_Enter>", _al_presionar_enter_login)

def _on_ventana_return_global(e=None):
    try:
        if 'frame_login' in globals() and frame_login.winfo_ismapped():
            _al_presionar_enter_login(e)
    except Exception:
        pass
ventana.bind("<Return>", _on_ventana_return_global, add="+")
ventana.bind("<KP_Enter>", _on_ventana_return_global, add="+")

f_foot = ctk.CTkFrame(f_login_inner, fg_color="transparent")
f_foot.pack(fill="x", pady=(14, 0))
ctk.CTkLabel(f_foot, text="Desarrollado en alianza con", font=(FONT_MAIN, 9), text_color=TEXT_MUTED).pack(side="left", expand=True, anchor="e", padx=4)
if img_logo_baluarte_login:
    ctk.CTkLabel(f_foot, image=img_logo_baluarte_login, text="").pack(side="left", expand=True, anchor="w")

# ------------------------------------------------------------------------------
# 5.2. PANTALLA 2: PANTALLA PRINCIPAL DEL OPERADOR
# ------------------------------------------------------------------------------
frame_operador = ctk.CTkFrame(ventana, fg_color=BG_MAIN, corner_radius=0)
try:
    frame_operador.configure(cursor="none")
except Exception:
    pass

header_hmi = ctk.CTkFrame(frame_operador, fg_color="transparent", height=58, corner_radius=0)
header_hmi.pack(fill="x", side="top", padx=12, pady=(8, 4))

# Bloque Izquierdo: Brand KOSTAL & Estación
hdr_left = ctk.CTkFrame(header_hmi, fg_color=COLOR_KOSTAL_NAVY, corner_radius=8, height=44)
hdr_left.pack(side="left", fill="y", padx=(0, 8))

if img_logo_kostal_hdr:
    ctk.CTkLabel(hdr_left, image=img_logo_kostal_hdr, text="").pack(side="left", padx=(12, 8), pady=6)
else:
    ctk.CTkLabel(hdr_left, text="KOSTAL", font=(FONT_MAIN, 16, "bold"), text_color="white").pack(side="left", padx=(14, 8), pady=6)

ctk.CTkLabel(
    hdr_left, 
    text="EST-KOSTAL-04", 
    font=(FONT_MONO, 10, "bold"), 
    fg_color="transparent", 
    text_color="#93C5FD", 
    corner_radius=4,
    padx=8, 
    pady=2
).pack(side="left", padx=(0, 12), pady=6)

# Logo Baluarte Integrador en Header Superior (al lado de KOSTAL)
hdr_baluarte = ctk.CTkFrame(header_hmi, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD, height=44)
hdr_baluarte.pack(side="left", fill="y", padx=(0, 6))
if img_logo_baluarte_hdr:
    ctk.CTkLabel(hdr_baluarte, image=img_logo_baluarte_hdr, text="").pack(side="left", padx=10, pady=3)
else:
    ctk.CTkLabel(hdr_baluarte, text="BALUARTE", font=(FONT_MAIN, 12, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left", padx=10, pady=6)

# Bloque Central: Floating White Card (Hora, Fecha, Ajustes HMI, Fin Hora)
hdr_mid = ctk.CTkFrame(header_hmi, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD, height=44)
hdr_mid.pack(side="left", expand=True, fill="y", padx=4)

f_time_box = ctk.CTkFrame(hdr_mid, fg_color="transparent")
f_time_box.pack(side="left", padx=12, pady=6)
ctk.CTkLabel(f_time_box, text="🕒", font=(FONT_MAIN, 11), text_color=COLOR_KOSTAL_NAVY).pack(side="left", padx=(0, 4))
lbl_hdr_time = ctk.CTkLabel(f_time_box, text="09:05:03", font=(FONT_MONO, 14, "bold"), text_color=COLOR_KOSTAL_NAVY)
lbl_hdr_time.pack(side="left")

f_date_box = ctk.CTkFrame(hdr_mid, fg_color="transparent")
f_date_box.pack(side="left", padx=(0, 14), pady=6)
ctk.CTkLabel(f_date_box, text="📅", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left", padx=(0, 4))
lbl_hdr_date = ctk.CTkLabel(f_date_box, text="24/09/2026", font=(FONT_MONO, 11), text_color=TEXT_MUTED)
lbl_hdr_date.pack(side="left")

btn_ir_admin = ctk.CTkButton(
    hdr_mid,
    text="⚙ Ajustes HMI",
    font=(FONT_MAIN, 10, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=28,
    width=110,
    command=ir_a_panel_admin
)
btn_ir_admin.pack(side="left", padx=6, pady=8)

kpis_fin_hora_registrados = False
piezas_ok_registradas = 0
piezas_nok_registradas = 0
calidad_registrada = 100.0
total_piezas_sesion_acum = 0

_top_dialogo_fin_hora = None

def abrir_dialogo_fin_de_hora():
    global kpis_fin_hora_registrados, piezas_ok_registradas, piezas_nok_registradas, calidad_registrada, _top_dialogo_fin_hora
    
    try:
        if _top_dialogo_fin_hora is not None:
            try:
                if _top_dialogo_fin_hora.winfo_exists():
                    _top_dialogo_fin_hora.destroy()
            except Exception:
                pass
            _top_dialogo_fin_hora = None

        dialogo = ctk.CTkToplevel(ventana)
        _top_dialogo_fin_hora = dialogo
        dialogo.title("Registro de Fin de Hora")
        dialogo.geometry("480x470")
        dialogo.resizable(False, False)
        dialogo.configure(fg_color=BG_MAIN)
        try:
            dialogo.transient(ventana)
        except Exception:
            pass
        centrar_ventana(dialogo, 480, 470)
        
        f_d_inner = ctk.CTkFrame(dialogo, fg_color=BG_CARD, corner_radius=12, border_width=1, border_color=BORDER_CARD)
        f_d_inner.pack(padx=18, pady=18, fill="both", expand=True)
        
        ctk.CTkLabel(
            f_d_inner, 
            text="⏱️  REGISTRO DE FIN DE HORA", 
            font=(FONT_MAIN, 15, "bold"), 
            text_color=COLOR_KOSTAL_NAVY
        ).pack(pady=(14, 2))
        ctk.CTkLabel(
            f_d_inner, 
            text="Ingrese las piezas defectuosas (NO OK) y confirme las piezas buenas (OK).", 
            font=(FONT_MAIN, 11), 
            text_color=TEXT_SECONDARY
        ).pack(pady=(0, 12))
        
        total_actual = total_piezas_sesion_acum
        try:
            if 'lbl_kpi_total' in globals() and lbl_kpi_total:
                txt_t = lbl_kpi_total.cget("text")
                if txt_t.isdigit() and int(txt_t) > 0:
                    total_actual = max(total_actual, int(txt_t))
        except Exception:
            pass
            
        f_tot = ctk.CTkFrame(f_d_inner, fg_color=BG_INPUT, corner_radius=8, border_width=1, border_color=BORDER_LIGHT)
        f_tot.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkLabel(f_tot, text="Total Piezas del Turno:", font=(FONT_MAIN, 12, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=12, pady=8)
        lbl_tot_num = ctk.CTkLabel(f_tot, text=str(total_actual), font=(FONT_MONO, 15, "bold"), text_color=COLOR_KOSTAL_NAVY)
        lbl_tot_num.pack(side="right", padx=12, pady=8)
        
        # Campo NOK (Defecto) primero con botones +/- para máxima rapidez operativa
        f_nok_row = ctk.CTkFrame(f_d_inner, fg_color="transparent")
        f_nok_row.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(
            f_nok_row, 
            text="Piezas NO OK (Defectos):", 
            font=(FONT_MAIN, 12, "bold"), 
            text_color=COLOR_NOK, 
            width=175, 
            anchor="w"
        ).pack(side="left")
        
        f_nok_ctrl = ctk.CTkFrame(f_nok_row, fg_color="transparent")
        f_nok_ctrl.pack(side="right")
        
        entry_nok = ctk.CTkEntry(
            f_nok_ctrl, 
            width=80, 
            height=34, 
            font=(FONT_MONO, 13, "bold"), 
            justify="center", 
            fg_color=BG_INPUT, 
            border_color=COLOR_NOK
        )
        val_nok_init = piezas_nok_registradas if kpis_fin_hora_registrados else 0
        entry_nok.insert(0, str(val_nok_init))
        
        def ajustar_nok(delta):
            try:
                cur = int(entry_nok.get().strip() or "0")
            except Exception:
                cur = 0
            nuevo = max(0, min(total_actual, cur + delta))
            entry_nok.delete(0, tk.END)
            entry_nok.insert(0, str(nuevo))
            # Sincronizar automáticamente OK
            nuevo_ok = max(0, total_actual - nuevo)
            entry_ok.delete(0, tk.END)
            entry_ok.insert(0, str(nuevo_ok))
            recalcular_prev()
            
        btn_nok_minus = ctk.CTkButton(
            f_nok_ctrl, text="−", width=32, height=34, font=(FONT_MONO, 14, "bold"),
            fg_color="#FEE2E2", hover_color="#FECACA", text_color=COLOR_NOK,
            corner_radius=6, command=lambda: ajustar_nok(-1)
        )
        btn_nok_minus.pack(side="left", padx=(0, 4))
        
        entry_nok.pack(side="left")
        
        btn_nok_plus = ctk.CTkButton(
            f_nok_ctrl, text="+", width=32, height=34, font=(FONT_MONO, 14, "bold"),
            fg_color="#FEE2E2", hover_color="#FECACA", text_color=COLOR_NOK,
            corner_radius=6, command=lambda: ajustar_nok(1)
        )
        btn_nok_plus.pack(side="left", padx=(4, 0))
        
        # Campo OK (Buenas)
        f_ok_row = ctk.CTkFrame(f_d_inner, fg_color="transparent")
        f_ok_row.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(
            f_ok_row, 
            text="Piezas OK (Aprobadas):", 
            font=(FONT_MAIN, 12, "bold"), 
            text_color=COLOR_OK, 
            width=175, 
            anchor="w"
        ).pack(side="left")
        
        val_ok_init = piezas_ok_registradas if kpis_fin_hora_registrados else max(0, total_actual - val_nok_init)
        entry_ok = ctk.CTkEntry(
            f_ok_row, 
            width=156, 
            height=34, 
            font=(FONT_MONO, 13, "bold"), 
            justify="center", 
            fg_color=BG_INPUT, 
            border_color=COLOR_OK
        )
        entry_ok.insert(0, str(val_ok_init))
        entry_ok.pack(side="right")
        
        # Preview de % Calidad
        f_cal_box = ctk.CTkFrame(f_d_inner, fg_color="#F8FAFC", corner_radius=8, border_width=1, border_color="#E2E8F0")
        f_cal_box.pack(fill="x", padx=18, pady=10)
        lbl_prev_cal = ctk.CTkLabel(
            f_cal_box, 
            text="% Calidad resultante: 100.0%", 
            font=(FONT_MAIN, 13, "bold"), 
            text_color=COLOR_OK
        )
        lbl_prev_cal.pack(pady=8)
        
        def recalcular_prev(event=None):
            try:
                v_ok = int(entry_ok.get().strip() or "0")
                v_nok = int(entry_nok.get().strip() or "0")
                tot = v_ok + v_nok
                if tot > 0:
                    cal = (v_ok / tot) * 100.0
                    c_clr = COLOR_OK if cal >= 95.0 else (COLOR_WARN if cal >= 85.0 else COLOR_NOK)
                    lbl_prev_cal.configure(text=f"% Calidad: {cal:.1f}%  (OK: {v_ok} | NOK: {v_nok} | Tot: {tot})", text_color=c_clr)
                else:
                    lbl_prev_cal.configure(text="% Calidad resultante: 100.0%", text_color=COLOR_OK)
            except Exception:
                pass
                
        def on_nok_key(event=None):
            try:
                v_nok = int(entry_nok.get().strip() or "0")
                # Autoajustar OK según el total
                v_ok = max(0, total_actual - v_nok)
                entry_ok.delete(0, tk.END)
                entry_ok.insert(0, str(v_ok))
            except Exception:
                pass
            recalcular_prev()
            
        entry_nok.bind("<KeyRelease>", on_nok_key)
        entry_ok.bind("<KeyRelease>", recalcular_prev)
        recalcular_prev()
        
        def guardar_fin_hora():
            global kpis_fin_hora_registrados, piezas_ok_registradas, piezas_nok_registradas, calidad_registrada
            try:
                try:
                    v_ok = int(entry_ok.get().strip())
                    v_nok = int(entry_nok.get().strip())
                    if v_ok < 0 or v_nok < 0:
                        messagebox.showerror("Error", "Los valores no pueden ser negativos.")
                        return
                except ValueError:
                    messagebox.showerror("Error", "Por favor ingrese números enteros válidos para OK y NO OK.")
                    return
                    
                tot = v_ok + v_nok
                cal = (v_ok / tot * 100.0) if tot > 0 else 100.0
                
                kpis_fin_hora_registrados = True
                piezas_ok_registradas = v_ok
                piezas_nok_registradas = v_nok
                calidad_registrada = cal
                
                # Actualizar visualmente la tarjeta de KPIs en la pantalla del operador
                if 'lbl_kpi_total' in globals() and lbl_kpi_total:
                    lbl_kpi_total.configure(text=str(tot))
                if 'lbl_kpi_ok' in globals() and lbl_kpi_ok:
                    lbl_kpi_ok.configure(text=str(v_ok))
                if 'lbl_kpi_nok' in globals() and lbl_kpi_nok:
                    lbl_kpi_nok.configure(text=str(v_nok))
                if 'lbl_kpi_calidad' in globals() and lbl_kpi_calidad:
                    lbl_kpi_calidad.configure(text=f"{cal:.1f}%")
                
                # Registrar en historial de producción
                hora_actual_str = datetime.datetime.now().strftime("%H:%M:%S")
                fecha_actual_str = datetime.datetime.now().strftime("%d/%m/%Y")
                dur_str = lbl_op_session_timer.cget("text") if 'lbl_op_session_timer' in globals() and lbl_op_session_timer else "00:00:00"
                usr_str = usuario_actual.get("nombre", "Operador") if 'usuario_actual' in globals() and usuario_actual else "Operador"
                rec_str = config_actual.get("receta_activa", "Receta") if 'config_actual' in globals() and config_actual else "Receta"
                
                nuevo_registro = {
                    "hora": hora_actual_str,
                    "fecha": fecha_actual_str,
                    "usuario": usr_str,
                    "receta": rec_str,
                    "total": tot,
                    "ok": v_ok,
                    "nok": v_nok,
                    "calidad": f"{cal:.1f}%",
                    "duracion": dur_str
                }
                if 'REGISTROS_PRODUCCION' in globals() and isinstance(REGISTROS_PRODUCCION, list):
                    REGISTROS_PRODUCCION.insert(0, nuevo_registro)
                    
                lbl_estado.configure(
                    text=f"✓ Fin de hora registrado: Total={tot} | OK={v_ok} | NOK={v_nok} | Calidad={cal:.1f}%",
                    text_color=COLOR_OK
                )
                try:
                    dialogo.destroy()
                except Exception:
                    pass
                messagebox.showinfo(
                    "Fin de Hora Registrado", 
                    f"Registro guardado con éxito:\n\n• Piezas OK: {v_ok}\n• Piezas NOK: {v_nok}\n• Total: {tot}\n• Calidad: {cal:.1f}%"
                )
            except Exception as e_guardar:
                print(f"Error al guardar fin de hora: {e_guardar}")
                messagebox.showerror("Error al Guardar", f"Ocurrió un error al guardar el registro:\n{e_guardar}")
            
        btn_guardar = ctk.CTkButton(
            f_d_inner,
            text="💾  Guardar y Registrar Fin de Hora",
            font=(FONT_MAIN, 13, "bold"),
            fg_color=COLOR_KOSTAL_NAVY,
            hover_color=COLOR_KOSTAL_ACCENT,
            text_color="white",
            height=42,
            corner_radius=8,
            command=guardar_fin_hora
        )
        btn_guardar.pack(fill="x", padx=18, pady=(8, 10))
        
        # Enlazar teclas Enter para navegación rápida entre campos y guardado inmediato
        entry_nok.bind("<Return>", lambda e: (entry_ok.focus_set(), entry_ok.select_range(0, tk.END), "break")[2])
        entry_nok.bind("<KP_Enter>", lambda e: (entry_ok.focus_set(), entry_ok.select_range(0, tk.END), "break")[2])
        entry_ok.bind("<Return>", lambda e: (ocultar_teclado_nativo(), guardar_fin_hora(), "break")[2])
        entry_ok.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), guardar_fin_hora(), "break")[2])
        dialogo.bind("<Return>", lambda e: (ocultar_teclado_nativo(), guardar_fin_hora(), "break")[2])
        dialogo.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), guardar_fin_hora(), "break")[2])

        # Poner foco directo en el campo NOK para que el operador solo escriba el número
        def _reintentar_grab_fin_hora():
            try:
                if dialogo.winfo_exists():
                    dialogo.grab_set()
            except Exception:
                pass

        def _activar_modal_fin_hora():
            try:
                if not dialogo.winfo_exists():
                    return
                dialogo.deiconify()
                dialogo.lift()
                try:
                    dialogo.grab_set()
                except Exception:
                    # En X11/Wayland (Linux) si aún no es viewable, reintentar tras mapearse
                    try:
                        dialogo.after(120, _reintentar_grab_fin_hora)
                    except Exception:
                        pass
                entry_nok.focus_set()
                entry_nok.select_range(0, tk.END)
            except Exception:
                pass

        dialogo.update_idletasks()
        dialogo.after(60, _activar_modal_fin_hora)
        
    except Exception as ex:
        print(f"Error al abrir diálogo de fin de hora: {ex}")
        messagebox.showerror("Error", f"No se pudo abrir el registro de fin de hora: {ex}")

btn_fin_hora = ctk.CTkButton(
    hdr_mid,
    text="⏱️ Fin Hora",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    corner_radius=6,
    height=28,
    width=95,
    command=abrir_dialogo_fin_de_hora
)
btn_fin_hora.pack(side="left", padx=(0, 6), pady=8)

btn_tutorial = ctk.CTkButton(
    hdr_mid,
    text="📖 Tutorial",
    font=(FONT_MAIN, 10, "bold"),
    fg_color=COLOR_KOSTAL_ACCENT,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=28,
    width=95,
    command=lambda: abrir_tutorial_proceso(obligatorio=False)
)
btn_tutorial.pack(side="left", padx=(0, 10), pady=8)

lbl_hdr_conn_status = ctk.CTkLabel(
    hdr_mid,
    text="● PLC ONLINE" if plc_conectado else "○ SIMULACIÓN",
    font=(FONT_MAIN, 9, "bold"),
    fg_color="#ECFDF5" if plc_conectado else "#FFFBEB",
    text_color="#065F46" if plc_conectado else "#B45309",
    corner_radius=4,
    padx=7,
    pady=2
)
lbl_hdr_conn_status.pack(side="left", padx=(0, 6))

def calar_conexion_plc():
    """Prueba la conexión real con el PLC WAGO y permite reconectar en caliente"""
    def _tarea_test():
        try:
            btn_calar_plc.configure(text="⏳ Calando...", state="disabled")
        except Exception:
            pass
        
        exito = False
        if plc_conectado and cliente:
            try:
                val = nodos["LISTO"].get_value()
                exito = True
            except Exception:
                exito = False
        
        if not exito:
            exito = intentar_conectar_plc(timeout_seg=2.0)
            
        def _actualizar_ui():
            try:
                btn_calar_plc.configure(state="normal")
                if exito:
                    lbl_hdr_conn_status.configure(
                        text="● PLC ONLINE",
                        fg_color="#ECFDF5",
                        text_color="#065F46"
                    )
                    btn_calar_plc.configure(
                        text="✓ PLC OK",
                        fg_color="#059669",
                        hover_color="#047857"
                    )
                    lbl_estado.configure(
                        text="✓ Enlace con PLC WAGO (192.168.1.69:4840) activo y respondiendo.",
                        text_color=COLOR_OK
                    )
                    ventana.after(2500, lambda: btn_calar_plc.configure(
                        text="🔌 Calar PLC",
                        fg_color="#0284C7",
                        hover_color="#0369A1"
                    ))
                else:
                    lbl_hdr_conn_status.configure(
                        text="○ SIMULACIÓN",
                        fg_color="#FFFBEB",
                        text_color="#B45309"
                    )
                    btn_calar_plc.configure(
                        text="✗ Sin PLC",
                        fg_color="#DC2626",
                        hover_color="#B91C1C"
                    )
                    lbl_estado.configure(
                        text="✗ Sin respuesta de PLC WAGO (192.168.1.69:4840) — Modo Simulación.",
                        text_color=COLOR_NOK
                    )
                    ventana.after(2500, lambda: btn_calar_plc.configure(
                        text="🔌 Calar PLC",
                        fg_color="#0284C7",
                        hover_color="#0369A1"
                    ))
            except Exception:
                pass
                
        ventana.after(10, _actualizar_ui)

    import threading
    threading.Thread(target=_tarea_test, daemon=True).start()

btn_calar_plc = ctk.CTkButton(
    hdr_mid,
    text="🔌 Calar PLC",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#0284C7",
    hover_color="#0369A1",
    text_color="#FFFFFF",
    corner_radius=6,
    height=28,
    width=92,
    command=calar_conexion_plc
)
btn_calar_plc.pack(side="left", padx=(0, 10), pady=8)

# Bloque Derecho: Usuario, Rol, Idioma y Cerrar Sesión (Imagen 5)
hdr_right = ctk.CTkFrame(header_hmi, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD, height=44)
hdr_right.pack(side="right", fill="y", padx=(8, 0))

f_u_top = ctk.CTkFrame(hdr_right, fg_color="transparent")
f_u_top.pack(side="left", padx=(12, 8), pady=6)

lbl_hdr_user_name = ctk.CTkLabel(f_u_top, text="Carlos Hernández", font=(FONT_MAIN, 11, "bold"), text_color=COLOR_KOSTAL_NAVY)
lbl_hdr_user_name.pack(anchor="w")

lbl_hdr_role_badge = ctk.CTkLabel(
    f_u_top, 
    text="ADMINISTRADOR", 
    font=(FONT_MAIN, 8, "bold"), 
    fg_color=COLOR_PURPLE_SOFT, 
    text_color=COLOR_PURPLE_TEXT, 
    corner_radius=4,
    padx=6, 
    pady=1
)
lbl_hdr_role_badge.pack(anchor="w", pady=(1, 0))

# Selector de idioma interactivo ES / EN
f_lang_pill = ctk.CTkFrame(hdr_right, fg_color=BG_INPUT, corner_radius=6, border_width=1, border_color=BORDER_LIGHT)
f_lang_pill.pack(side="left", padx=6, pady=8)

btn_lang_es = ctk.CTkButton(
    f_lang_pill,
    text="ES",
    width=32,
    height=24,
    font=(FONT_MAIN, 10, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    text_color="white",
    corner_radius=4,
    command=lambda: cambiar_idioma("ES")
)
btn_lang_es.pack(side="left", padx=2, pady=2)

btn_lang_en = ctk.CTkButton(
    f_lang_pill,
    text="EN",
    width=32,
    height=24,
    font=(FONT_MAIN, 10, "bold"),
    fg_color="transparent",
    hover_color="#E2E8F0",
    text_color=TEXT_MUTED,
    corner_radius=4,
    command=lambda: cambiar_idioma("EN")
)
btn_lang_en.pack(side="left", padx=2, pady=2)

lbl_hdr_session_timer = ctk.CTkLabel(hdr_right, text="00:00:00", font=(FONT_MONO, 10, "bold"), text_color=TEXT_MUTED)

btn_logout_top = ctk.CTkButton(
    hdr_right,
    text="⏻ Cerrar Sesión",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#FEE2E2",
    hover_color="#FECACA",
    text_color="#DC2626",
    corner_radius=6,
    width=105,
    height=28,
    command=cerrar_sesion
)
btn_logout_top.pack(side="left", padx=(4, 6), pady=8)

btn_kb_top = ctk.CTkButton(
    hdr_right,
    text="⌨",
    font=(FONT_MAIN, 13, "bold"),
    fg_color="#F1F5F9",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=32,
    height=28,
    command=alternar_teclado_tactil
)
btn_kb_top.pack(side="left", padx=(0, 4), pady=8)

btn_min_top = ctk.CTkButton(
    hdr_right,
    text="🗕",
    font=(FONT_MAIN, 12, "bold"),
    fg_color="#F1F5F9",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=32,
    height=28,
    command=minimizar_ventana
)
btn_min_top.pack(side="left", padx=(0, 4), pady=8)

btn_fs_top = ctk.CTkButton(
    hdr_right,
    text="⛶",
    font=(FONT_MAIN, 12, "bold"),
    fg_color="#F1F5F9",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=32,
    height=28,
    command=alternar_pantalla_completa
)
btn_fs_top.pack(side="left", padx=(0, 10), pady=8)

body_grid = ctk.CTkFrame(frame_operador, fg_color=BG_MAIN, corner_radius=0)
body_grid.pack(fill="both", expand=True, padx=12, pady=10)

# Columna 0 (Proceso): Ancho optimizado y más compacto
body_grid.columnconfigure(0, weight=20)
# Columna 1 (Matriz de Piezas): Mayor amplitud visual
body_grid.columnconfigure(1, weight=54)
# Columna 2 (JOG y Mandos): Espacio ergonómico para botones
body_grid.columnconfigure(2, weight=26)
body_grid.rowconfigure(0, weight=1)

def crear_card_moderna(parent, titulo=None, btn_widget=None):
    c = ctk.CTkFrame(parent, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color="#CBD5E1")
    if titulo:
        hdr = ctk.CTkFrame(c, fg_color="#F1F5F9", corner_radius=6, height=36)
        hdr.pack(fill="x", padx=1, pady=1)
        lbl_t = ctk.CTkLabel(
            hdr, 
            text=titulo.upper(), 
            font=(FONT_MAIN, 11, "bold"), 
            text_color="#1E3A8A"
        )
        lbl_t.pack(side="left", padx=14, pady=6)
        c._header_frame = hdr
        c._header_title = lbl_t
        if btn_widget:
            btn_w = btn_widget(hdr)
            btn_w.pack(side="right", padx=8, pady=4)
            c._header_btn = btn_w
    return c

# ==============================================================================
# COLUMNA 1 (IZQUIERDA): PROCESO + MANDOS (PARO, INICIO, RESET, PINTAR)
# ==============================================================================
col_1 = ctk.CTkFrame(body_grid, fg_color="transparent")
col_1.grid(row=0, column=0, sticky="nsew", padx=6)

# 1. Información de Proceso
card_proc = crear_card_moderna(col_1, "Información de Proceso")
card_proc.pack(fill="x", pady=(0, 6))

f_proc_b = ctk.CTkFrame(card_proc, fg_color="transparent")
f_proc_b.pack(fill="x", padx=14, pady=8)

def fila_info(parent, label, val, c_val=TEXT_PRIMARY, mono=False):
    f = ctk.CTkFrame(parent, fg_color="transparent")
    f.pack(fill="x", pady=2)
    ctk.CTkLabel(f, text=label, font=(FONT_MAIN, 10), text_color=TEXT_SECONDARY).pack(side="left")
    lbl_v = ctk.CTkLabel(f, text=str(val), font=(FONT_MONO if mono else FONT_MAIN, 10, "bold"), text_color=c_val)
    lbl_v.pack(side="right")
    return lbl_v

# Selector de recetas definidas en Admin (Agrandado para uso táctil cómodo)
f_sel_rec = ctk.CTkFrame(f_proc_b, fg_color="transparent")
f_sel_rec.pack(fill="x", pady=(2, 6))

f_sel_rec_hdr = ctk.CTkFrame(f_sel_rec, fg_color="transparent")
f_sel_rec_hdr.pack(fill="x", pady=(0, 3))

lbl_rec_title = ctk.CTkLabel(f_sel_rec_hdr, text="Receta / Jig Activo:", font=(FONT_MAIN, 12, "bold"), text_color=COLOR_KOSTAL_NAVY)
lbl_rec_title.pack(side="left")

btn_ver_jigs = ctk.CTkButton(
    f_sel_rec_hdr,
    text="🔍 Ver Jigs",
    font=(FONT_MAIN, 10, "bold"),
    width=74,
    height=24,
    fg_color="#EFF6FF",
    hover_color="#DBEAFE",
    text_color="#1E40AF",
    border_width=1,
    border_color="#BFDBFE",
    corner_radius=4,
    command=lambda: abrir_dialogo_seleccion_jig()
)
btn_ver_jigs.pack(side="right")

lista_nom_recetas = [r.get("nombre", "") for r in config_actual.get("recetas", [])] or ["Receta Base"]
receta_activa_nom = config_actual.get("receta_activa", lista_nom_recetas[0])
rec_obj_activa = next((r for r in config_actual.get("recetas", []) if r.get("nombre") == receta_activa_nom), config_actual["recetas"][0] if config_actual.get("recetas") else {})

# Selector desplegable nativo de recetas (Agrandado para uso táctil ergonómico)
f_opt_border = ctk.CTkFrame(f_sel_rec, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color="#CBD5E1")
f_opt_border.pack(fill="x")

option_receta = ctk.CTkOptionMenu(
    f_opt_border,
    values=lista_nom_recetas,
    command=lambda val: cargar_receta_por_nombre(val),
    fg_color="#FFFFFF",
    button_color="#F1F5F9",
    button_hover_color="#E2E8F0",
    text_color="#1E3A8A",
    dropdown_fg_color="#FFFFFF",
    dropdown_text_color="#0F172A",
    dropdown_hover_color="#EFF6FF",
    corner_radius=8,
    height=46,
    font=(FONT_MAIN, 13, "bold"),
    dropdown_font=(FONT_MAIN, 12, "bold")
)
option_receta.set(receta_activa_nom)
option_receta.pack(fill="x", padx=1, pady=1)

# Contenedor Información Normal de Proceso (fijo y permanente)
f_proc_info_normal = ctk.CTkFrame(f_proc_b, fg_color="transparent")
f_proc_info_normal.pack(fill="x")

# Card de Detalles de la Pieza e Imagen de Referencia (dentro de f_proc_info_normal)
f_pieza_preview = ctk.CTkFrame(f_proc_info_normal, fg_color="#F8FAFC", corner_radius=8, border_width=1, border_color="#E2E8F0")
f_pieza_preview.pack(fill="x", pady=4)

f_pz_txt = ctk.CTkFrame(f_pieza_preview, fg_color="transparent")
f_pz_txt.pack(side="left", fill="both", expand=True, padx=8, pady=4)

lbl_proc_pieza = fila_info(f_pz_txt, "Pieza:", rec_obj_activa.get("pieza", "Bisel Frontal"), COLOR_KOSTAL_NAVY)
lbl_proc_modelo = fila_info(f_pz_txt, "Modelo:", rec_obj_activa.get("modelo", "KSL-001"), TEXT_PRIMARY)
lbl_proc_matriz_dim = fila_info(f_pz_txt, "Matriz Fixture:", f"{matriz_filas} × {matriz_cols} ({obtener_piezas_por_jig_actual()} Piezas/Jig)", mono=True)
lbl_proc_matriz_dist = fila_info(f_pz_txt, "Dist. C-C:", f"X:{matriz_delta_x:.1f} Y:{matriz_delta_y:.1f} mm", mono=True)

# Thumbnail de la pieza
f_img_pieza_box = ctk.CTkFrame(f_pieza_preview, fg_color="#FFFFFF", corner_radius=6, border_width=1, border_color="#CBD5E1", width=95, height=76)
f_img_pieza_box.pack(side="right", padx=6, pady=4)
f_img_pieza_box.pack_propagate(False)

lbl_proc_img_pieza = ctk.CTkLabel(f_img_pieza_box, text="[Sin imagen]", font=(FONT_MAIN, 8), text_color=TEXT_MUTED)
lbl_proc_img_pieza.pack(expand=True, fill="both")

actualizar_imagen_pieza_proceso(rec_obj_activa.get("imagen", ""))

lbl_piezas_realizadas = fila_info(f_proc_info_normal, "Piezas Realizadas:", f"00 / {matriz_total:02d}", COLOR_KOSTAL_NAVY, mono=True)

f_cero_ind = ctk.CTkFrame(
    f_proc_info_normal, 
    fg_color="#ECFDF5" if cero_ha_sido_seteado else "#FEF3C7", 
    corner_radius=6, 
    border_width=1, 
    border_color="#A7F3D0" if cero_ha_sido_seteado else "#FDE68A"
)
f_cero_ind.pack(fill="x", pady=4)
lbl_indicador_set0 = ctk.CTkLabel(
    f_cero_ind, 
    text=f"✓ CERO LISTO: X={cero_guardado_x:.2f}  Y={cero_guardado_y:.2f} mm" if cero_ha_sido_seteado else "⚠️ SET 0 PENDIENTE: Calibre con JOG y presione 'Set 0'",
    font=(FONT_MONO, 10, "bold"),
    text_color="#065F46" if cero_ha_sido_seteado else "#B45309",
    wraplength=250,
    justify="center"
)
lbl_indicador_set0.pack(pady=4)

f_ses = ctk.CTkFrame(f_proc_info_normal, fg_color="transparent")
f_ses.pack(fill="x", pady=2)
ctk.CTkLabel(f_ses, text="Tiempo Sesión:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_SECONDARY).pack(side="left")
lbl_op_session_timer = ctk.CTkLabel(f_ses, text="00:00:00", font=(FONT_MONO, 13, "bold"), text_color=COLOR_KOSTAL_ACCENT)
lbl_op_session_timer.pack(side="right")

f_kpis_box = ctk.CTkFrame(f_proc_info_normal, fg_color=BG_INPUT, corner_radius=8, border_width=1, border_color=BORDER_LIGHT)
f_kpis_box.pack(fill="x", pady=4)

def crear_kpi_item(parent, lbl_txt, val_init, c_txt):
    kb = ctk.CTkFrame(parent, fg_color="transparent")
    kb.pack(side="left", expand=True, fill="both", pady=6)
    ctk.CTkLabel(kb, text=lbl_txt, font=(FONT_MAIN, 10, "bold"), text_color=TEXT_MUTED).pack()
    lbl_v = ctk.CTkLabel(kb, text=val_init, font=(FONT_MONO, 15, "bold"), text_color=c_txt)
    lbl_v.pack(pady=(2, 0))
    return lbl_v

lbl_kpi_total   = crear_kpi_item(f_kpis_box, "TOTAL", "0", TEXT_PRIMARY)
lbl_kpi_ok      = crear_kpi_item(f_kpis_box, "OK", "0", COLOR_OK)
lbl_kpi_nok     = None
lbl_kpi_calidad = None

btn_fin_de_hora = ctk.CTkButton(
    f_proc_info_normal,
    text="⏱️  Registrar Fin de Hora",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    height=34,
    corner_radius=6,
    command=abrir_dialogo_fin_de_hora
)
btn_fin_de_hora.pack(fill="x", pady=(3, 6))

# 2. Control Principal: Botón Maestro de Pintado (Diseño Industrial Ergonómico)
card_marcha = crear_card_moderna(col_1, "Control Principal de Pintado")
card_marcha.pack(fill="both", expand=True, pady=(6, 0))

f_marcha_b = ctk.CTkFrame(card_marcha, fg_color="transparent")
f_marcha_b.pack(fill="both", expand=True, padx=10, pady=8)

btn_pintar = ctk.CTkButton(
    f_marcha_b,
    text="🎨\nINICIO DE\nPINTADO\n[OFF]",
    font=(FONT_MAIN, 22, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    corner_radius=18,
    border_width=3,
    border_color="#CBD5E1",
    command=alternar_pintar
)
btn_pintar.pack(fill="both", expand=True, padx=4, pady=(4, 6))

lbl_pintar_sub = ctk.CTkLabel(
    f_marcha_b,
    text="Toque para alternar ciclo automático",
    font=(FONT_MAIN, 10),
    text_color=TEXT_MUTED
)
lbl_pintar_sub.pack(side="bottom", pady=(2, 4))

# ==============================================================================
# COLUMNA 2 (CENTRAL): ESTADO DEL FIXTURE (3x5) + DIAGNÓSTICO
# ==============================================================================
col_2 = ctk.CTkFrame(body_grid, fg_color="transparent")
col_2.grid(row=0, column=1, sticky="nsew", padx=6)

card_fixture = crear_card_moderna(
    col_2, 
    f"Estado del Fixture (Matriz {matriz_filas} × {matriz_cols})",
    btn_widget=lambda h: ctk.CTkButton(
        h, 
        text="🛠️ Recetario Admin", 
        width=130, 
        height=24, 
        font=(FONT_MAIN, 10, "bold"), 
        fg_color=COLOR_KOSTAL_NAVY, 
        hover_color=COLOR_KOSTAL_ACCENT, 
        text_color="white", 
        corner_radius=4, 
        command=lambda: (ir_a_panel_admin(), cambiar_modulo_admin("recetas"))
    )
)
card_fixture.pack(fill="both", expand=True, pady=(0, 6))

# Banner de Dimensiones y Distancia Centro a Centro
f_matriz_info_bar = ctk.CTkFrame(card_fixture, fg_color=BG_INPUT, corner_radius=6, border_width=1, border_color=BORDER_LIGHT)
f_matriz_info_bar.pack(fill="x", padx=8, pady=(4, 2))

lbl_matriz_dim_badge = ctk.CTkLabel(
    f_matriz_info_bar,
    text=f"📐 MATRIZ: {matriz_filas} × {matriz_cols} ({matriz_total} PTS)",
    font=(FONT_MAIN, 10, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    text_color="white",
    corner_radius=4,
    padx=8,
    pady=2
)
lbl_matriz_dim_badge.pack(side="left", padx=4, pady=3)

lbl_ciclo_badge = ctk.CTkLabel(
    f_matriz_info_bar,
    text=f"🏁 CICLOS: {contador_ciclos_completados}",
    font=(FONT_MAIN, 10, "bold"),
    fg_color=COLOR_PURPLE_SOFT,
    text_color=COLOR_PURPLE_TEXT,
    corner_radius=4,
    padx=8,
    pady=2
)
lbl_ciclo_badge.pack(side="left", padx=4, pady=3)

lbl_matriz_dist_badge = ctk.CTkLabel(
    f_matriz_info_bar,
    text=f"↔ X: {matriz_delta_x:.1f} mm  |  ↕ Y: {matriz_delta_y:.1f} mm  |  ↗ Diag: {matriz_dist_diag:.1f} mm",
    font=(FONT_MONO, 10, "bold"),
    text_color=COLOR_KOSTAL_ACCENT
)
lbl_matriz_dist_badge.pack(side="left", padx=8, pady=3)

coordenadas_visibles_map = {i: True for i in range(100)}
coords_visibles_todas = True

def alternar_todas_coordenadas():
    global coords_visibles_todas
    coords_visibles_todas = not coords_visibles_todas
    for i in range(matriz_total):
        coordenadas_visibles_map[i] = coords_visibles_todas
    if coords_visibles_todas:
        btn_toggle_coords.configure(fg_color=COLOR_KOSTAL_NAVY)
    else:
        btn_toggle_coords.configure(fg_color="#64748B")
    dibujar_fixture_moderno()
    estado_str = "visibles" if coords_visibles_todas else "ocultas"
    lbl_estado.configure(text=f"Coordenadas de la matriz: {estado_str}", text_color=COLOR_KOSTAL_ACCENT)

btn_toggle_coords = ctk.CTkButton(
    f_matriz_info_bar,
    text="Coords",
    font=(FONT_MAIN, 9, "bold"),
    width=75,
    height=24,
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    corner_radius=4,
    command=alternar_todas_coordenadas
)
btn_toggle_coords.pack(side="right", padx=6, pady=3)

def alternar_patron_pintado():
    global config_actual
    cur = config_actual.get("patron_pintado", "vertical")
    nuevo = "horizontal" if cur == "vertical" else "vertical"
    config_actual["patron_pintado"] = nuevo
    guardar_configuracion()
    actualizar_boton_patron()
    actualizar_coordenadas_matriz()
    dibujar_fixture_moderno()
    estado_str = "Vertical (columnas ↓)" if nuevo == "vertical" else "Horizontal (filas →)"
    lbl_estado.configure(text=f"Patrón de pintado configurado a: {estado_str}", text_color=COLOR_KOSTAL_ACCENT)

def actualizar_boton_patron():
    if 'btn_toggle_patron' in globals() and btn_toggle_patron:
        p = config_actual.get("patron_pintado", "vertical")
        if p == "vertical":
            btn_toggle_patron.configure(text="⬇️ Vertical", fg_color=COLOR_KOSTAL_NAVY)
        else:
            btn_toggle_patron.configure(text="➡️ Horizontal", fg_color="#64748B")

btn_toggle_patron = ctk.CTkButton(
    f_matriz_info_bar,
    text="⬇️ Vertical",
    font=(FONT_MAIN, 9, "bold"),
    width=85,
    height=24,
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    corner_radius=4,
    command=alternar_patron_pintado
)
btn_toggle_patron.pack(side="right", padx=(0, 4), pady=3)

# ------------------------------------------------------------------------------
# BARRA DE TELEMETRÍA Y RECEPCIÓN PLC EN VIVO (DIAGNÓSTICO & PRUEBAS DE MOVIMIENTO)
# ------------------------------------------------------------------------------
f_plc_telemetria_bar = ctk.CTkFrame(card_fixture, fg_color="#F8FAFC", corner_radius=6, border_width=1, border_color="#CBD5E1")
f_plc_telemetria_bar.pack(fill="x", padx=8, pady=(2, 3))

f_tel_izq = ctk.CTkFrame(f_plc_telemetria_bar, fg_color="transparent")
f_tel_izq.pack(side="left", padx=8, pady=3)

_init_p_x = matriz_coordenadas[0][0] if matriz_coordenadas else 0.0
_init_p_y = matriz_coordenadas[0][1] if matriz_coordenadas else 0.0

lbl_tel_destino = ctk.CTkLabel(
    f_tel_izq,
    text=f"🎯 DESTINO P01: X={_init_p_x:.2f} mm | Y={_init_p_y:.2f} mm",
    font=(FONT_MONO, 10, "bold"),
    text_color="#1E3A8A"
)
lbl_tel_destino.pack(anchor="w")

lbl_tel_plc_rx = ctk.CTkLabel(
    f_tel_izq,
    text="📡 PLC RECIBIÓ: POS_X=--- mm | POS_Y=--- mm",
    font=(FONT_MONO, 9, "bold"),
    text_color="#334155"
)
lbl_tel_plc_rx.pack(anchor="w")

f_tel_cen = ctk.CTkFrame(f_plc_telemetria_bar, fg_color="transparent")
f_tel_cen.pack(side="left", padx=10, pady=3, expand=True)

lbl_tel_status = ctk.CTkLabel(
    f_tel_cen,
    text="🟢 VERIFICANDO CONEXIÓN CON PLC...",
    font=(FONT_MAIN, 9, "bold"),
    fg_color=COLOR_OK_SOFT,
    text_color=COLOR_OK_TEXT,
    corner_radius=4,
    padx=8,
    pady=2
)
lbl_tel_status.pack()

lbl_tel_interlock = ctk.CTkLabel(
    f_tel_cen,
    text="Interlocks: Verificando estados de máquina...",
    font=(FONT_MAIN, 8, "bold"),
    text_color=TEXT_MUTED,
    wraplength=420,
    justify="center"
)
lbl_tel_interlock.pack(pady=(1, 0))

def forzar_prueba_movimiento_actual():
    if not estado_manual:
        lbl_estado.configure(
            text="⚠️ MODO MANUAL REQUERIDO: Active el 'MODO MANUAL' para mover piezas.",
            text_color=COLOR_WARN
        )
        return
    idx = indice_matriz_actual if 0 <= indice_matriz_actual < len(matriz_coordenadas) else 0
    probar_enviar_pieza_especifica(idx)

btn_probar_mov = ctk.CTkButton(
    f_plc_telemetria_bar,
    text="⚡ Probar Movimiento",
    font=(FONT_MAIN, 9, "bold"),
    width=140,
    height=26,
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    corner_radius=4,
    command=forzar_prueba_movimiento_actual
)
btn_probar_mov.pack(side="right", padx=6, pady=3)

canvas_fixture = tk.Canvas(card_fixture, bg="#FFFFFF", highlightthickness=0, height=235)
canvas_fixture.pack(fill="both", expand=True, padx=8, pady=4)

coordenadas_pill_bboxes = {}

def al_click_en_pieza_fixture(event):
    """Permite hacer clic sobre una tarjeta para enviar su coordenada al PLC únicamente en MODO MANUAL"""
    try:
        filas = max(1, matriz_filas)
        cols = max(1, matriz_cols)
        total_piezas = matriz_total
        w = canvas_fixture.winfo_width()
        h = canvas_fixture.winfo_height()
        if w < 50: w = 480
        if h < 50: h = 210
        gap_x = max(2, min(8, int(30 / cols)))
        gap_y = max(2, min(8, int(30 / filas)))
        pad_x = max(4, min(10, int(40 / cols)))
        pad_y = max(4, min(8, int(30 / filas)))
        card_w = max(12.0, (w - 2 * pad_x - (cols - 1) * gap_x) / cols)
        card_h = max(12.0, (h - 2 * pad_y - (filas - 1) * gap_y) / filas)
        
        patron = config_actual.get("patron_pintado", "vertical")
        for i in range(total_piezas):
            if patron == "vertical":
                c = i // filas
                r_idx = i % filas
                r = r_idx if (c % 2 == 0) else (filas - 1 - r_idx)
            else:
                r = i // cols
                c = i % cols
            x1 = pad_x + c * (card_w + gap_x)
            y1 = pad_y + r * (card_h + gap_y)
            x2 = x1 + card_w
            y2 = y1 + card_h
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if not estado_manual:
                    lbl_estado.configure(
                        text="⚠️ MODO MANUAL REQUERIDO: Active el 'MODO MANUAL' para enviar piezas al PLC.",
                        text_color=COLOR_WARN
                    )
                    return
                probar_enviar_pieza_especifica(i)
                break
    except Exception:
        pass

canvas_fixture.bind("<Button-1>", al_click_en_pieza_fixture)

# Estado dinámico de las piezas de la matriz (inicializado en 0 procesadas)
fixture_piezas_estado = ["pendiente"] * matriz_total

def actualizar_kpis_fixture():
    global total_piezas_sesion_acum
    ok_cnt = sum(1 for s in fixture_piezas_estado if s == "ok")
    
    # Requisito: El total de piezas acumula el "Número de Piezas por Jig" cada vez que termina un ciclo de jig
    if 'lbl_kpi_total' in globals() and lbl_kpi_total:
        lbl_kpi_total.configure(text=str(total_piezas_sesion_acum))
        
    if 'lbl_piezas_realizadas' in globals() and lbl_piezas_realizadas:
        lbl_piezas_realizadas.configure(text=f"{ok_cnt:02d} / {matriz_total:02d}")
        
    if 'lbl_kpi_ok' in globals() and lbl_kpi_ok:
        val_ok = max(0, total_piezas_sesion_acum - piezas_nok_registradas) if kpis_fin_hora_registrados else total_piezas_sesion_acum
        lbl_kpi_ok.configure(text=str(val_ok))

f_leg = ctk.CTkFrame(card_fixture, fg_color="transparent")
f_leg.pack(fill="x", padx=10, pady=(2, 6))
for colr, txt in [
    ("#94A3B8", "Pendiente"),
    ("#2563EB", "Activa"),
    (COLOR_OK, "Procesada")
]:
    fi = ctk.CTkFrame(f_leg, fg_color="transparent")
    fi.pack(side="left", padx=5)
    ctk.CTkLabel(fi, text="●", font=(FONT_MAIN, 13), text_color=colr).pack(side="left")
    ctk.CTkLabel(fi, text=f" {txt}", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).pack(side="left")

ctk.CTkLabel(
    f_leg,
    text="Clic en tarjeta para enviar a PLC (Solo en Modo Manual)",
    font=(FONT_MAIN, 10, "bold"),
    text_color=COLOR_KOSTAL_ACCENT
).pack(side="right", padx=6)

def dibujar_fixture_moderno():
    canvas_fixture.delete("all")
    w = canvas_fixture.winfo_width()
    h = canvas_fixture.winfo_height()
    if w < 50: w = 480
    if h < 50: h = 210
    
    filas = max(1, matriz_filas)
    cols = max(1, matriz_cols)
    total_piezas = matriz_total
    
    gap_x = max(2, min(8, int(30 / cols)))
    gap_y = max(2, min(8, int(30 / filas)))
    pad_x = max(4, min(10, int(40 / cols)))
    pad_y = max(4, min(8, int(30 / filas)))
    
    card_w = max(12.0, (w - 2 * pad_x - (cols - 1) * gap_x) / cols)
    card_h = max(12.0, (h - 2 * pad_y - (filas - 1) * gap_y) / filas)
    
    # Receta activa e imagen para sustituir los pequeños círculos por la imagen de la pieza
    receta_activa = next((r for r in config_actual.get("recetas", []) if r.get("nombre") == config_actual.get("receta_activa")), None)
    ruta_img = receta_activa.get("imagen", "") if receta_activa else ""
    
    rad = max(2, min(10, int(card_h * 0.18)))
    
    patron = config_actual.get("patron_pintado", "vertical")
    for i in range(total_piezas):
        if patron == "vertical":
            c = i // filas
            r_idx = i % filas
            r = r_idx if (c % 2 == 0) else (filas - 1 - r_idx)
        else:
            r = i // cols
            c = i % cols
        x1 = pad_x + c * (card_w + gap_x)
        y1 = pad_y + r * (card_h + gap_y)
        x2 = x1 + card_w
        y2 = y1 + card_h
        
        st = fixture_piezas_estado[i] if i < len(fixture_piezas_estado) else "pendiente"
        cx, cy = matriz_coordenadas[i] if i < len(matriz_coordenadas) else (0.0, 0.0)
        
        # Colores idénticos al Mockup Oficial KOSTAL (Sin punto ni fondo rojo NOK en matriz)
        if st == "activa":
            c_bg = "#EFF6FF"      # Azul claro de selección
            c_border = "#2563EB"  # Azul vivo de contorno
            c_txt = "#1E3A8A"     # Azul marino
            bw = 2
        elif st in ("ok", "nok"):
            c_bg = "#F0FDF4"      # Verde pastel
            c_border = "#10B981"  # Verde esmeralda OK
            c_txt = "#065F46"     # Verde texto
            bw = 2
        else:
            c_bg = "#FFFFFF"      # Blanco puro pendiente
            c_border = "#CBD5E1"  # Borde sutil industrial
            c_txt = "#334155"     # Gris carbón
            bw = 1
            
        # Dibujar tarjeta redondeada con polígono suave
        pts = [
            x1 + rad, y1,
            x2 - rad, y1,
            x2, y1,
            x2, y1 + rad,
            x2, y2 - rad,
            x2, y2,
            x2 - rad, y2,
            x1 + rad, y2,
            x1, y2,
            x1, y2 - rad,
            x1, y1 + rad,
            x1, y1
        ]
        canvas_fixture.create_polygon(pts, smooth=True, fill=c_bg, outline=c_border, width=bw)
        
        # 1. Número secuencial y coordenadas a la izquierda
        es_visible_coord = coordenadas_visibles_map.get(i, coords_visibles_todas)
        num_x = x1 + card_w * 0.26
        
        if es_visible_coord:
            num_y = y1 + card_h * 0.25
            font_sz = max(9, min(13, int(card_h * 0.24)))
            canvas_fixture.create_text(
                num_x, num_y,
                text=f"{i+1:02d}",
                font=(FONT_MAIN, font_sz, "bold"),
                fill=c_txt
            )
            
            # 2. Coordenadas a las que se envía este punto (X e Y) debajo del número
            coord_x = num_x
            coord_y = y1 + card_h * 0.69
            
            pill_w = card_w * 0.44
            pill_h = card_h * 0.40
            px1 = coord_x - pill_w / 2
            py1 = coord_y - pill_h / 2
            px2 = coord_x + pill_w / 2
            py2 = coord_y + pill_h / 2
            
            if st == "activa":
                c_pbg, c_pbd, c_ptxt = "#DBEAFE", "#93C5FD", "#1E40AF"
            elif st in ("ok", "nok"):
                c_pbg, c_pbd, c_ptxt = "#DCFCE7", "#86EFAC", "#065F46"
            else:
                c_pbg, c_pbd, c_ptxt = "#F1F5F9", "#CBD5E1", "#475569"
                
            canvas_fixture.create_rectangle(px1, py1, px2, py2, fill=c_pbg, outline=c_pbd, width=1)
            font_sz_c = max(7, min(9, int(card_h * 0.16)))
            canvas_fixture.create_text(
                coord_x, coord_y,
                text=f"X:{cx:.1f}\nY:{cy:.1f}",
                font=(FONT_MONO, font_sz_c, "bold"),
                fill=c_ptxt,
                justify="center"
            )
        else:
            # Al estar ocultas, el número se centra con tipografía amplia y clara
            num_y = y1 + card_h * 0.50
            font_sz = max(11, min(16, int(card_h * 0.34)))
            canvas_fixture.create_text(
                num_x, num_y,
                text=f"{i+1:02d}",
                font=(FONT_MAIN, font_sz, "bold"),
                fill=c_txt
            )
        
        # 3. Ícono / Imagen de la pieza a la derecha
        icon_x = x2 - card_w * 0.28
        icon_y = y1 + card_h * 0.50
        d_icon = int(min(card_w * 0.38, card_h * 0.64))
        d_icon = max(14, d_icon)
        
        img_circ = obtener_imagen_circular_pieza(ruta_img, d_icon, st) if ruta_img else None
        if img_circ:
            canvas_fixture.create_image(icon_x, icon_y, image=img_circ)
        else:
            # Símbolo vectorial fluido estilo gota / pieza oficial KOSTAL (Imagen 5)
            ir = d_icon / 2.0
            if st == "activa":
                dfill, dout = "#3B82F6", "#1D4ED8"
            elif st in ("ok", "nok"):
                dfill, dout = "#10B981", "#047857"
            else:
                dfill, dout = "#E2E8F0", "#94A3B8"
            
            canvas_fixture.create_oval(
                icon_x - ir * 0.65, icon_y - ir * 0.3,
                icon_x + ir * 0.65, icon_y + ir * 0.85,
                fill=dfill, outline=dout, width=1
            )
            canvas_fixture.create_polygon(
                icon_x - ir * 0.5, icon_y,
                icon_x, icon_y - ir * 0.9,
                icon_x + ir * 0.5, icon_y,
                fill=dfill, outline=dout, smooth=True
            )
            
        # 4. Marcador de estado y recepción PLC en esquina superior derecha (sin rojo NOK)
        badge_x = x2 - 8
        badge_y = y1 + 9
        if st == "activa":
            recibida_plc = ultimo_plc_rx_ok if 'ultimo_plc_rx_ok' in globals() else True
            n_p = int(config_actual.get("pasadas_tampo", 2))
            if n_p == 2 and 'estado_pintar' in globals() and estado_pintar:
                tag_txt = f"▶ P{pasada_actual_pieza}/2"
            else:
                tag_txt = "▶ RX✓" if recibida_plc else "▶ TX"
            canvas_fixture.create_text(badge_x - 6, badge_y, text=tag_txt, font=(FONT_MAIN, 8, "bold"), fill="#2563EB")
        elif st in ("ok", "nok"):
            canvas_fixture.create_text(badge_x, badge_y, text="✓", font=(FONT_MAIN, 9, "bold"), fill=COLOR_OK)

# Redimensionamiento suave (con debounce de 25ms para evitar trabas o jank)
_fixture_resize_timer = None
def al_redimensionar_fixture(event=None):
    global _fixture_resize_timer
    if _fixture_resize_timer:
        try:
            ventana.after_cancel(_fixture_resize_timer)
        except Exception:
            pass
    _fixture_resize_timer = ventana.after(25, dibujar_fixture_moderno)

canvas_fixture.bind("<Configure>", al_redimensionar_fixture)

# 2. Diagnóstico y Señales del PLC WAGO (Diseño Compacto y Ergonómico)
card_diag = crear_card_moderna(col_2, "Diagnóstico & Señales del PLC WAGO")
card_diag.pack(fill="x", pady=(4, 2))

f_diag_b = ctk.CTkFrame(card_diag, fg_color="transparent")
f_diag_b.pack(fill="x", padx=10, pady=5)
f_diag_b.columnconfigure(0, weight=1)
f_diag_b.columnconfigure(1, weight=1)
f_diag_b.columnconfigure(2, weight=1)

def crear_pill_ctk(parent, r, c, txt_init, cspan=1):
    lbl = ctk.CTkLabel(
        parent,
        text=txt_init,
        font=(FONT_MAIN, 9, "bold"),
        fg_color="#F8FAFC",
        text_color="#334155",
        corner_radius=5,
        height=24
    )
    lbl.grid(row=r, column=c, columnspan=cspan, padx=2, pady=1, sticky="ew")
    return lbl

lbl_m_ene   = crear_pill_ctk(f_diag_b, 0, 0, "ENERGIZADOS", cspan=2)
lbl_listo   = crear_pill_ctk(f_diag_b, 0, 2, "LISTO")
lbl_m_set   = crear_pill_ctk(f_diag_b, 1, 0, "ORÍGENES LISTOS", cspan=2)
lbl_pintado = crear_pill_ctk(f_diag_b, 1, 2, "ESPERANDO PINTADO...")
lbl_alarm_x = crear_pill_ctk(f_diag_b, 2, 0, "ALARM X")
lbl_alarm_y = crear_pill_ctk(f_diag_b, 2, 1, "ALARM Y")
lbl_pe      = crear_pill_ctk(f_diag_b, 2, 2, "PE: OK")
lbl_sensor_x= crear_pill_ctk(f_diag_b, 3, 0, "SENSOR X")
lbl_sensor_y= crear_pill_ctk(f_diag_b, 3, 1, "SENSOR Y")
lbl_tampo   = crear_pill_ctk(f_diag_b, 3, 2, "TAMPOGRAFIADO: INACTIVO")
lbl_no_listo= crear_pill_ctk(f_diag_b, 4, 0, "ESTADO: NO LISTO", cspan=3)

# ==============================================================================
# COLUMNA 3 (DERECHA): OPERACIÓN & MOVIMIENTO MANUAL JOG (UNIFICADO)
# ==============================================================================
col_3 = ctk.CTkFrame(body_grid, fg_color="transparent")
col_3.grid(row=0, column=2, sticky="nsew", padx=6)

# Estado y control de pestaña retráctil para Operación & JOG
jog_pestana_retraida = False
btn_toggle_jog = None

def alternar_pestana_jog(event=None):
    global jog_pestana_retraida, btn_toggle_jog
    jog_pestana_retraida = not jog_pestana_retraida
    if jog_pestana_retraida:
        # Retraer toda la sección JOG en la pestaña superior
        f_jog_b.pack_forget()
        card_jog.pack_configure(fill="x", expand=False)
        if btn_toggle_jog:
            btn_toggle_jog.configure(
                text="▼ Desplegar" if idioma_actual == "ES" else "▼ Expand",
                fg_color=COLOR_KOSTAL_NAVY,
                hover_color=COLOR_KOSTAL_ACCENT,
                text_color="white"
            )
        if hasattr(card_jog, '_header_title'):
            card_jog._header_title.configure(text=DICCIONARIO_IDIOMAS[idioma_actual]["op_jog"])
        if hasattr(card_jog, '_header_frame'):
            card_jog._header_frame.configure(fg_color="#E2E8F0")

        # Escalar los mandos de control (INICIO, RESET, PARO) para llenar el espacio restante
        try:
            if 'card_mandos_der' in globals():
                card_mandos_der.pack_configure(fill="both", expand=True, pady=(6, 0))
            if 'f_mandos_der_b' in globals():
                f_mandos_der_b.pack_configure(fill="both", expand=True, padx=12, pady=12)
            if 'f_ir_row' in globals():
                f_ir_row.pack_configure(fill="both", expand=True, pady=(0, 10))
            if 'btn_inicio' in globals():
                btn_inicio.pack_configure(side="left", fill="both", expand=True, padx=(0, 5))
                btn_inicio.configure(font=(FONT_MAIN, 20, "bold"), height=90)
            if 'btn_reset' in globals():
                btn_reset.pack_configure(side="right", fill="both", expand=True, padx=(5, 0))
                btn_reset.configure(font=(FONT_MAIN, 20, "bold"), height=90)
            if 'btn_stop' in globals():
                btn_stop.pack_configure(fill="both", expand=True, pady=(4, 0))
                btn_stop.configure(font=(FONT_MAIN, 24, "bold"), height=110)
        except Exception as err:
            print(f"Error escalando mandos: {err}")

    else:
        # Desplegar toda la sección JOG hacia abajo de forma amplia
        card_jog.pack_configure(fill="both", expand=True)
        f_jog_b.pack(fill="both", expand=True, padx=10, pady=6)
        if btn_toggle_jog:
            btn_toggle_jog.configure(
                text="▲ Plegar" if idioma_actual == "ES" else "▲ Fold",
                fg_color="#E2E8F0",
                hover_color=BORDER_CARD,
                text_color=COLOR_KOSTAL_NAVY
            )
        if hasattr(card_jog, '_header_title'):
            card_jog._header_title.configure(text=DICCIONARIO_IDIOMAS[idioma_actual]["op_jog"])
        if hasattr(card_jog, '_header_frame'):
            card_jog._header_frame.configure(fg_color="#F1F5F9")

        # Ajustar mandos de control a tamaño compacto para dar prioridad al JOG
        try:
            if 'card_mandos_der' in globals():
                card_mandos_der.pack_configure(fill="x", expand=False, pady=(4, 0))
            if 'f_mandos_der_b' in globals():
                f_mandos_der_b.pack_configure(fill="x", expand=False, padx=8, pady=6)
            if 'f_ir_row' in globals():
                f_ir_row.pack_configure(fill="x", expand=False, pady=(0, 4))
            if 'btn_inicio' in globals():
                btn_inicio.pack_configure(side="left", fill="x", expand=True, padx=(0, 3))
                btn_inicio.configure(font=(FONT_MAIN, 13, "bold"), height=42)
            if 'btn_reset' in globals():
                btn_reset.pack_configure(side="right", fill="x", expand=True, padx=(3, 0))
                btn_reset.configure(font=(FONT_MAIN, 13, "bold"), height=42)
            if 'btn_stop' in globals():
                btn_stop.pack_configure(fill="x", expand=False, pady=(2, 0))
                btn_stop.configure(font=(FONT_MAIN, 14, "bold"), height=46)
        except Exception as err:
            print(f"Error contrayendo mandos: {err}")

def _widget_btn_toggle_jog(parent_hdr):
    global btn_toggle_jog
    btn_toggle_jog = ctk.CTkButton(
        parent_hdr,
        text="▲ Plegar",
        width=85,
        height=26,
        font=(FONT_MAIN, 10, "bold"),
        fg_color="#E2E8F0",
        hover_color=BORDER_CARD,
        text_color=COLOR_KOSTAL_NAVY,
        corner_radius=5,
        command=alternar_pestana_jog
    )
    return btn_toggle_jog

# Tarjeta Única: Operación y Avance + Movimiento Manual JOG (Expandible hacia abajo)
card_jog = crear_card_moderna(
    col_3, 
    "OPERACIÓN & JOG",
    btn_widget=_widget_btn_toggle_jog
)
card_jog.pack(fill="both", expand=True, pady=(0, 6))

if hasattr(card_jog, '_header_frame'):
    card_jog._header_frame.bind("<Button-1>", alternar_pestana_jog)
if hasattr(card_jog, '_header_title'):
    card_jog._header_title.bind("<Button-1>", alternar_pestana_jog)

f_jog_b = ctk.CTkFrame(card_jog, fg_color="transparent")
f_jog_b.pack(fill="both", expand=True, padx=10, pady=6)

# ------------------------------------------------------------------------------
# 1. SECCIÓN SUPERIOR: OPERACIÓN Y AVANCE
# ------------------------------------------------------------------------------
f_sec_op_hdr = ctk.CTkFrame(f_jog_b, fg_color=BG_INPUT, corner_radius=6, border_width=1, border_color=BORDER_LIGHT)
f_sec_op_hdr.pack(fill="x", pady=(0, 5))
ctk.CTkLabel(f_sec_op_hdr, text="⚡ OPERACIÓN", font=(FONT_MAIN, 10, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left", padx=8, pady=3)

# Botón Modo Auto / Manual (destacado)
btn_manual = ctk.CTkButton(
    f_jog_b,
    text="⚙ MODO: AUTO",
    font=(FONT_MAIN, 12, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_ACCENT,
    text_color="white",
    border_width=1,
    border_color=COLOR_KOSTAL_LIGHT,
    corner_radius=8,
    height=36,
    command=alternar_manual
)
btn_manual.pack(fill="x", pady=(0, 6))

# Velocidad General
f_vel_row = ctk.CTkFrame(f_jog_b, fg_color="transparent")
f_vel_row.pack(fill="x", pady=(0, 6))
ctk.CTkLabel(f_vel_row, text="Velocidad:", font=(FONT_MAIN, 10, "bold"), text_color=TEXT_SECONDARY).pack(side="left")
caja_vel = ctk.CTkEntry(f_vel_row, width=60, height=28, font=(FONT_MONO, 11, "bold"), justify="center", corner_radius=4)
caja_vel.insert(0, str(config_actual.get("velocidad", "100")))
caja_vel.pack(side="right", padx=(2, 0))

def _on_caja_vel_change(e=None):
    try:
        v = int(float(caja_vel.get().strip()))
        if v > 0:
            config_actual["velocidad"] = v
            guardar_configuracion()
    except Exception:
        pass

caja_vel.bind("<KeyRelease>", _on_caja_vel_change)
caja_vel.bind("<Return>", lambda e: (ocultar_teclado_nativo(), caja_vel.master.focus_set(), _on_caja_vel_change(), "break")[3])
caja_vel.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), caja_vel.master.focus_set(), _on_caja_vel_change(), "break")[3])
ctk.CTkLabel(f_vel_row, text="mm/s", font=(FONT_MAIN, 9), text_color=TEXT_MUTED).pack(side="right")

# Divisor sutil
sep_jog = ctk.CTkFrame(f_jog_b, fg_color=BORDER_LIGHT, height=1)
sep_jog.pack(fill="x", pady=(2, 6))

# ------------------------------------------------------------------------------
# 2. SECCIÓN INFERIOR: MOVIMIENTO MANUAL JOG (CRUCETA AGRANDADA)
# ------------------------------------------------------------------------------
f_sec_jog_hdr = ctk.CTkFrame(f_jog_b, fg_color=BG_INPUT, corner_radius=6, border_width=1, border_color=BORDER_LIGHT)
f_sec_jog_hdr.pack(fill="x", pady=(0, 5))
ctk.CTkLabel(f_sec_jog_hdr, text="🕹️ JOG MANUAL", font=(FONT_MAIN, 10, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left", padx=8, pady=3)

# Paso de Movimiento con controles táctiles avanzados (Sin bugs ni trabas)
f_step_row = ctk.CTkFrame(f_jog_b, fg_color="transparent")
f_step_row.pack(fill="x", pady=(0, 2))
ctk.CTkLabel(f_step_row, text="Paso JOG:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_SECONDARY).pack(side="left")

# Botón abrir teclado numérico táctil flotante
btn_open_numpad = ctk.CTkButton(
    f_step_row,
    text="🔢",
    font=(FONT_MAIN, 13),
    width=34,
    height=36,
    fg_color="#FFFFFF",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=6,
    command=abrir_teclado_numerico_paso
)
btn_open_numpad.pack(side="right", padx=(2, 0))

ctk.CTkLabel(f_step_row, text="mm", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).pack(side="right", padx=(3, 2))

# Botón incrementar paso [+]
btn_step_plus = ctk.CTkButton(
    f_step_row,
    text="+",
    font=(FONT_MAIN, 16, "bold"),
    width=34,
    height=36,
    fg_color="#FFFFFF",
    hover_color="#E2E8F0",
    text_color="#1E293B",
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=6,
    command=lambda: paso_jog_siguiente(1)
)
btn_step_plus.pack(side="right", padx=1)

# Caja de entrada interactiva con sincronización y persistencia (Ampliada a 110x38 para admitir múltiples dígitos y decimales)
caja_paso = ctk.CTkEntry(f_step_row, width=110, height=38, font=(FONT_MONO, 14, "bold"), justify="center", corner_radius=6)
caja_paso.insert(0, str(config_actual.get("paso_jog", "5.0")))
caja_paso.pack(side="right", padx=2)

def _on_caja_paso_change(event=None):
    txt = caja_paso.get().strip().replace(",", ".")
    try:
        v = float(txt)
        if 0.01 <= v <= 200.0:
            config_actual["paso_jog"] = v
            guardar_configuracion()
            actualizar_botones_paso_preset(v)
    except Exception:
        pass

def _on_caja_paso_focusout(event=None):
    txt = caja_paso.get().strip().replace(",", ".")
    try:
        v = float(txt)
        if 0.01 <= v <= 200.0:
            config_actual["paso_jog"] = v
            guardar_configuracion()
            actualizar_botones_paso_preset(v)
            caja_paso.delete(0, tk.END)
            if "." in txt:
                partes = txt.split('.')
                dec = partes[1] if len(partes) > 1 else ""
                if len(dec) == 0:
                    caja_paso.insert(0, f"{v:g}")
                else:
                    caja_paso.insert(0, f"{v:.{min(len(dec), 3)}f}")
            else:
                caja_paso.insert(0, f"{v:g}")
            return
    except Exception:
        pass
    v = obtener_paso_jog()
    caja_paso.delete(0, tk.END)
    caja_paso.insert(0, f"{v:g}")
    actualizar_botones_paso_preset(v)

caja_paso.bind("<KeyRelease>", _on_caja_paso_change)
caja_paso.bind("<FocusOut>", _on_caja_paso_focusout)
caja_paso.bind("<Return>", lambda e: (ocultar_teclado_nativo(), caja_paso.master.focus_set(), _on_caja_paso_focusout(), "break")[3])
caja_paso.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), caja_paso.master.focus_set(), _on_caja_paso_focusout(), "break")[3])
caja_paso.bind("<Double-Button-1>", lambda e: abrir_teclado_numerico_paso())

# Botón reducir paso [-]
btn_step_minus = ctk.CTkButton(
    f_step_row,
    text="-",
    font=(FONT_MAIN, 16, "bold"),
    width=34,
    height=36,
    fg_color="#FFFFFF",
    hover_color="#E2E8F0",
    text_color="#1E293B",
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=6,
    command=lambda: paso_jog_siguiente(-1)
)
btn_step_minus.pack(side="right", padx=(0, 1))

# Fila de Presets Rápidos de Paso (0.5, 1.0, 2.0, 5.0, 10.0)
f_step_presets = ctk.CTkFrame(f_jog_b, fg_color="transparent")
f_step_presets.pack(fill="x", pady=(2, 5))

_btns_paso_presets.clear()
for p_preset in [0.5, 1.0, 2.0, 5.0, 10.0]:
    b_p = ctk.CTkButton(
        f_step_presets,
        text=f"{p_preset:g}",
        font=(FONT_MAIN, 10, "bold"),
        height=28,
        corner_radius=4,
        border_width=1,
        border_color=BORDER_LIGHT,
        fg_color="#FFFFFF",
        hover_color="#F1F5F9",
        text_color="#334155",
        command=lambda v=p_preset: fijar_paso_jog(v)
    )
    b_p.pack(side="left", expand=True, fill="x", padx=1)
    _btns_paso_presets[p_preset] = b_p

# Cruzeta y Coordenadas Agrandadas para pantalla táctil
f_jog_ctrl = ctk.CTkFrame(f_jog_b, fg_color="transparent")
f_jog_ctrl.pack(fill="x", pady=4)

f_coord_box = ctk.CTkFrame(f_jog_ctrl, fg_color=BG_INPUT, corner_radius=8, border_width=1, border_color=BORDER_LIGHT)
f_coord_box.pack(side="left", padx=(0, 6), pady=2)

ctk.CTkLabel(f_coord_box, text="COORD (0-200)", font=(FONT_MAIN, 8, "bold"), text_color=TEXT_MUTED).pack(padx=8, pady=(4, 0))
fc_x = ctk.CTkFrame(f_coord_box, fg_color="transparent")
fc_x.pack(fill="x", padx=8, pady=2)
ctk.CTkLabel(fc_x, text="X:", font=(FONT_MAIN, 11, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left")
lbl_jog_coord_x = ctk.CTkLabel(fc_x, text=f"{cero_guardado_x:.2f}", font=(FONT_MONO, 12, "bold"), text_color=TEXT_PRIMARY)
lbl_jog_coord_x.pack(side="right")

fc_y = ctk.CTkFrame(f_coord_box, fg_color="transparent")
fc_y.pack(fill="x", padx=8, pady=(2, 4))
ctk.CTkLabel(fc_y, text="Y:", font=(FONT_MAIN, 11, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left")
lbl_jog_coord_y = ctk.CTkLabel(fc_y, text=f"{cero_guardado_y:.2f}", font=(FONT_MONO, 12, "bold"), text_color=TEXT_PRIMARY)
lbl_jog_coord_y.pack(side="right")

f_pad = ctk.CTkFrame(f_jog_ctrl, fg_color="transparent")
f_pad.pack(side="left", expand=True)

def btn_pad(parent, txt, cmd):
    return ctk.CTkButton(
        parent,
        text=txt,
        font=(FONT_MAIN, 20, "bold"),
        fg_color="#FFFFFF",
        hover_color="#F1F5F9",
        text_color="#1E293B",
        border_width=1,
        border_color=BORDER_LIGHT,
        corner_radius=8,
        width=64,
        height=52,
        command=cmd
    )

btn_y_mas = btn_pad(f_pad, "▲", lambda: mover_cruzeta('Y', 1))
btn_y_mas.grid(row=0, column=1, pady=2)

btn_x_menos = btn_pad(f_pad, "◄", lambda: mover_cruzeta('X', -1))
btn_x_menos.grid(row=1, column=0, padx=2)

ctk.CTkLabel(f_pad, text="○", font=(FONT_MAIN, 14), text_color=TEXT_MUTED).grid(row=1, column=1)

btn_x_mas = btn_pad(f_pad, "►", lambda: mover_cruzeta('X', 1))
btn_x_mas.grid(row=1, column=2, padx=2)

btn_y_menos = btn_pad(f_pad, "▼", lambda: mover_cruzeta('Y', -1))
btn_y_menos.grid(row=2, column=1, pady=2)

# Botones de Referencia y Activación Manual de Tampo
f_refs = ctk.CTkFrame(f_jog_b, fg_color="transparent")
f_refs.pack(fill="x", pady=(4, 0))

# Fila 1: Home y Activar Tampo (SIEMPRE VISIBLES Y MÁS GRANDES)
f_main_jog_actions = ctk.CTkFrame(f_refs, fg_color="transparent")
f_main_jog_actions.pack(fill="x", pady=(2, 4))

btn_ir_home = ctk.CTkButton(
    f_main_jog_actions,
    text="🏠 Home",
    font=(FONT_MAIN, 13, "bold"),
    fg_color="#FFFFFF",
    hover_color="#F1F5F9",
    text_color="#1E293B",
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=8,
    height=46,
    command=ejecutar_home_general
)
btn_ir_home.pack(side="left", expand=True, fill="x", padx=2)

btn_activar_tampo = ctk.CTkButton(
    f_main_jog_actions,
    text="🏷️ Activar Tampo",
    font=(FONT_MAIN, 13, "bold"),
    fg_color="#FFFFFF",
    hover_color="#F1F5F9",
    text_color=COLOR_KOSTAL_ACCENT,
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=8,
    height=46,
    command=activar_tampo_manual
)
btn_activar_tampo.pack(side="left", expand=True, fill="x", padx=2)

# Fila 2: Pestaña Desplegable para Set 0 y Set Home (Ocultos por defecto para proteger calibración)
_calib_jog_desplegada = False

f_calib_desplegable = ctk.CTkFrame(f_refs, fg_color="#FEF3C7", corner_radius=8, border_width=1, border_color="#FDE68A")
# f_calib_desplegable permanece oculto hasta pulsar desplegar

f_calib_banner = ctk.CTkFrame(f_calib_desplegable, fg_color="transparent")
f_calib_banner.pack(fill="x", padx=8, pady=(6, 2))
ctk.CTkLabel(
    f_calib_banner,
    text="⚠️ CALIBRACIÓN Y FIJACIÓN DE ORIGEN",
    font=(FONT_MAIN, 9, "bold"),
    text_color="#92400E"
).pack(side="left")

f_calib_row = ctk.CTkFrame(f_calib_desplegable, fg_color="transparent")
f_calib_row.pack(fill="x", padx=4, pady=(2, 6))

btn_set_cero = ctk.CTkButton(
    f_calib_row,
    text="↩ Set 0",
    font=(FONT_MAIN, 11, "bold"),
    fg_color="#FFFFFF",
    hover_color="#F0FDF4",
    text_color=COLOR_OK_TEXT,
    border_width=1,
    border_color="#86EFAC",
    corner_radius=6,
    height=42,
    command=guardar_posicion_como_cero
)
btn_set_cero.pack(side="left", expand=True, fill="x", padx=2)

btn_set_home = ctk.CTkButton(
    f_calib_row,
    text="⚙ Set Home",
    font=(FONT_MAIN, 11, "bold"),
    fg_color="#FFFFFF",
    hover_color="#FFFBEB",
    text_color="#92400E",
    border_width=1,
    border_color="#FDE68A",
    corner_radius=6,
    height=42,
    command=guardar_posicion_como_home
)
btn_set_home.pack(side="left", expand=True, fill="x", padx=2)

def alternar_desplegable_calibracion():
    global _calib_jog_desplegada
    _calib_jog_desplegada = not _calib_jog_desplegada
    if _calib_jog_desplegada:
        f_calib_desplegable.pack(fill="x", pady=(2, 4))
        btn_toggle_calib.configure(text="▲ Ocultar Calibración", fg_color="#FEF3C7", text_color="#92400E")
    else:
        f_calib_desplegable.pack_forget()
        btn_toggle_calib.configure(text="▼ Desplegar Calibración (Set 0 / Set Home)", fg_color="#F8FAFC", text_color=COLOR_KOSTAL_NAVY)

def abrir_desplegable_calibracion():
    global _calib_jog_desplegada
    if not _calib_jog_desplegada:
        alternar_desplegable_calibracion()

btn_toggle_calib = ctk.CTkButton(
    f_refs,
    text="▼ Desplegar Calibración (Set 0 / Set Home)",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#F8FAFC",
    hover_color="#F1F5F9",
    text_color=COLOR_KOSTAL_NAVY,
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=6,
    height=34,
    command=alternar_desplegable_calibracion
)
btn_toggle_calib.pack(fill="x", pady=(2, 2))

# DISPLAY NO EDITABLE DE CERO GUARDADO (INFERIOR DE CRUZETA)
f_cero_readonly = ctk.CTkFrame(f_jog_b, fg_color=BG_INPUT, corner_radius=8, border_width=1, border_color=BORDER_CARD)
f_cero_readonly.pack(fill="x", pady=(5, 2))

f_cro_hdr = ctk.CTkFrame(f_cero_readonly, fg_color="transparent")
f_cro_hdr.pack(fill="x", padx=8, pady=(3, 1))
ctk.CTkLabel(f_cro_hdr, text="🔒 CERO REGISTRADO:", font=(FONT_MAIN, 9, "bold"), text_color=TEXT_MUTED).pack(side="left")
ctk.CTkLabel(f_cro_hdr, text="ORIGEN", font=(FONT_MAIN, 8, "bold"), text_color=COLOR_OK, fg_color=COLOR_OK_SOFT, corner_radius=4, padx=5).pack(side="right")

lbl_cero_jog_val = ctk.CTkLabel(
    f_cero_readonly,
    text=f"X: {cero_guardado_x:.2f} mm  |  Y: {cero_guardado_y:.2f} mm",
    font=(FONT_MONO, 11, "bold"),
    text_color=COLOR_KOSTAL_NAVY
)
lbl_cero_jog_val.pack(anchor="w", padx=8, pady=(0, 3))

# ==============================================================================
# MANDOS MAESTROS DE MARCHA (LADO DERECHO INFERIOR COMPACTO: INICIO, RESET, PARO)
# ==============================================================================
card_mandos_der = crear_card_moderna(col_3, "MANDOS DE CONTROL")
card_mandos_der.pack(fill="x", expand=False, pady=(4, 0))

f_mandos_der_b = ctk.CTkFrame(card_mandos_der, fg_color="transparent")
f_mandos_der_b.pack(fill="x", padx=8, pady=6)

# Fila superior: INICIO y RESET lado a lado
f_ir_row = ctk.CTkFrame(f_mandos_der_b, fg_color="transparent")
f_ir_row.pack(fill="x", pady=(0, 4))

btn_inicio = ctk.CTkButton(
    f_ir_row,
    text="▶  INICIO",
    font=(FONT_MAIN, 13, "bold"),
    fg_color=COLOR_OK,
    hover_color=COLOR_OK_HOVER,
    text_color="white",
    corner_radius=8,
    height=42,
    command=iniciar_ciclo_general
)
btn_inicio.pack(side="left", fill="x", expand=True, padx=(0, 3))

btn_reset = ctk.CTkButton(
    f_ir_row,
    text="↻  RESET",
    font=(FONT_MAIN, 13, "bold"),
    fg_color=COLOR_WARN,
    hover_color=COLOR_WARN_HOVER,
    text_color="#1E293B",
    corner_radius=8,
    height=42,
    command=ejecutar_reset_general
)
btn_reset.pack(side="right", fill="x", expand=True, padx=(3, 0))

# Fila inferior: PARO compacto y de alta visibilidad
btn_stop = ctk.CTkButton(
    f_mandos_der_b,
    text="🛑  PARO",
    font=(FONT_MAIN, 14, "bold"),
    fg_color=COLOR_NOK,
    hover_color=COLOR_NOK_HOVER,
    text_color="white",
    corner_radius=8,
    height=46,
    command=alternar_stop
)
btn_stop.pack(fill="x", pady=(2, 0))

# ------------------------------------------------------------------------------
# 5.3. PANTALLA 3: MÓDULO ADMINISTRADOR (ESTILO 1:1 INTERFAZ_KOSTAL)
# ------------------------------------------------------------------------------
frame_admin = ctk.CTkFrame(ventana, fg_color=BG_MAIN, corner_radius=0)
try:
    frame_admin.configure(cursor="none")
except Exception:
    pass

# Header Superior de Admin (Blanco limpio con Título, Subtítulo, Logo Baluarte y Reloj - Imágenes 1-4)
hdr_admin = ctk.CTkFrame(frame_admin, fg_color="#FFFFFF", height=60, corner_radius=0, border_width=1, border_color=BORDER_CARD)
hdr_admin.pack(fill="x", side="top")

f_adm_hdr_left = ctk.CTkFrame(hdr_admin, fg_color="transparent")
f_adm_hdr_left.pack(side="left", padx=20, fill="y", pady=10)

lbl_adm_title = ctk.CTkLabel(f_adm_hdr_left, text="Recetario de producción", font=(FONT_MAIN, 17, "bold"), text_color=COLOR_KOSTAL_DARK)
lbl_adm_title.pack(anchor="w")

lbl_adm_subtitle = ctk.CTkLabel(f_adm_hdr_left, text="Configuración de recetas, matrices y coordenadas.", font=(FONT_MAIN, 11), text_color=TEXT_MUTED)
lbl_adm_subtitle.pack(anchor="w")

f_adm_hdr_right = ctk.CTkFrame(hdr_admin, fg_color="transparent")
f_adm_hdr_right.pack(side="right", padx=20, fill="y", pady=8)

if img_logo_baluarte_panel:
    ctk.CTkLabel(f_adm_hdr_right, image=img_logo_baluarte_panel, text="").pack(side="left", padx=(0, 16))

f_adm_clock = ctk.CTkFrame(f_adm_hdr_right, fg_color="transparent")
f_adm_clock.pack(side="left", padx=(0, 14))
lbl_adm_time = ctk.CTkLabel(f_adm_clock, text="09:05:17", font=(FONT_MONO, 12, "bold"), text_color=COLOR_KOSTAL_DARK)
lbl_adm_time.pack(anchor="e")
lbl_adm_date = ctk.CTkLabel(f_adm_clock, text="24/09/2026", font=(FONT_MONO, 10), text_color=TEXT_MUTED)
lbl_adm_date.pack(anchor="e")

btn_adm_kb = ctk.CTkButton(
    f_adm_hdr_right,
    text="⌨ Teclado",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#F1F5F9",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=85,
    height=30,
    command=alternar_teclado_tactil
)
btn_adm_kb.pack(side="left", padx=(4, 4))

btn_adm_min = ctk.CTkButton(
    f_adm_hdr_right,
    text="🗕 Minimizar",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#F1F5F9",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=90,
    height=30,
    command=minimizar_ventana
)
btn_adm_min.pack(side="left", padx=(0, 4))

btn_adm_fs = ctk.CTkButton(
    f_adm_hdr_right,
    text="⛶ Pantalla",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#F1F5F9",
    hover_color="#E2E8F0",
    text_color=COLOR_KOSTAL_NAVY,
    corner_radius=6,
    width=85,
    height=30,
    command=alternar_pantalla_completa
)
btn_adm_fs.pack(side="left", padx=(0, 4))

# CUERPO DEL PANEL ADMIN CON SIDEBAR IZQUIERDA CORPORATIVA KOSTAL
f_adm_body = ctk.CTkFrame(frame_admin, fg_color="transparent")
f_adm_body.pack(fill="both", expand=True, padx=0, pady=0)

# Sidebar Izquierda Fiel a las Imágenes 1 a 4 (Azul Marino Corporativo #162746)
f_adm_nav = ctk.CTkFrame(f_adm_body, fg_color=COLOR_KOSTAL_NAVY, width=220, corner_radius=0)
f_adm_nav.pack(side="left", fill="y")
f_adm_nav.pack_propagate(False)

# Brand Header en la Sidebar
f_brand_sb = ctk.CTkFrame(f_adm_nav, fg_color="transparent")
f_brand_sb.pack(fill="x", padx=16, pady=(18, 18))

ctk.CTkLabel(f_brand_sb, text="KOSTAL", font=(FONT_MAIN, 18, "bold"), text_color="white").pack(side="left")
ctk.CTkLabel(f_brand_sb, text=" HMI ADMIN", font=(FONT_MAIN, 10, "bold"), text_color="#93C5FD").pack(side="left", padx=(4, 0), pady=(4, 0))

admin_vistas = {}
admin_nav_btns = {}

MODULOS_INFO = {
    "recetas": {
        "titulo": "Recetario de producción",
        "sub": "Configuración de recetas, matrices y coordenadas."
    },
    "excel": {
        "titulo": "Estadísticas de producción",
        "sub": "Histórico general de rendimiento, piezas y tiempos de la estación."
    },
    "usuarios": {
        "titulo": "Gestión de usuarios",
        "sub": "Administración de operadores y administradores del sistema."
    },
    "ajustes": {
        "titulo": "Configuración del HMI",
        "sub": "Ajustes globales de la estación, límites de inactividad y parámetros."
    }
}

def cambiar_modulo_admin(nombre):
    for n, f in admin_vistas.items():
        if n == nombre:
            f.pack(fill="both", expand=True, padx=16, pady=16)
            if n in admin_nav_btns:
                admin_nav_btns[n].configure(fg_color="#004F9F", text_color="white")
        else:
            f.pack_forget()
            if n in admin_nav_btns:
                admin_nav_btns[n].configure(fg_color="transparent", text_color="#CBD5E1")
                
    if nombre in MODULOS_INFO:
        lbl_adm_title.configure(text=MODULOS_INFO[nombre]["titulo"])
        lbl_adm_subtitle.configure(text=MODULOS_INFO[nombre]["sub"])

def crear_btn_nav_admin(nombre, texto_icono):
    btn = ctk.CTkButton(
        f_adm_nav,
        text=texto_icono,
        anchor="w",
        font=(FONT_MAIN, 12, "bold"),
        fg_color="transparent",
        hover_color="#243B66",
        text_color="#CBD5E1",
        corner_radius=6,
        height=40,
        command=lambda n=nombre: cambiar_modulo_admin(n)
    )
    btn.pack(fill="x", padx=10, pady=3)
    admin_nav_btns[nombre] = btn
    return btn

# Botón Volver al Panel Principal (al inicio del menú de administración)
btn_nav_volver = ctk.CTkButton(
    f_adm_nav,
    text="⬅  Volver",
    anchor="w",
    font=(FONT_MAIN, 12, "bold"),
    fg_color=COLOR_KOSTAL_ACCENT,
    hover_color="#243B66",
    text_color="white",
    corner_radius=6,
    height=40,
    command=volver_a_panel_operador
)
btn_nav_volver.pack(fill="x", padx=10, pady=(4, 10))

# Botones de navegación con iconos y textos fieles a las imágenes
crear_btn_nav_admin("recetas", "📖  Recetario")
crear_btn_nav_admin("excel", "📈  Estadísticas")
crear_btn_nav_admin("usuarios", "👥  Usuarios")
crear_btn_nav_admin("ajustes", "⚙  Ajustes Globales")

# Spacer inferior en la sidebar
f_sb_spacer = ctk.CTkFrame(f_adm_nav, fg_color="transparent")
f_sb_spacer.pack(fill="both", expand=True)

# Sección inferior de la sidebar: Idioma, Usuario y Logout (Imágenes 1-4)
f_sb_bottom = ctk.CTkFrame(f_adm_nav, fg_color="transparent")
f_sb_bottom.pack(fill="x", padx=12, pady=16)

# Idioma interactivo ES / EN
f_adm_lang = ctk.CTkFrame(f_sb_bottom, fg_color="#0F1E36", corner_radius=6, border_width=1, border_color="#243B66", height=30)
f_adm_lang.pack(fill="x", pady=(0, 12))

btn_adm_lang_es = ctk.CTkButton(
    f_adm_lang,
    text="ES",
    width=55,
    height=24,
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#004F9F",
    text_color="white",
    corner_radius=4,
    command=lambda: cambiar_idioma("ES")
)
btn_adm_lang_es.pack(side="left", expand=True, padx=2, pady=3)

btn_adm_lang_en = ctk.CTkButton(
    f_adm_lang,
    text="EN",
    width=55,
    height=24,
    font=(FONT_MAIN, 10, "bold"),
    fg_color="transparent",
    hover_color="#1E293B",
    text_color="#94A3B8",
    corner_radius=4,
    command=lambda: cambiar_idioma("EN")
)
btn_adm_lang_en.pack(side="left", expand=True, padx=2, pady=3)

# Usuario activo en la sidebar
f_adm_user_card = ctk.CTkFrame(f_sb_bottom, fg_color="transparent")
f_adm_user_card.pack(fill="x", pady=(0, 10))

f_u_av = ctk.CTkLabel(f_adm_user_card, text="👤", font=(FONT_MAIN, 16), text_color="#93C5FD")
f_u_av.pack(side="left", padx=(4, 8))

f_u_txts = ctk.CTkFrame(f_adm_user_card, fg_color="transparent")
f_u_txts.pack(side="left", fill="both", expand=True)

ctk.CTkLabel(f_u_txts, text="Carlos H.", font=(FONT_MAIN, 11, "bold"), text_color="white", anchor="w").pack(fill="x")
ctk.CTkLabel(f_u_txts, text="ADMINISTRADOR", font=(FONT_MAIN, 8, "bold"), text_color="#94A3B8", anchor="w").pack(fill="x")

# Botón Cerrar Sesión en Sidebar Admin
btn_adm_logout = ctk.CTkButton(
    f_sb_bottom,
    text="↪  Cerrar Sesión",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#2D1824",
    hover_color="#7F1D1D",
    text_color="#FCA5A5",
    border_width=1,
    border_color="#7F1D1D",
    corner_radius=6,
    height=32,
    command=cerrar_sesion
)
btn_adm_logout.pack(fill="x")

# Contenedor Derecho del Contenido
f_adm_content = ctk.CTkFrame(f_adm_body, fg_color=BG_MAIN, corner_radius=0)
f_adm_content.pack(side="left", fill="both", expand=True)

# Vista 1: Excel (Estadísticas)
view_excel = ctk.CTkScrollableFrame(f_adm_content, fg_color="transparent")
admin_vistas["excel"] = view_excel

# Vista 2: Recetas & Matriz (Programas)
view_recetas = ctk.CTkScrollableFrame(f_adm_content, fg_color="transparent")
admin_vistas["recetas"] = view_recetas

# Vista 3: Usuarios
view_usuarios = ctk.CTkScrollableFrame(f_adm_content, fg_color="transparent")
admin_vistas["usuarios"] = view_usuarios

# Vista 4: Ajustes
view_ajustes = ctk.CTkScrollableFrame(f_adm_content, fg_color="transparent")
admin_vistas["ajustes"] = view_ajustes

# --- VISTA 1: EXPORTACIÓN A EXCEL (ALTO CONTRASTE) ---
def exportar_excel_accion():
    ruta_guardado = filedialog.asksaveasfilename(
        defaultextension=".xlsx",
        filetypes=[("Libro de Excel (*.xlsx)", "*.xlsx"), ("Archivo CSV (*.csv)", "*.csv")],
        initialfile=f"Reporte_Produccion_KOSTAL_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    if not ruta_guardado:
        return
        
    try:
        df = pd.DataFrame(REGISTROS_PRODUCCION)
        if ruta_guardado.endswith(".csv"):
            df.to_csv(ruta_guardado, index=False, encoding="utf-8-sig")
        else:
            df.to_excel(ruta_guardado, index=False, engine="openpyxl")
        messagebox.showinfo("Exportación Exitosa", f"Archivo generado exitosamente en:\n{ruta_guardado}")
        lbl_estado.configure(text=f"✓ Reporte Excel exportado correctamente: {os.path.basename(ruta_guardado)}", text_color=COLOR_OK)
    except Exception as e:
        messagebox.showerror("Error al Exportar", f"No se pudo guardar el archivo:\n{e}")

# --- VISTA 1: ESTADÍSTICAS & EXPORTACIÓN A EXCEL (IMAGEN 3) ---
# Card de Filtros de Búsqueda (Imagen 3)
card_filtros = ctk.CTkFrame(view_excel, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
card_filtros.pack(fill="x", pady=(0, 10))

f_flt_hdr = ctk.CTkFrame(card_filtros, fg_color="transparent")
f_flt_hdr.pack(fill="x", padx=14, pady=(10, 4))
ctk.CTkLabel(f_flt_hdr, text="🔍  Filtros de Búsqueda", font=(FONT_MAIN, 13, "bold"), text_color=COLOR_KOSTAL_DARK).pack(side="left")

f_flt_row = ctk.CTkFrame(card_filtros, fg_color="transparent")
f_flt_row.pack(fill="x", padx=14, pady=(0, 12))

def campo_filtro(parent, etiqueta, val_default, ancho=110):
    box = ctk.CTkFrame(parent, fg_color="transparent")
    box.pack(side="left", padx=(0, 8))
    ctk.CTkLabel(box, text=etiqueta.upper(), font=(FONT_MAIN, 8, "bold"), text_color=TEXT_MUTED).pack(anchor="w")
    entry = ctk.CTkEntry(box, width=ancho, height=32, font=(FONT_MAIN, 10), fg_color="#FFFFFF", border_color=BORDER_CARD)
    entry.insert(0, val_default)
    entry.pack()
    return entry

campo_filtro(f_flt_row, "Fecha Desde", "20/08/2026", 115)
campo_filtro(f_flt_row, "Fecha Hasta", "26/08/2026", 115)

box_u_flt = ctk.CTkFrame(f_flt_row, fg_color="transparent")
box_u_flt.pack(side="left", padx=(0, 8))
ctk.CTkLabel(box_u_flt, text="USUARIO", font=(FONT_MAIN, 8, "bold"), text_color=TEXT_MUTED).pack(anchor="w")
opt_u_flt = ctk.CTkOptionMenu(box_u_flt, values=["Todos los usuarios", "Juan Pérez", "Carlos Hernández", "María López"], width=130, height=32, fg_color="#FFFFFF", text_color=TEXT_PRIMARY, button_color=BORDER_CARD, button_hover_color=BORDER_LIGHT)
opt_u_flt.pack()

box_m_flt = ctk.CTkFrame(f_flt_row, fg_color="transparent")
box_m_flt.pack(side="left", padx=(0, 8))
ctk.CTkLabel(box_m_flt, text="MODELO", font=(FONT_MAIN, 8, "bold"), text_color=TEXT_MUTED).pack(anchor="w")
opt_m_flt = ctk.CTkOptionMenu(box_m_flt, values=["Todos los modelos", "KSL-001", "KSL-002", "KSL-003"], width=120, height=32, fg_color="#FFFFFF", text_color=TEXT_PRIMARY, button_color=BORDER_CARD, button_hover_color=BORDER_LIGHT)
opt_m_flt.pack()

box_t_flt = ctk.CTkFrame(f_flt_row, fg_color="transparent")
box_t_flt.pack(side="left", padx=(0, 12))
ctk.CTkLabel(box_t_flt, text="TURNO", font=(FONT_MAIN, 8, "bold"), text_color=TEXT_MUTED).pack(anchor="w")
opt_t_flt = ctk.CTkOptionMenu(box_t_flt, values=["Todos los turnos", "T1 (Matutino)", "T2 (Vespertino)", "T3 (Nocturno)"], width=120, height=32, fg_color="#FFFFFF", text_color=TEXT_PRIMARY, button_color=BORDER_CARD, button_hover_color=BORDER_LIGHT)
opt_t_flt.pack()

btn_filtrar = ctk.CTkButton(
    f_flt_row,
    text="🔍 Filtrar",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=34,
    width=90,
    command=lambda: messagebox.showinfo("Filtro", "Filtro aplicado sobre los registros de producción.")
)
btn_filtrar.pack(side="left", padx=(4, 8), pady=(14, 0))

btn_exportar_excel = ctk.CTkButton(
    f_flt_row,
    text="📥 Exportar a Excel (CSV)",
    font=(FONT_MAIN, 10, "bold"),
    fg_color="#FFFFFF",
    hover_color=BG_INPUT,
    text_color=COLOR_KOSTAL_NAVY,
    border_width=1,
    border_color=BORDER_LIGHT,
    corner_radius=6,
    height=34,
    width=150,
    command=exportar_excel_accion
)
btn_exportar_excel.pack(side="left", pady=(14, 0))

# Fila de 5 Tarjetas KPI (Imagen 3)
f_kpis_row = ctk.CTkFrame(view_excel, fg_color="transparent")
f_kpis_row.pack(fill="x", pady=(0, 10))
f_kpis_row.columnconfigure((0, 1, 2, 3, 4), weight=1)

def card_kpi_stat(parent, col, titulo, valor, badge_txt, badge_bg, badge_fg, sub=""):
    c = ctk.CTkFrame(parent, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
    c.grid(row=0, column=col, padx=4, sticky="nsew")
    f_in = ctk.CTkFrame(c, fg_color="transparent")
    f_in.pack(padx=12, pady=10, fill="both")
    
    f_h = ctk.CTkFrame(f_in, fg_color="transparent")
    f_h.pack(fill="x")
    ctk.CTkLabel(f_h, text=titulo.upper(), font=(FONT_MAIN, 8, "bold"), text_color=TEXT_MUTED).pack(side="left")
    if badge_txt:
        ctk.CTkLabel(f_h, text=badge_txt, font=(FONT_MAIN, 8, "bold"), fg_color=badge_bg, text_color=badge_fg, corner_radius=4, padx=5, pady=1).pack(side="right")
        
    ctk.CTkLabel(f_in, text=valor, font=(FONT_MAIN, 18, "bold"), text_color=COLOR_KOSTAL_DARK).pack(anchor="w", pady=(4, 2))
    if sub:
        ctk.CTkLabel(f_in, text=sub, font=(FONT_MAIN, 8), text_color=TEXT_MUTED).pack(anchor="w")

card_kpi_stat(f_kpis_row, 0, "TOTAL PRODUCIDO", "1,428", "+12% vs sem", COLOR_OK_SOFT, COLOR_OK_TEXT, "↗ vs semana anterior")
card_kpi_stat(f_kpis_row, 1, "TOTAL PIEZAS OK", "1,392", "97.5%", COLOR_OK_SOFT, COLOR_OK_TEXT, "Conformidad pieza buena")
card_kpi_stat(f_kpis_row, 2, "TOTAL PIEZAS NOK", "36", "2.5%", COLOR_NOK_SOFT, COLOR_NOK_TEXT, "Rechazos o defectos")
card_kpi_stat(f_kpis_row, 3, "AVG LOGIN → 1ª SEC.", "04:18", "Eficiencia OK", COLOR_OK_SOFT, COLOR_OK_TEXT, "Minutos arranque turno")
card_kpi_stat(f_kpis_row, 4, "AVG DURACIÓN SESIÓN", "08:04:12", "Jornadas OK", COLOR_PURPLE_SOFT, COLOR_PURPLE_TEXT, "Jornadas completas")

# Tabla de Registros de Producción (Imagen 3)
frame_tabla_excel = ctk.CTkFrame(view_excel, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
frame_tabla_excel.pack(fill="both", expand=True, pady=4)

cols_tabla = ["FECHA", "USUARIO", "INICIO SESIÓN", "1ª SECUENCIA", "DELTA 1ª SEC", "MODELO", "PROG", "TOTAL", "OK", "NOK", "% CALIDAD", "DURACIÓN"]
f_tbl_hdr = ctk.CTkFrame(frame_tabla_excel, fg_color="#F8FAFC", height=32, corner_radius=0)
f_tbl_hdr.pack(fill="x", padx=1, pady=1)

for c_nom in cols_tabla:
    ctk.CTkLabel(f_tbl_hdr, text=c_nom, font=(FONT_MAIN, 9, "bold"), text_color="#475569").pack(side="left", expand=True)

for idx, reg in enumerate(REGISTROS_PRODUCCION):
    bg_fila = "#FFFFFF" if idx % 2 == 0 else "#F8FAFC"
    f_row = ctk.CTkFrame(frame_tabla_excel, fg_color=bg_fila, height=30, corner_radius=0)
    f_row.pack(fill="x", padx=1, pady=1)
    ctk.CTkLabel(f_row, text=reg["fecha"], font=(FONT_MONO, 9), text_color=TEXT_PRIMARY).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=reg["usuario"], font=(FONT_MAIN, 9, "bold"), text_color=COLOR_KOSTAL_NAVY).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=reg["hora_login"], font=(FONT_MONO, 9), text_color=TEXT_MUTED).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text="07:34:02", font=(FONT_MONO, 9), text_color=TEXT_MUTED).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text="00:03:47", font=(FONT_MONO, 9, "bold"), text_color="#2563EB").pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=reg["receta"][:7], font=(FONT_MAIN, 9), text_color=TEXT_PRIMARY).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text="P01", font=(FONT_MONO, 9, "bold"), text_color="#0284C7").pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=str(reg["total"]), font=(FONT_MONO, 9, "bold"), text_color=TEXT_PRIMARY).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=str(reg["ok"]), font=(FONT_MONO, 9, "bold"), text_color=COLOR_OK).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=str(reg["nok"]), font=(FONT_MONO, 9, "bold"), text_color=COLOR_NOK).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=reg["calidad"], font=(FONT_MONO, 9, "bold"), text_color=COLOR_OK).pack(side="left", expand=True)
    ctk.CTkLabel(f_row, text=reg["duracion"], font=(FONT_MONO, 9), text_color=TEXT_SECONDARY).pack(side="left", expand=True)

# --- VISTA 2: PROGRAMAS DE PRODUCCIÓN (IMAGEN 4) ---
# Barra Superior con Buscador y Botón "+ NUEVA RECETA"
f_rec_top_bar = ctk.CTkFrame(view_recetas, fg_color="transparent")
f_rec_top_bar.pack(fill="x", pady=(0, 10))

f_rec_search_box = ctk.CTkFrame(f_rec_top_bar, fg_color="#FFFFFF", corner_radius=6, border_width=1, border_color=BORDER_CARD)
f_rec_search_box.pack(side="left")
ctk.CTkLabel(f_rec_search_box, text="🔍", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left", padx=(10, 4), pady=4)
entry_search_receta = ctk.CTkEntry(f_rec_search_box, placeholder_text="Buscar receta...", width=260, height=30, fg_color="#FFFFFF", border_width=0, font=(FONT_MAIN, 10))
entry_search_receta.pack(side="left", padx=(0, 8), pady=2)
entry_search_receta.bind("<KeyRelease>", lambda e: refrescar_lista_recetas_admin())

_mostrar_form_rec = False
_receta_en_edicion_nombre_original = None

def alternar_mostrar_form_receta():
    global _mostrar_form_rec
    _mostrar_form_rec = not _mostrar_form_rec
    if _mostrar_form_rec:
        f_rec_form_card.pack(fill="x", pady=(0, 10), before=frame_tabla_rec)
        btn_nueva_rec.configure(text="▲ OCULTAR FORMULARIO")
        try:
            view_recetas._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass
    else:
        f_rec_form_card.pack_forget()
        btn_nueva_rec.configure(text="+ NUEVA RECETA")

btn_nueva_rec = ctk.CTkButton(
    f_rec_top_bar,
    text="+ NUEVA RECETA",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=36,
    width=140,
    command=alternar_mostrar_form_receta
)
btn_nueva_rec.pack(side="right")

lbl_contador_recetas = ctk.CTkLabel(
    f_rec_top_bar,
    text=f"Recetas: {len(config_actual.get('recetas', []))}/99",
    font=(FONT_MAIN, 11, "bold"),
    text_color=COLOR_KOSTAL_DARK
)
lbl_contador_recetas.pack(side="right", padx=(0, 14))

# Formulario de Creación / Modificación de Receta
f_rec_form_card = ctk.CTkFrame(view_recetas, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)

f_rec_form_grid = ctk.CTkFrame(f_rec_form_card, fg_color="transparent")
f_rec_form_grid.pack(fill="x", padx=16, pady=12)
f_rec_form_grid.columnconfigure(0, weight=0)
f_rec_form_grid.columnconfigure(1, weight=1)
f_rec_form_grid.columnconfigure(2, weight=0)
f_rec_form_grid.columnconfigure(3, weight=1)

lbl_form_rec_titulo = ctk.CTkLabel(f_rec_form_grid, text="Formulario de Receta", font=(FONT_MAIN, 13, "bold"), text_color=COLOR_KOSTAL_DARK)
lbl_form_rec_titulo.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

ctk.CTkLabel(f_rec_form_grid, text="Nombre Receta:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=1, column=0, sticky="w", pady=6)
entry_rec_nom = ctk.CTkEntry(f_rec_form_grid, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MAIN, 13, "bold"))
entry_rec_nom.insert(0, "Botón A")
entry_rec_nom.grid(row=1, column=1, sticky="ew", padx=6, pady=6)

ctk.CTkLabel(f_rec_form_grid, text="Nombre de la Pieza:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=1, column=2, sticky="w", padx=(14, 0), pady=6)
entry_rec_pieza = ctk.CTkEntry(f_rec_form_grid, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MAIN, 13, "bold"))
entry_rec_pieza.insert(0, "Emblema KOSTAL Gotita")
entry_rec_pieza.grid(row=1, column=3, sticky="ew", padx=6, pady=6)

ctk.CTkLabel(f_rec_form_grid, text="Modelo:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=2, column=0, sticky="w", pady=6)
entry_rec_mod = ctk.CTkEntry(f_rec_form_grid, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MAIN, 13, "bold"))
entry_rec_mod.insert(0, "KSL-001")
entry_rec_mod.grid(row=2, column=1, sticky="ew", padx=6, pady=6)

ctk.CTkLabel(f_rec_form_grid, text="Velocidad (mm/s):", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=2, column=2, sticky="w", padx=(14, 0), pady=6)
entry_rec_vel = ctk.CTkEntry(f_rec_form_grid, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MAIN, 13, "bold"))
entry_rec_vel.insert(0, "115")
entry_rec_vel.grid(row=2, column=3, sticky="ew", padx=6, pady=6)

ctk.CTkLabel(f_rec_form_grid, text="Tamaño Matriz:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=3, column=0, sticky="w", pady=6)
f_mat_select = ctk.CTkFrame(f_rec_form_grid, fg_color="transparent")
f_mat_select.grid(row=3, column=1, sticky="w", padx=6, pady=6)
ctk.CTkLabel(f_mat_select, text="Filas:", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left")
entry_rec_filas = ctk.CTkEntry(f_mat_select, width=75, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MONO, 14, "bold"), justify="center")
entry_rec_filas.insert(0, "3")
entry_rec_filas.pack(side="left", padx=4)
entry_rec_filas.set = lambda v: (entry_rec_filas.delete(0, tk.END), entry_rec_filas.insert(0, str(v)))
option_rec_filas = entry_rec_filas

ctk.CTkLabel(f_mat_select, text="Cols:", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left", padx=(8, 0))
entry_rec_cols = ctk.CTkEntry(f_mat_select, width=75, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MONO, 14, "bold"), justify="center")
entry_rec_cols.insert(0, "5")
entry_rec_cols.pack(side="left", padx=4)
entry_rec_cols.set = lambda v: (entry_rec_cols.delete(0, tk.END), entry_rec_cols.insert(0, str(v)))
option_rec_cols = entry_rec_cols

ctk.CTkLabel(f_rec_form_grid, text="Dist. Centro a Centro:", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=3, column=2, sticky="w", padx=(14, 0), pady=6)
f_dist_inputs = ctk.CTkFrame(f_rec_form_grid, fg_color="transparent")
f_dist_inputs.grid(row=3, column=3, sticky="w", padx=6, pady=6)
ctk.CTkLabel(f_dist_inputs, text="ΔX (mm):", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left")
entry_rec_dx = ctk.CTkEntry(f_dist_inputs, width=85, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MONO, 13, "bold"), justify="center")
entry_rec_dx.insert(0, "42.0")
entry_rec_dx.pack(side="left", padx=4)
ctk.CTkLabel(f_dist_inputs, text="ΔY (mm):", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left", padx=(8, 0))
entry_rec_dy = ctk.CTkEntry(f_dist_inputs, width=85, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MONO, 13, "bold"), justify="center")
entry_rec_dy.insert(0, "36.0")
entry_rec_dy.pack(side="left", padx=4)

# Fila 4: Coordenadas iniciales de origen (Set 0 / Pieza 01) + Rectángulo de Piezas por Jig
ctk.CTkLabel(f_rec_form_grid, text="Coord. Inicial (Set 0):", font=(FONT_MAIN, 11, "bold"), text_color=TEXT_MUTED).grid(row=4, column=0, sticky="w", pady=6)
f_coord_inputs = ctk.CTkFrame(f_rec_form_grid, fg_color="transparent")
f_coord_inputs.grid(row=4, column=1, sticky="w", padx=6, pady=6)
ctk.CTkLabel(f_coord_inputs, text="X0 (mm):", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left")
entry_rec_x0 = ctk.CTkEntry(f_coord_inputs, width=85, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MONO, 13, "bold"), justify="center")
entry_rec_x0.insert(0, "0.00")
entry_rec_x0.pack(side="left", padx=4)
ctk.CTkLabel(f_coord_inputs, text="Y0 (mm):", font=(FONT_MAIN, 11), text_color=TEXT_MUTED).pack(side="left", padx=(8, 0))
entry_rec_y0 = ctk.CTkEntry(f_coord_inputs, width=85, height=42, corner_radius=8, border_width=2, border_color="#CBD5E1", text_color=TEXT_PRIMARY, font=(FONT_MONO, 13, "bold"), justify="center")
entry_rec_y0.insert(0, "0.00")
entry_rec_y0.pack(side="left", padx=4)

# Rectángulo destacado para pedir el Número de Piezas por Jig (independiente de la matriz)
f_piezas_jig_box = ctk.CTkFrame(
    f_rec_form_grid,
    fg_color="#EFF6FF",
    corner_radius=8,
    border_width=2,
    border_color="#0284C7"
)
f_piezas_jig_box.grid(row=3, rowspan=2, column=1, sticky="e", padx=(12, 10), pady=4)

ctk.CTkLabel(
    f_piezas_jig_box,
    text="Piezas por Jig:",
    font=(FONT_MAIN, 11, "bold"),
    text_color="#0369A1"
).pack(side="left", padx=(10, 6), pady=8)

entry_rec_piezas_jig = ctk.CTkEntry(
    f_piezas_jig_box,
    width=82,
    height=38,
    corner_radius=6,
    border_width=2,
    border_color="#0284C7",
    fg_color="#FFFFFF",
    text_color=COLOR_KOSTAL_NAVY,
    font=(FONT_MONO, 14, "bold"),
    justify="center"
)
entry_rec_piezas_jig.insert(0, "15")
entry_rec_piezas_jig.pack(side="left", padx=(0, 10), pady=8)

ctk.CTkLabel(f_rec_form_grid, text="ℹ️ Coordenada de origen (P01) guardada al hacer 'Set 0' en JOG.", font=(FONT_MAIN, 10), text_color="#0369A1").grid(row=4, column=2, columnspan=2, sticky="w", padx=(14, 0), pady=6)

# Fila 5: Imagen de la Pieza
ctk.CTkLabel(f_rec_form_grid, text="Imagen de la Pieza:", font=(FONT_MAIN, 10, "bold"), text_color=TEXT_MUTED).grid(row=5, column=0, sticky="w", pady=4)
f_img_picker = ctk.CTkFrame(f_rec_form_grid, fg_color="transparent")
f_img_picker.grid(row=5, column=1, columnspan=2, sticky="we", padx=6, pady=4)
entry_rec_img = ctk.CTkEntry(f_img_picker, width=260, fg_color="#FFFFFF", border_color=BORDER_CARD, text_color=TEXT_PRIMARY, font=(FONT_MAIN, 10))
entry_rec_img.insert(0, os.path.join(BASE_DIR, "Assets", "840d61ad-a9f0-4cc2-8114-b74643920fd2.jpg"))
entry_rec_img.pack(side="left", fill="x", expand=True)

def seleccionar_imagen_receta():
    archivo = filedialog.askopenfilename(
        title="Seleccionar Imagen de la Pieza",
        filetypes=[("Imágenes", "*.jpg *.jpeg *.png *.webp *.bmp"), ("Todos los archivos", "*.*")]
    )
    if archivo:
        entry_rec_img.delete(0, tk.END)
        entry_rec_img.insert(0, archivo)
        actualizar_preview_formulario_receta(archivo)

btn_sel_img = ctk.CTkButton(
    f_img_picker,
    text="📁 Explorar...",
    width=90,
    height=28,
    font=(FONT_MAIN, 10, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    command=seleccionar_imagen_receta
)
btn_sel_img.pack(side="left", padx=(6, 0))

f_form_img_thumb = ctk.CTkFrame(f_rec_form_grid, fg_color="#FFFFFF", width=90, height=65, corner_radius=6, border_width=1, border_color=BORDER_CARD)
f_form_img_thumb.grid(row=5, column=3, sticky="w", padx=6, pady=4)
f_form_img_thumb.pack_propagate(False)

lbl_rec_form_preview = ctk.CTkLabel(f_form_img_thumb, text="[Preview]", font=(FONT_MAIN, 8), text_color=TEXT_MUTED)
lbl_rec_form_preview.pack(expand=True, fill="both")

_preview_cache_form = None
def actualizar_preview_formulario_receta(ruta):
    global _preview_cache_form
    img_tk = cargar_imagen(ruta, 85, 60)
    if img_tk:
        _preview_cache_form = img_tk
        lbl_rec_form_preview.configure(image=_preview_cache_form, text="")
    else:
        lbl_rec_form_preview.configure(image=None, text="[Sin Vista]")

actualizar_preview_formulario_receta(entry_rec_img.get())

f_rec_btns = ctk.CTkFrame(f_rec_form_card, fg_color="transparent")
f_rec_btns.pack(fill="x", padx=14, pady=(0, 12))

def limpiar_campos_receta():
    global _receta_en_edicion_nombre_original
    _receta_en_edicion_nombre_original = None
    if 'lbl_form_rec_titulo' in globals():
        lbl_form_rec_titulo.configure(text="Formulario de Receta (Nueva)", text_color=COLOR_KOSTAL_DARK)
    if 'btn_guardar_rec' in globals():
        btn_guardar_rec.configure(text="💾 Guardar Receta", fg_color=COLOR_KOSTAL_NAVY, hover_color=COLOR_KOSTAL_LIGHT)
    if 'btn_cancelar_edicion' in globals():
        btn_cancelar_edicion.pack_forget()
    entry_rec_nom.delete(0, tk.END)
    entry_rec_pieza.delete(0, tk.END)
    entry_rec_mod.delete(0, tk.END)
    entry_rec_vel.delete(0, tk.END)
    entry_rec_vel.insert(0, "115")
    entry_rec_dx.delete(0, tk.END)
    entry_rec_dx.insert(0, "30.0")
    entry_rec_dy.delete(0, tk.END)
    entry_rec_dy.insert(0, "30.0")
    if 'entry_rec_x0' in globals():
        entry_rec_x0.delete(0, tk.END)
        entry_rec_x0.insert(0, "0.00")
    if 'entry_rec_y0' in globals():
        entry_rec_y0.delete(0, tk.END)
        entry_rec_y0.insert(0, "0.00")
    entry_rec_img.delete(0, tk.END)
    option_rec_filas.set("3")
    option_rec_cols.set("5")
    if 'entry_rec_piezas_jig' in globals():
        entry_rec_piezas_jig.delete(0, tk.END)
        entry_rec_piezas_jig.insert(0, "15")
    lbl_rec_form_preview.configure(image=None, text="[Preview]")
    if 'f_rec_items_container' in globals():
        ventana.after(50, refrescar_lista_recetas_admin)

def guardar_o_actualizar_receta():
    global _receta_en_edicion_nombre_original
    nom = entry_rec_nom.get().strip()
    pieza = entry_rec_pieza.get().strip()
    modelo = entry_rec_mod.get().strip()
    
    if not nom or not pieza or not modelo:
        messagebox.showerror("Error", "Nombre de receta, nombre de la pieza y modelo son obligatorios.", parent=ventana)
        return
        
    try:
        filas = max(1, int(str(option_rec_filas.get()).strip()))
        cols = max(1, int(str(option_rec_cols.get()).strip()))
        pz_jig_str = str(entry_rec_piezas_jig.get()).strip() if 'entry_rec_piezas_jig' in globals() else ""
        piezas_jig = max(1, int(float(pz_jig_str))) if pz_jig_str else (filas * cols)
        dx = float(str(entry_rec_dx.get()).strip().replace(',', '.'))
        dy = float(str(entry_rec_dy.get()).strip().replace(',', '.'))
        vel = int(float(str(entry_rec_vel.get()).strip().replace(',', '.')))
        x0_val = float(str(entry_rec_x0.get()).strip().replace(',', '.')) if 'entry_rec_x0' in globals() else 0.0
        y0_val = float(str(entry_rec_y0.get()).strip().replace(',', '.')) if 'entry_rec_y0' in globals() else 0.0
    except ValueError:
        messagebox.showerror("Error", "Verifique que filas, columnas, piezas por jig, distancias, coordenadas iniciales y velocidad sean valores numéricos válidos.", parent=ventana)
        return
        
    img_path = entry_rec_img.get().strip()
    
    receta_dict = {
        "nombre": nom,
        "pieza": pieza,
        "modelo": modelo,
        "filas": filas,
        "cols": cols,
        "piezas": piezas_jig,
        "piezas_por_jig": piezas_jig,
        "delta_x": round(dx, 2),
        "delta_y": round(dy, 2),
        "velocidad": vel,
        "imagen": img_path
    }
    
    recetas_lista = config_actual.get("recetas", [])
    
    # Caso 1: Se está editando una receta existente previamente cargada
    if _receta_en_edicion_nombre_original:
        idx_edicion = -1
        for i, r in enumerate(recetas_lista):
            if r.get("nombre") == _receta_en_edicion_nombre_original:
                idx_edicion = i
                break
                
        if idx_edicion >= 0:
            receta_previa = recetas_lista[idx_edicion]
            receta_dict["cero_guardado_x"] = x0_val
            receta_dict["cero_guardado_y"] = y0_val
            receta_dict["cero_calibrado"] = bool(receta_previa.get("cero_calibrado", False) or (x0_val > 0.0 or y0_val > 0.0))
            receta_dict["corridas"] = receta_previa.get("corridas", 0)
            recetas_lista[idx_edicion] = receta_dict
            
            # Si se le cambió el nombre y era la receta activa, actualizar nombre en config
            if config_actual.get("receta_activa") == _receta_en_edicion_nombre_original:
                config_actual["receta_activa"] = nom
                
            msg = f"Receta '{nom}' actualizada con éxito."
            _receta_en_edicion_nombre_original = None
        else:
            receta_dict["cero_guardado_x"] = x0_val
            receta_dict["cero_guardado_y"] = y0_val
            receta_dict["cero_calibrado"] = bool(x0_val > 0.0 or y0_val > 0.0)
            receta_dict["corridas"] = 0
            recetas_lista.append(receta_dict)
            msg = f"Receta '{nom}' guardada con éxito."
            _receta_en_edicion_nombre_original = None
            
    else:
        # Caso 2: Es una receta nueva -> Validar límite máximo de 99 recetas
        idx_existente = -1
        for i, r in enumerate(recetas_lista):
            if r.get("nombre") == nom:
                idx_existente = i
                break
                
        if idx_existente >= 0:
            receta_previa = recetas_lista[idx_existente]
            receta_dict["cero_guardado_x"] = x0_val
            receta_dict["cero_guardado_y"] = y0_val
            receta_dict["cero_calibrado"] = bool(receta_previa.get("cero_calibrado", False) or (x0_val > 0.0 or y0_val > 0.0))
            receta_dict["corridas"] = receta_previa.get("corridas", 0)
            recetas_lista[idx_existente] = receta_dict
            msg = f"Receta '{nom}' actualizada con éxito."
        else:
            if len(recetas_lista) >= 99:
                messagebox.showwarning(
                    "Límite de Recetas",
                    "Se ha alcanzado el límite máximo de 99 recetas configuradas en el sistema.\n"
                    "Para agregar una nueva, debe eliminar o configurar alguna existente.",
                    parent=ventana
                )
                return
            receta_dict["cero_guardado_x"] = x0_val
            receta_dict["cero_guardado_y"] = y0_val
            receta_dict["cero_calibrado"] = bool(x0_val > 0.0 or y0_val > 0.0)
            receta_dict["corridas"] = 0
            recetas_lista.append(receta_dict)
            msg = f"Nueva receta '{nom}' registrada con éxito ({len(recetas_lista)}/99)."
        
    config_actual["recetas"] = recetas_lista
    guardar_configuracion()
    
    nombres_actuales = [r["nombre"] for r in config_actual["recetas"]]
    if 'option_receta' in globals():
        option_receta.configure(values=nombres_actuales)
        if config_actual.get("receta_activa") == nom:
            option_receta.set(nom)
        
    if config_actual.get("receta_activa") == nom:
        cargar_receta_por_nombre(nom, mostrar_modal_aviso=False)
        
    limpiar_campos_receta()
    refrescar_lista_recetas_admin()
    messagebox.showinfo("Receta Guardada", msg, parent=ventana)
    lbl_estado.configure(text=f"✓ {msg}", text_color=COLOR_OK)

btn_guardar_rec = ctk.CTkButton(
    f_rec_btns,
    text="💾 Guardar Receta",
    font=(FONT_MAIN, 12, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=8,
    height=42,
    command=guardar_o_actualizar_receta
)
btn_guardar_rec.pack(side="left", padx=(0, 10))

btn_cancelar_edicion = ctk.CTkButton(
    f_rec_btns,
    text="✕ Cancelar Edición",
    font=(FONT_MAIN, 12, "bold"),
    fg_color="#FEE2E2",
    hover_color="#FECACA",
    text_color="#DC2626",
    corner_radius=8,
    height=42,
    command=limpiar_campos_receta
)
btn_cancelar_edicion.pack(side="left", padx=(0, 10))
btn_cancelar_edicion.pack_forget()

btn_limpiar_rec = ctk.CTkButton(
    f_rec_btns,
    text="🔄 Limpiar",
    font=(FONT_MAIN, 12, "bold"),
    fg_color="#FFFFFF",
    hover_color=BORDER_LIGHT,
    text_color=TEXT_PRIMARY,
    border_width=1,
    border_color=BORDER_CARD,
    corner_radius=8,
    height=42,
    command=limpiar_campos_receta
)
btn_limpiar_rec.pack(side="left")

# Enlazar teclas Enter para navegación rápida entre campos y guardado de receta
entry_search_receta.bind("<Return>", lambda e: (ocultar_teclado_nativo(), refrescar_lista_recetas_admin(), "break")[2])
entry_search_receta.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), refrescar_lista_recetas_admin(), "break")[2])
entry_rec_nom.bind("<Return>", lambda e: (entry_rec_pieza.focus_set(), "break")[1])
entry_rec_nom.bind("<KP_Enter>", lambda e: (entry_rec_pieza.focus_set(), "break")[1])
entry_rec_pieza.bind("<Return>", lambda e: (entry_rec_mod.focus_set(), "break")[1])
entry_rec_pieza.bind("<KP_Enter>", lambda e: (entry_rec_mod.focus_set(), "break")[1])
entry_rec_mod.bind("<Return>", lambda e: (entry_rec_vel.focus_set(), "break")[1])
entry_rec_mod.bind("<KP_Enter>", lambda e: (entry_rec_vel.focus_set(), "break")[1])
entry_rec_vel.bind("<Return>", lambda e: (entry_rec_filas.focus_set(), "break")[1])
entry_rec_vel.bind("<KP_Enter>", lambda e: (entry_rec_filas.focus_set(), "break")[1])
entry_rec_filas.bind("<Return>", lambda e: (entry_rec_cols.focus_set(), "break")[1])
entry_rec_filas.bind("<KP_Enter>", lambda e: (entry_rec_cols.focus_set(), "break")[1])
entry_rec_cols.bind("<Return>", lambda e: (entry_rec_dx.focus_set(), "break")[1])
entry_rec_cols.bind("<KP_Enter>", lambda e: (entry_rec_dx.focus_set(), "break")[1])
entry_rec_dx.bind("<Return>", lambda e: (entry_rec_dy.focus_set(), "break")[1])
entry_rec_dx.bind("<KP_Enter>", lambda e: (entry_rec_dy.focus_set(), "break")[1])
entry_rec_dy.bind("<Return>", lambda e: (entry_rec_x0.focus_set(), "break")[1])
entry_rec_dy.bind("<KP_Enter>", lambda e: (entry_rec_x0.focus_set(), "break")[1])
entry_rec_x0.bind("<Return>", lambda e: (entry_rec_y0.focus_set(), "break")[1])
entry_rec_x0.bind("<KP_Enter>", lambda e: (entry_rec_y0.focus_set(), "break")[1])
entry_rec_y0.bind("<Return>", lambda e: (entry_rec_piezas_jig.focus_set(), "break")[1])
entry_rec_y0.bind("<KP_Enter>", lambda e: (entry_rec_piezas_jig.focus_set(), "break")[1])
entry_rec_piezas_jig.bind("<Return>", lambda e: (entry_rec_img.focus_set(), "break")[1])
entry_rec_piezas_jig.bind("<KP_Enter>", lambda e: (entry_rec_img.focus_set(), "break")[1])
entry_rec_img.bind("<Return>", lambda e: (ocultar_teclado_nativo(), guardar_o_actualizar_receta(), "break")[2])
entry_rec_img.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), guardar_o_actualizar_receta(), "break")[2])

# Tabla Catálogo de Recetas Fiel a la Imagen 4
frame_tabla_rec = ctk.CTkFrame(view_recetas, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
frame_tabla_rec.pack(fill="both", expand=True, pady=4)

cols_rec = ["NOMBRE", "MODELO", "CÓDIGO", "MATRIZ", "COORD. INICIAL (P01)", "DIST. CENTRO X", "DIST. CENTRO Y", "ÚLTIMA MODIF.", "ESTADO", "ACCIONES"]
col_weights_rec = [3, 2, 1, 2, 3, 2, 2, 2, 2, 3]

f_rec_tbl_hdr = ctk.CTkFrame(frame_tabla_rec, fg_color="#F8FAFC", height=38, corner_radius=0)
f_rec_tbl_hdr.pack(fill="x", padx=1, pady=1)

for c_i, w in enumerate(col_weights_rec):
    f_rec_tbl_hdr.columnconfigure(c_i, weight=w, uniform="col_rec")

for c_idx, c_nom in enumerate(cols_rec):
    ctk.CTkLabel(f_rec_tbl_hdr, text=c_nom, font=(FONT_MAIN, 9, "bold"), text_color="#475569").grid(row=0, column=c_idx, sticky="ew", padx=2, pady=6)

f_rec_items_container = ctk.CTkFrame(frame_tabla_rec, fg_color="transparent")
f_rec_items_container.pack(fill="both", expand=True, padx=1, pady=1)

def editar_receta_formulario(r):
    global _mostrar_form_rec, _receta_en_edicion_nombre_original
    _receta_en_edicion_nombre_original = r.get("nombre", "")
    if not _mostrar_form_rec:
        alternar_mostrar_form_receta()
        
    if 'lbl_form_rec_titulo' in globals():
        lbl_form_rec_titulo.configure(text=f"✏️ Configurar Receta: '{_receta_en_edicion_nombre_original}'", text_color="#1E3A8A")
    if 'btn_guardar_rec' in globals():
        btn_guardar_rec.configure(text="💾 Actualizar Receta", fg_color="#166534", hover_color="#15803D")
    if 'btn_cancelar_edicion' in globals():
        btn_cancelar_edicion.pack(side="left", padx=(0, 8))
        
    entry_rec_nom.delete(0, tk.END)
    entry_rec_nom.insert(0, r.get("nombre", ""))
    entry_rec_pieza.delete(0, tk.END)
    entry_rec_pieza.insert(0, r.get("pieza", ""))
    entry_rec_mod.delete(0, tk.END)
    entry_rec_mod.insert(0, r.get("modelo", ""))
    option_rec_filas.set(str(r.get("filas", 3)))
    option_rec_cols.set(str(r.get("cols", 5)))
    if 'entry_rec_piezas_jig' in globals():
        entry_rec_piezas_jig.delete(0, tk.END)
        entry_rec_piezas_jig.insert(0, str(obtener_piezas_por_jig_receta(r)))
    entry_rec_dx.delete(0, tk.END)
    entry_rec_dx.insert(0, str(r.get("delta_x", 25.0)))
    entry_rec_dy.delete(0, tk.END)
    entry_rec_dy.insert(0, str(r.get("delta_y", 30.0)))
    if 'entry_rec_x0' in globals():
        entry_rec_x0.delete(0, tk.END)
        entry_rec_x0.insert(0, f"{float(r.get('cero_guardado_x', 0.0)):.2f}")
    if 'entry_rec_y0' in globals():
        entry_rec_y0.delete(0, tk.END)
        entry_rec_y0.insert(0, f"{float(r.get('cero_guardado_y', 0.0)):.2f}")
    entry_rec_vel.delete(0, tk.END)
    entry_rec_vel.insert(0, str(r.get("velocidad", 100)))
    entry_rec_img.delete(0, tk.END)
    entry_rec_img.insert(0, str(r.get("imagen", "")))
    actualizar_preview_formulario_receta(r.get("imagen", ""))
    
    try:
        view_recetas._parent_canvas.yview_moveto(0.0)
    except Exception:
        pass
        
    ventana.after(50, refrescar_lista_recetas_admin)

def eliminar_receta_accion(r_nom):
    global _receta_en_edicion_nombre_original
    if len(config_actual.get("recetas", [])) <= 1:
        messagebox.showwarning("Atención", "Debe existir al menos una receta en el sistema.", parent=ventana)
        return
    if messagebox.askyesno("Confirmar Eliminación", f"¿Está seguro de eliminar la receta '{r_nom}'?", parent=ventana):
        config_actual["recetas"] = [r for r in config_actual["recetas"] if r.get("nombre") != r_nom]
        if _receta_en_edicion_nombre_original == r_nom:
            limpiar_campos_receta()
            
        if config_actual.get("receta_activa") == r_nom:
            config_actual["receta_activa"] = config_actual["recetas"][0]["nombre"]
            cargar_receta_por_nombre(config_actual["receta_activa"], mostrar_modal_aviso=False)
            
        guardar_configuracion()
        nombres_actuales = [r["nombre"] for r in config_actual["recetas"]]
        if 'option_receta' in globals():
            option_receta.configure(values=nombres_actuales)
            option_receta.set(config_actual["receta_activa"])
        refrescar_lista_recetas_admin()
        messagebox.showinfo("Eliminada", f"Receta '{r_nom}' eliminada correctamente ({len(config_actual['recetas'])}/99).", parent=ventana)

def refrescar_lista_recetas_admin():
    for w in f_rec_items_container.winfo_children():
        w.destroy()
        
    recetas_actuales = config_actual.get("recetas", [])
    if 'lbl_contador_recetas' in globals():
        lbl_contador_recetas.configure(text=f"Recetas: {len(recetas_actuales)}/99")
        
    receta_activa_actual = config_actual.get("receta_activa", "")
    filtro_txt = entry_search_receta.get().lower().strip() if 'entry_search_receta' in globals() else ""
    
    fechas_mod = ["25/08/2026 10:42", "24/08/2026 14:15", "22/08/2026 11:30", "20/08/2026 09:10"]
    prog_codes = ["P01", "P02", "P03", "P04"]
    
    for idx, r in enumerate(recetas_actuales):
        nom = r.get("nombre", "Receta")
        if filtro_txt and filtro_txt not in nom.lower() and filtro_txt not in r.get("modelo", "").lower():
            continue
            
        es_activa = (nom == receta_activa_actual)
        en_edicion = (nom == _receta_en_edicion_nombre_original)
        
        bg_fila = "#EFF6FF" if en_edicion else ("#FFFFFF" if idx % 2 == 0 else "#F8FAFC")
        bdr_color = "#3B82F6" if en_edicion else bg_fila
        bdr_w = 1 if en_edicion else 0

        f_row = ctk.CTkFrame(f_rec_items_container, fg_color=bg_fila, border_color=bdr_color, border_width=bdr_w, height=40, corner_radius=0)
        f_row.pack(fill="x", padx=0, pady=1)
        
        for c_i, w in enumerate(col_weights_rec):
            f_row.columnconfigure(c_i, weight=w, uniform="col_rec")
            
        # 1. NOMBRE
        nom_txt = f"✏️ {nom}" if en_edicion else nom
        ctk.CTkLabel(f_row, text=nom_txt, font=(FONT_MAIN, 11, "bold"), text_color="#1E3A8A" if en_edicion else COLOR_KOSTAL_DARK).grid(row=0, column=0, sticky="ew", padx=2, pady=4)
        # 2. MODELO
        ctk.CTkLabel(f_row, text=r.get("modelo", "KSL-001"), font=(FONT_MONO, 11), text_color=TEXT_PRIMARY).grid(row=0, column=1, sticky="ew", padx=2, pady=4)
        # 3. PROGRAMA
        prog_id = prog_codes[idx % len(prog_codes)]
        ctk.CTkLabel(f_row, text=prog_id, font=(FONT_MONO, 11, "bold"), text_color="#0284C7").grid(row=0, column=2, sticky="ew", padx=2, pady=4)
        # 4. MATRIZ Y PIEZAS POR JIG
        pz_rec = obtener_piezas_por_jig_receta(r)
        dim_txt = f"{r.get('filas', 3)}×{r.get('cols', 5)} ({pz_rec} pz)"
        ctk.CTkLabel(f_row, text=dim_txt, font=(FONT_MONO, 11), text_color=TEXT_PRIMARY).grid(row=0, column=3, sticky="ew", padx=2, pady=4)
        # 5. COORD. INICIAL (P01 / SET 0)
        cero_x = float(r.get("cero_guardado_x", 0.0))
        cero_y = float(r.get("cero_guardado_y", 0.0))
        ya_cal = bool(r.get("cero_calibrado", False) or (cero_x > 0.0 or cero_y > 0.0))
        f_coord_p01 = ctk.CTkFrame(f_row, fg_color="transparent")
        f_coord_p01.grid(row=0, column=4, sticky="ew", padx=2, pady=2)
        txt_coord = f"X: {cero_x:.2f} mm | Y: {cero_y:.2f} mm"
        col_coord = "#0284C7" if ya_cal else TEXT_MUTED
        ctk.CTkLabel(f_coord_p01, text=txt_coord, font=(FONT_MONO, 10, "bold"), text_color=col_coord).pack(anchor="center")
        # 6. DISTANCIA CENTRO EN X
        try:
            dx_num = float(str(r.get('delta_x', 25.0)).replace(',', '.'))
        except Exception:
            dx_num = 25.0
        ctk.CTkLabel(f_row, text=f"{dx_num:.2f} mm", font=(FONT_MONO, 11), text_color=TEXT_PRIMARY).grid(row=0, column=5, sticky="ew", padx=2, pady=4)
        # 7. DISTANCIA CENTRO EN Y
        try:
            dy_num = float(str(r.get('delta_y', 30.0)).replace(',', '.'))
        except Exception:
            dy_num = 30.0
        ctk.CTkLabel(f_row, text=f"{dy_num:.2f} mm", font=(FONT_MONO, 11), text_color=TEXT_PRIMARY).grid(row=0, column=6, sticky="ew", padx=2, pady=4)
        # 8. ÚLTIMA MODIFICACIÓN
        f_mod = fechas_mod[idx % len(fechas_mod)]
        ctk.CTkLabel(f_row, text=f_mod, font=(FONT_MONO, 10), text_color=TEXT_MUTED).grid(row=0, column=7, sticky="ew", padx=2, pady=4)
        # 9. ESTADO (Pill Activo / Inactivo)
        st_pill_bg = "#DCFCE7" if es_activa else "#F1F5F9"
        st_pill_fg = "#15803D" if es_activa else "#64748B"
        st_pill_txt = "● Activo" if es_activa else "● Inactivo"
        f_st = ctk.CTkFrame(f_row, fg_color="transparent")
        f_st.grid(row=0, column=8, sticky="ew", padx=2, pady=4)
        ctk.CTkLabel(f_st, text=st_pill_txt, font=(FONT_MAIN, 10, "bold"), fg_color=st_pill_bg, text_color=st_pill_fg, corner_radius=10, padx=8, pady=2).pack()
        # 10. ACCIONES: Configurar y Eliminar
        f_act = ctk.CTkFrame(f_row, fg_color="transparent")
        f_act.grid(row=0, column=9, sticky="ew", padx=2, pady=4)
        
        btn_cfg = ctk.CTkButton(
            f_act,
            text="⛭ Configurar",
            font=(FONT_MAIN, 10, "bold"),
            width=88,
            height=28,
            fg_color="#EFF6FF" if en_edicion else "#FFFFFF",
            hover_color=BORDER_LIGHT,
            text_color="#1E3A8A",
            border_width=1,
            border_color="#93C5FD" if en_edicion else BORDER_CARD,
            corner_radius=4,
            command=lambda rec=r: editar_receta_formulario(rec)
        )
        btn_cfg.pack(side="left", padx=2)
        
        btn_del = ctk.CTkButton(
            f_act,
            text="🗑",
            font=(FONT_MAIN, 11),
            width=28,
            height=28,
            fg_color="#FEF2F2",
            hover_color="#FEE2E2",
            text_color="#DC2626",
            border_width=1,
            border_color="#FECACA",
            corner_radius=4,
            command=lambda nom=r.get("nombre"): eliminar_receta_accion(nom)
        )
        btn_del.pack(side="left", padx=2)

refrescar_lista_recetas_admin()

# Habilitar desplazamiento táctil fluido (Touch Drag) en el catálogo de recetas
def habilitar_scroll_tactil_recetas():
    try:
        c = view_recetas._parent_canvas
        _touch_s = {"y": 0}
        def _on_p(e):
            _touch_s["y"] = e.y_root
        def _on_m(e):
            dy = _touch_s["y"] - e.y_root
            _touch_s["y"] = e.y_root
            if dy != 0:
                c.yview_scroll(int(dy), "units")
        c.bind("<ButtonPress-1>", _on_p, add="+")
        c.bind("<B1-Motion>", _on_m, add="+")
    except Exception as e:
        print(f"Scroll táctil no disponible en recetas: {e}")

habilitar_scroll_tactil_recetas()

# --- VISTA 3: GESTIÓN DE USUARIOS ---
# --- VISTA 3: GESTIÓN DE USUARIOS (IMAGEN 2) ---
# Barra Superior con Indicadores y Botón de Nuevo Usuario
f_usr_top_bar = ctk.CTkFrame(view_usuarios, fg_color="transparent")
f_usr_top_bar.pack(fill="x", pady=(0, 10))

f_usr_badges = ctk.CTkFrame(f_usr_top_bar, fg_color="transparent")
f_usr_badges.pack(side="left")

def crear_pill_stat(parent, label, valor, color_val=TEXT_PRIMARY):
    p = ctk.CTkFrame(parent, fg_color="#FFFFFF", corner_radius=14, border_width=1, border_color=BORDER_CARD, height=32)
    p.pack(side="left", padx=(0, 8))
    f_in = ctk.CTkFrame(p, fg_color="transparent")
    f_in.pack(padx=12, pady=4)
    ctk.CTkLabel(f_in, text=f"{label}: ", font=(FONT_MAIN, 9, "bold"), text_color=TEXT_MUTED).pack(side="left")
    ctk.CTkLabel(f_in, text=str(valor), font=(FONT_MAIN, 10, "bold"), text_color=color_val).pack(side="left")
    return p

lbl_pill_activos = crear_pill_stat(f_usr_badges, "Usuarios Activos", str(len([u for u in USUARIOS_SISTEMA])), COLOR_KOSTAL_DARK)
lbl_pill_ops     = crear_pill_stat(f_usr_badges, "Operadores", str(len([u for u in USUARIOS_SISTEMA if u.get("rol")=="OPERADOR"])), COLOR_KOSTAL_DARK)
lbl_pill_adms    = crear_pill_stat(f_usr_badges, "Administradores", str(len([u for u in USUARIOS_SISTEMA if u.get("rol")=="ADMIN"])), COLOR_PURPLE_TEXT)

_mostrar_form_usr = False
def alternar_mostrar_form_usuario():
    global _mostrar_form_usr
    _mostrar_form_usr = not _mostrar_form_usr
    if _mostrar_form_usr:
        f_usr_form_card.pack(fill="x", pady=(0, 10), before=frame_tabla_usr)
        btn_nuevo_usr.configure(text="▲ OCULTAR FORMULARIO")
    else:
        f_usr_form_card.pack_forget()
        btn_nuevo_usr.configure(text="+ NUEVO USUARIO")

btn_nuevo_usr = ctk.CTkButton(
    f_usr_top_bar,
    text="+ NUEVO USUARIO",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=36,
    width=140,
    command=alternar_mostrar_form_usuario
)
btn_nuevo_usr.pack(side="right")

# Formulario de Registro / Modificación de Usuario
f_usr_form_card = ctk.CTkFrame(view_usuarios, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)

f_usr_grid = ctk.CTkFrame(f_usr_form_card, fg_color="transparent")
f_usr_grid.pack(fill="x", padx=14, pady=12)

ctk.CTkLabel(f_usr_grid, text="Formulario de Usuario", font=(FONT_MAIN, 12, "bold"), text_color=COLOR_KOSTAL_DARK).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

ctk.CTkLabel(f_usr_grid, text="No. Empleado / ID:", font=(FONT_MAIN, 10, "bold"), text_color=TEXT_MUTED).grid(row=1, column=0, sticky="w", pady=4)
entry_usr_num = ctk.CTkEntry(f_usr_grid, width=170, fg_color="#FFFFFF", border_color=BORDER_CARD, text_color=TEXT_PRIMARY, font=(FONT_MONO, 11))
entry_usr_num.insert(0, f"KOSTAL-{len(USUARIOS_SISTEMA)+1:04d}")
entry_usr_num.grid(row=1, column=1, sticky="w", padx=6, pady=4)

ctk.CTkLabel(f_usr_grid, text="Nombre Completo:", font=(FONT_MAIN, 10, "bold"), text_color=TEXT_MUTED).grid(row=1, column=2, sticky="w", padx=(14, 0), pady=4)
entry_usr_nom = ctk.CTkEntry(f_usr_grid, width=220, fg_color="#FFFFFF", border_color=BORDER_CARD, text_color=TEXT_PRIMARY, font=(FONT_MAIN, 11))
entry_usr_nom.grid(row=1, column=3, sticky="w", padx=6, pady=4)

ctk.CTkLabel(f_usr_grid, text="Rol del Sistema:", font=(FONT_MAIN, 10, "bold"), text_color=TEXT_MUTED).grid(row=2, column=0, sticky="w", pady=4)
option_usr_rol = ctk.CTkOptionMenu(f_usr_grid, values=["OPERADOR", "ADMIN"], width=170, height=30, fg_color="#FFFFFF", text_color=TEXT_PRIMARY, button_color=BORDER_CARD, button_hover_color=BORDER_LIGHT)
option_usr_rol.set("OPERADOR")
option_usr_rol.grid(row=2, column=1, sticky="w", padx=6, pady=4)

ctk.CTkLabel(f_usr_grid, text="Contraseña:", font=(FONT_MAIN, 10, "bold"), text_color=TEXT_MUTED).grid(row=2, column=2, sticky="w", padx=(14, 0), pady=4)
entry_usr_pass = ctk.CTkEntry(f_usr_grid, width=220, fg_color="#FFFFFF", border_color=BORDER_CARD, text_color=TEXT_PRIMARY, font=(FONT_MONO, 11))
entry_usr_pass.insert(0, "1234")
entry_usr_pass.grid(row=2, column=3, sticky="w", padx=6, pady=4)

f_usr_form_btns = ctk.CTkFrame(f_usr_form_card, fg_color="transparent")
f_usr_form_btns.pack(fill="x", padx=14, pady=(0, 12))

def limpiar_formulario_usuario():
    entry_usr_num.delete(0, tk.END)
    entry_usr_num.insert(0, f"KOSTAL-{len(USUARIOS_SISTEMA)+1:04d}")
    entry_usr_nom.delete(0, tk.END)
    entry_usr_pass.delete(0, tk.END)
    entry_usr_pass.insert(0, "1234")
    option_usr_rol.set("OPERADOR")

def guardar_o_actualizar_usuario():
    global USUARIOS_SISTEMA
    num_val = entry_usr_num.get().strip()
    nom_val = entry_usr_nom.get().strip()
    rol_val = option_usr_rol.get().strip()
    pass_val = entry_usr_pass.get().strip()
    
    if not num_val or not nom_val or not pass_val:
        messagebox.showerror("Error", "Todos los campos de usuario son obligatorios.")
        return
        
    usr_obj = {"num": num_val, "nombre": nom_val, "rol": rol_val, "pass": pass_val}
    
    idx_existente = -1
    for i, u in enumerate(USUARIOS_SISTEMA):
        if u["num"] == num_val or u["nombre"].lower() == nom_val.lower():
            idx_existente = i
            break
            
    if idx_existente >= 0:
        usr_obj["tutorial_completado"] = USUARIOS_SISTEMA[idx_existente].get("tutorial_completado", True)
        USUARIOS_SISTEMA[idx_existente] = usr_obj
        msg = f"Usuario '{nom_val}' actualizado correctamente."
    else:
        # REQUERIMIENTO OBLIGATORIO: Nuevo usuario debe completar el tutorial
        usr_obj["tutorial_completado"] = False
        USUARIOS_SISTEMA.append(usr_obj)
        msg = f"Nuevo usuario '{nom_val}' registrado exitosamente.\n(Se le exigirá completar el tutorial obligatorio en su primer inicio de sesión)."
        
    config_actual["usuarios"] = list(USUARIOS_SISTEMA)
    guardar_configuracion()
    
    # Sincronizar combo de Login
    if 'option_usuario' in globals():
        option_usuario.configure(values=[f"{u['nombre']} ({u['rol']})" for u in USUARIOS_SISTEMA])
        
    refrescar_lista_usuarios_admin()
    limpiar_formulario_usuario()
    messagebox.showinfo("Usuario Guardado", msg)
    lbl_estado.configure(text=f"✓ {msg}", text_color=COLOR_OK)

btn_guardar_usr = ctk.CTkButton(
    f_usr_form_btns,
    text="💾 Guardar Usuario",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=34,
    command=guardar_o_actualizar_usuario
)
btn_guardar_usr.pack(side="left", padx=(0, 8))

btn_limpiar_usr = ctk.CTkButton(
    f_usr_form_btns,
    text="🔄 Limpiar",
    font=(FONT_MAIN, 11),
    fg_color="#FFFFFF",
    hover_color=BORDER_LIGHT,
    text_color=TEXT_PRIMARY,
    border_width=1,
    border_color=BORDER_CARD,
    corner_radius=6,
    height=34,
    command=limpiar_formulario_usuario
)
btn_limpiar_usr.pack(side="left")

# Enlazar teclas Enter para navegación rápida entre campos y guardado de usuario
entry_usr_num.bind("<Return>", lambda e: (entry_usr_nom.focus_set(), "break")[1])
entry_usr_num.bind("<KP_Enter>", lambda e: (entry_usr_nom.focus_set(), "break")[1])
entry_usr_nom.bind("<Return>", lambda e: (entry_usr_pass.focus_set(), "break")[1])
entry_usr_nom.bind("<KP_Enter>", lambda e: (entry_usr_pass.focus_set(), "break")[1])
entry_usr_pass.bind("<Return>", lambda e: (ocultar_teclado_nativo(), guardar_o_actualizar_usuario(), "break")[2])
entry_usr_pass.bind("<KP_Enter>", lambda e: (ocultar_teclado_nativo(), guardar_o_actualizar_usuario(), "break")[2])

# Tabla de Usuarios Fiel al Mockup Oficial (Imagen 2)
frame_tabla_usr = ctk.CTkFrame(view_usuarios, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
frame_tabla_usr.pack(fill="both", expand=True, pady=4)

cols_usr = ["ID EMPLEADO", "NOMBRE COMPLETO", "NOMBRE DE USUARIO", "ROL", "ESTADO", "ÚLTIMO ACCESO", "ACCIONES"]
f_usr_tbl_hdr = ctk.CTkFrame(frame_tabla_usr, fg_color="#F8FAFC", height=34, corner_radius=0)
f_usr_tbl_hdr.pack(fill="x", padx=1, pady=1)

for c_nom in cols_usr:
    ctk.CTkLabel(f_usr_tbl_hdr, text=c_nom, font=(FONT_MAIN, 9, "bold"), text_color="#475569").pack(side="left", expand=True)

f_usr_cards_container = ctk.CTkFrame(frame_tabla_usr, fg_color="transparent")
f_usr_cards_container.pack(fill="both", expand=True, padx=1, pady=1)

def editar_usuario_formulario(u):
    global _mostrar_form_usr
    if not _mostrar_form_usr:
        alternar_mostrar_form_usuario()
    entry_usr_num.delete(0, tk.END)
    entry_usr_num.insert(0, u.get("num", ""))
    entry_usr_nom.delete(0, tk.END)
    entry_usr_nom.insert(0, u.get("nombre", ""))
    option_usr_rol.set(u.get("rol", "OPERADOR"))
    entry_usr_pass.delete(0, tk.END)
    entry_usr_pass.insert(0, u.get("pass", ""))

def eliminar_usuario_accion(num_id, nom_usr):
    global USUARIOS_SISTEMA
    admins_restantes = [u for u in USUARIOS_SISTEMA if u.get("rol") == "ADMIN" and u.get("num") != num_id]
    user_to_del = next((u for u in USUARIOS_SISTEMA if u.get("num") == num_id), None)
    if user_to_del and user_to_del.get("rol") == "ADMIN" and not admins_restantes:
        messagebox.showerror("Acción Denegada", "No se puede eliminar el único Administrador del sistema.")
        return
        
    if messagebox.askyesno("Confirmar Eliminación", f"¿Desea eliminar al usuario '{nom_usr}' ({num_id})?"):
        USUARIOS_SISTEMA = [u for u in USUARIOS_SISTEMA if u.get("num") != num_id]
        config_actual["usuarios"] = list(USUARIOS_SISTEMA)
        guardar_configuracion()
        if 'option_usuario' in globals():
            option_usuario.configure(values=[f"{u['nombre']} ({u['rol']})" for u in USUARIOS_SISTEMA])
            option_usuario.set(f"{USUARIOS_SISTEMA[0]['nombre']} ({USUARIOS_SISTEMA[0]['rol']})")
        refrescar_lista_usuarios_admin()
        messagebox.showinfo("Eliminado", f"Usuario '{nom_usr}' eliminado.")

def refrescar_lista_usuarios_admin():
    for w in f_usr_cards_container.winfo_children():
        w.destroy()
        
    for idx, u in enumerate(USUARIOS_SISTEMA):
        bg_fila = "#FFFFFF" if idx % 2 == 0 else "#F8FAFC"
        f_u_row = ctk.CTkFrame(f_usr_cards_container, fg_color=bg_fila, height=36, corner_radius=0)
        f_u_row.pack(fill="x", padx=0, pady=1)
        
        # ID Empleado
        ctk.CTkLabel(f_u_row, text=u.get("num", "KOSTAL-0001"), font=(FONT_MONO, 10, "bold"), text_color=TEXT_PRIMARY).pack(side="left", expand=True)
        # Nombre Completo
        ctk.CTkLabel(f_u_row, text=u.get("nombre", "Usuario"), font=(FONT_MAIN, 10, "bold"), text_color=COLOR_KOSTAL_DARK).pack(side="left", expand=True)
        # Nombre de Usuario (azul como en Imagen 2)
        u_handle = u.get("nombre", "").lower().replace(" ", ".")[:10]
        ctk.CTkLabel(f_u_row, text=u_handle, font=(FONT_MONO, 10), text_color="#2563EB").pack(side="left", expand=True)
        
        # Rol (Badge Imagen 2)
        rol = u.get("rol", "OPERADOR")
        c_bg = "#F1F5F9" if rol == "OPERADOR" else COLOR_PURPLE_SOFT
        c_fg = "#475569" if rol == "OPERADOR" else COLOR_PURPLE_TEXT
        f_r_box = ctk.CTkFrame(f_u_row, fg_color="transparent")
        f_r_box.pack(side="left", expand=True)
        ctk.CTkLabel(f_r_box, text=rol, font=(FONT_MAIN, 8, "bold"), fg_color=c_bg, text_color=c_fg, corner_radius=4, padx=8, pady=2).pack()
        
        # Estado (Pill Activo / Inactivo Imagen 2)
        es_inactivo = (idx == 3) # Mock Pedro Picapiedra inactivo como en imagen 2
        st_pill_bg = "#F1F5F9" if es_inactivo else "#DCFCE7"
        st_pill_fg = "#64748B" if es_inactivo else "#15803D"
        st_pill_txt = "● Inactivo" if es_inactivo else "● Activo"
        f_s_box = ctk.CTkFrame(f_u_row, fg_color="transparent")
        f_s_box.pack(side="left", expand=True)
        ctk.CTkLabel(f_s_box, text=st_pill_txt, font=(FONT_MAIN, 9, "bold"), fg_color=st_pill_bg, text_color=st_pill_fg, corner_radius=10, padx=8, pady=2).pack()
        
        # Último Acceso
        fechas_mock = ["26/08/2026 09:22", "26/08/2026 07:31", "25/08/2026 07:30", "18/08/2026 15:42"]
        f_acc = fechas_mock[idx % len(fechas_mock)]
        ctk.CTkLabel(f_u_row, text=f_acc, font=(FONT_MONO, 9), text_color=TEXT_MUTED).pack(side="left", expand=True)
        
        # Acciones (Ver / Editar y Eliminar)
        f_act = ctk.CTkFrame(f_u_row, fg_color="transparent")
        f_act.pack(side="left", expand=True)
        
        ctk.CTkButton(
            f_act,
            text="✏️",
            font=(FONT_MAIN, 11),
            width=28,
            height=24,
            fg_color="#FFFFFF",
            hover_color=BORDER_LIGHT,
            text_color="#475569",
            border_width=1,
            border_color=BORDER_CARD,
            corner_radius=4,
            command=lambda user=u: editar_usuario_formulario(user)
        ).pack(side="left", padx=2)
        
        ctk.CTkButton(
            f_act,
            text="🗑",
            font=(FONT_MAIN, 10),
            width=28,
            height=24,
            fg_color="#FEF2F2",
            hover_color="#FEE2E2",
            text_color="#DC2626",
            border_width=1,
            border_color="#FECACA",
            corner_radius=4,
            command=lambda num=u.get("num"), nom=u.get("nombre"): eliminar_usuario_accion(num, nom)
        ).pack(side="left", padx=2)

refrescar_lista_usuarios_admin()

# --- VISTA 4: AJUSTES GLOBALES (IMAGEN 1) ---
# Card 1: Configuración del HMI
card_aj_hmi = ctk.CTkFrame(view_ajustes, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
card_aj_hmi.pack(fill="x", pady=(0, 12))

f_aj_hdr = ctk.CTkFrame(card_aj_hmi, fg_color="transparent")
f_aj_hdr.pack(fill="x", padx=16, pady=(12, 6))
ctk.CTkLabel(f_aj_hdr, text="⚙  CONFIGURACIÓN DEL HMI", font=(FONT_MAIN, 11, "bold"), text_color="#1E3A8A").pack(side="left")

f_aj_inputs_grid = ctk.CTkFrame(card_aj_hmi, fg_color="transparent")
f_aj_inputs_grid.pack(fill="x", padx=16, pady=(0, 12))
f_aj_inputs_grid.columnconfigure(0, weight=1)
f_aj_inputs_grid.columnconfigure(1, weight=1)

def campo_config_hmi(parent, row, col, label_txt, val_default):
    box = ctk.CTkFrame(parent, fg_color="transparent")
    box.grid(row=row, column=col, sticky="we", padx=8, pady=6)
    ctk.CTkLabel(box, text=label_txt, font=(FONT_MAIN, 10, "bold"), text_color=TEXT_PRIMARY).pack(anchor="w", pady=(0, 2))
    entry = ctk.CTkEntry(box, height=32, font=(FONT_MAIN, 11), fg_color="#FFFFFF", border_color=BORDER_CARD)
    entry.insert(0, val_default)
    entry.pack(fill="x")
    return entry

entry_aj_proj   = campo_config_hmi(f_aj_inputs_grid, 0, 0, "Nombre del Proyecto", "KOSTAL HMI Dashboard Prototipo")
entry_aj_est    = campo_config_hmi(f_aj_inputs_grid, 0, 1, "Código de la Estación", config_actual["ajustes_globales"].get("estacion_id", "EST-KOSTAL-04"))
entry_aj_desc   = campo_config_hmi(f_aj_inputs_grid, 1, 0, "Descripción / Línea", "Línea de Ensamblado & Calidad KOSTAL")
entry_aj_param  = campo_config_hmi(f_aj_inputs_grid, 1, 1, "Parámetro Calidad (Buena Pieza)", "0.5 mm")
entry_aj_inact  = campo_config_hmi(f_aj_inputs_grid, 2, 0, "Límite de Inactividad (segundos)", "15")
entry_aj_efec   = campo_config_hmi(f_aj_inputs_grid, 2, 1, "Efectividad Mínima (minutos por hora)", "15")

def guardar_ajustes_globales():
    config_actual["ajustes_globales"]["estacion_id"] = entry_aj_est.get()
    guardar_configuracion()
    messagebox.showinfo("Ajustes Guardados", "Los parámetros de configuración del HMI han sido guardados correctamente.")

f_aj_btn_row = ctk.CTkFrame(card_aj_hmi, fg_color="transparent")
f_aj_btn_row.pack(fill="x", padx=16, pady=(0, 14))

btn_guardar_ajustes = ctk.CTkButton(
    f_aj_btn_row,
    text="💾  Guardar Ajustes",
    font=(FONT_MAIN, 11, "bold"),
    fg_color=COLOR_KOSTAL_NAVY,
    hover_color=COLOR_KOSTAL_LIGHT,
    text_color="white",
    corner_radius=6,
    height=36,
    width=140,
    command=guardar_ajustes_globales
)
btn_guardar_ajustes.pack(side="right")

# Card 2: Histórico de Tiempos Muertos Justificados (Imagen 1)
card_aj_tm = ctk.CTkFrame(view_ajustes, fg_color="#FFFFFF", corner_radius=8, border_width=1, border_color=BORDER_CARD)
card_aj_tm.pack(fill="both", expand=True, pady=4)

f_tm_hdr = ctk.CTkFrame(card_aj_tm, fg_color="transparent")
f_tm_hdr.pack(fill="x", padx=16, pady=(12, 6))
ctk.CTkLabel(f_tm_hdr, text="⚠️  HISTÓRICO DE TIEMPOS MUERTOS JUSTIFICADOS", font=(FONT_MAIN, 11, "bold"), text_color="#DC2626").pack(side="left")

# Tabla de tiempos muertos
f_tbl_tm = ctk.CTkFrame(card_aj_tm, fg_color="#FFFFFF", corner_radius=6, border_width=1, border_color=BORDER_CARD)
f_tbl_tm.pack(fill="both", expand=True, padx=16, pady=(4, 16))

cols_tm = ["N°", "MOTIVO", "DURACIÓN", "COMENTARIOS", "FECHA Y HORA"]
f_tm_cols = ctk.CTkFrame(f_tbl_tm, fg_color="#F8FAFC", height=32, corner_radius=0)
f_tm_cols.pack(fill="x", padx=1, pady=1)

for c_nom in cols_tm:
    ctk.CTkLabel(f_tm_cols, text=c_nom, font=(FONT_MAIN, 9, "bold"), text_color="#475569").pack(side="left", expand=True)

f_tm_empty = ctk.CTkFrame(f_tbl_tm, fg_color="#FFFFFF", height=40)
f_tm_empty.pack(fill="x", padx=1, pady=8)
ctk.CTkLabel(f_tm_empty, text="No hay registros de tiempo muerto aún.", font=(FONT_MAIN, 10), text_color=TEXT_MUTED).pack(expand=True)

# Mostrar la primera vista por defecto
cambiar_modulo_admin("excel")

# ==============================================================================
# BARRA DE ESTADO INFERIOR (CON CONTROLES PERMANENTES DE VENTANA)
# ==============================================================================
status_bar = ctk.CTkFrame(ventana, fg_color=COLOR_KOSTAL_DARK, height=32, corner_radius=0)
status_bar.pack(fill="x", side="bottom")

lbl_estado = ctk.CTkLabel(
    status_bar,
    text="Sistema HMI inicializado correctamente. Esperando autenticación...",
    font=(FONT_MAIN, 9, "bold"),
    text_color="#94A3B8",
    anchor="w"
)
lbl_estado.pack(side="left", padx=16, fill="x", expand=True)

btn_sb_kb = ctk.CTkButton(
    status_bar,
    text="⌨ Teclado",
    font=(FONT_MAIN, 9, "bold"),
    fg_color="#1E293B",
    hover_color="#334155",
    text_color="#E2E8F0",
    corner_radius=4,
    width=80,
    height=24,
    command=alternar_teclado_tactil
)
btn_sb_kb.pack(side="right", padx=4, pady=4)

if MODO_SIM_WINDOWS:
    btn_sim_disparo = ctk.CTkButton(
        status_bar,
        text="⚡ Disparar Pintado (Sim)",
        font=(FONT_MAIN, 9, "bold"),
        fg_color="#059669",
        hover_color="#047857",
        text_color="white",
        corner_radius=4,
        height=24,
        command=forzar_disparo_simulado
    )
    btn_sim_disparo.pack(side="right", padx=3, pady=4)

    btn_sim_auto = ctk.CTkButton(
        status_bar,
        text="▶ Auto-Sim: OFF",
        font=(FONT_MAIN, 9, "bold"),
        fg_color="#0284C7",
        hover_color="#0369A1",
        text_color="white",
        corner_radius=4,
        height=24,
        command=alternar_auto_sim
    )
    btn_sim_auto.pack(side="right", padx=3, pady=4)

btn_sb_min = ctk.CTkButton(
    status_bar,
    text="🗕 Minimizar",
    font=(FONT_MAIN, 9, "bold"),
    fg_color="#1E293B",
    hover_color="#334155",
    text_color="#E2E8F0",
    corner_radius=4,
    width=90,
    height=24,
    command=minimizar_ventana
)
btn_sb_min.pack(side="right", padx=(4, 14), pady=4)

btn_sb_fs = ctk.CTkButton(
    status_bar,
    text="⛶ Pantalla",
    font=(FONT_MAIN, 9, "bold"),
    fg_color="#1E293B",
    hover_color="#334155",
    text_color="#E2E8F0",
    corner_radius=4,
    width=85,
    height=24,
    command=alternar_pantalla_completa
)
btn_sb_fs.pack(side="right", padx=4, pady=4)

ctk.CTkLabel(
    status_bar,
    text="KOSTAL Group Automotriz © 2026",
    font=(FONT_MAIN, 8),
    text_color="#64748B"
).pack(side="right", padx=(8, 14))

actualizar_reloj_y_fecha()
monitoreo_ciclico()
resetear_variables_inicio()
fijar_pasadas_tampo(config_actual.get("pasadas_tampo", 2))
actualizar_botones_paso_preset()
actualizar_boton_patron()

ventana.mainloop()