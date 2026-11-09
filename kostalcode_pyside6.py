# -*- coding: utf-8 -*-
"""
==============================================================================
HMI INDUSTRIAL KOSTAL — PYSIDE6 (Qt6) NATIVE ARCHITECTURE (60 FPS + QThread)
==============================================================================
Migración 100% fiel de la capa gráfica a PySide6 conservando intacta toda la
lógica interna:
- Comunicación OPC UA (Cliente Puerto 1 + Servidor Dual-Homing Puerto 2)
  alojada en un worker QThread dedicado con comunicación por Qt Signals/Slots.
- Persistencia exacta en config_hmi.json y bitácora debug_hmi.log.
- Pantalla Splash de carga con verificación real de conexión al PLC WAGO.
- Cálculo exacto de matriz dinámica (patrón vertical en serpiente y horizontal),
  Set 0 por receta, 1 o 2 pasadas de tampo, secuencia de seguridad post-PARO
  (PARO -> RESET -> INICIO), interlocks del PLC y cambio de idioma ES/EN en vivo.
- Overlays modales integrados dentro del mismo marco principal (sin ventanas
  secundarias toscas ni fondos grises en etiquetas).
==============================================================================
"""

import sys
import os
import time
import json
import math
import socket
import datetime
import subprocess
import threading

from PySide6.QtCore import (
    Qt, QThread, Signal, Slot, QTimer, QPropertyAnimation, QEasingCurve,
    QRectF, QPointF, QSize, QObject, QEvent
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QPixmap, QPainterPath, QIcon,
    QImage, QLinearGradient, QPalette
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QFrame, QStackedWidget,
    QScrollArea, QSizePolicy, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QAbstractItemView, QListView
)

try:
    from PIL import Image as PilImage
    PIL_DISPONIBLE = True
except ImportError:
    PIL_DISPONIBLE = False

try:
    from opcua import Client, Server, ua
    OPCUA_DISPONIBLE = True
except ImportError:
    OPCUA_DISPONIBLE = False

try:
    import openpyxl
    from openpyxl.styles import Font as XlFont, PatternFill, Alignment, Border, Side
    OPENPYXL_DISPONIBLE = True
except ImportError:
    OPENPYXL_DISPONIBLE = False


# ==============================================================================
# UTILIDAD PARA TECLADO TÁCTIL DE WINDOWS (TABTIP / OSK) E ICONOS VECTORIALES
# ==============================================================================
_ultimo_teclado_ts = 0.0
_estado_teclado_visible = False


def abrir_teclado_tactil_windows(forzar=False):
    """Abre automáticamente el teclado táctil o en pantalla de Windows sin bloquear la UI."""
    global _ultimo_teclado_ts, _estado_teclado_visible
    if os.name != "nt":
        return
    ahora = time.time()
    if not forzar and (ahora - _ultimo_teclado_ts < 0.45):
        return
    _ultimo_teclado_ts = ahora
    _estado_teclado_visible = True

    def _async_launch():
        try:
            rutas_tabtip = [
                r"C:\Program Files\Common Files\microsoft shared\ink\TabTip.exe",
                r"C:\Program Files (x86)\Common Files\microsoft shared\ink\TabTip.exe",
            ]
            for ruta in rutas_tabtip:
                if os.path.exists(ruta):
                    try:
                        os.startfile(ruta)
                        return
                    except Exception:
                        subprocess.Popen([ruta], shell=False)
                        return
            os.startfile("osk.exe")
        except Exception:
            try:
                subprocess.Popen(
                    ["osk.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
            except Exception:
                pass

    threading.Thread(target=_async_launch, daemon=True).start()


def ocultar_teclado_tactil_windows(forzar=False):
    """Solicita el cierre del teclado en pantalla cuando se confirma una entrada."""
    global _estado_teclado_visible
    if os.name != "nt":
        return
    if not _estado_teclado_visible and not forzar:
        return
    _estado_teclado_visible = False
    if forzar:
        def _async_hide():
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "osk.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                    timeout=0.5,
                )
            except Exception:
                pass
        threading.Thread(target=_async_hide, daemon=True).start()


class _AutoTouchKeyboardFilter(QObject):
    """Filtro global que despliega automáticamente el teclado al enfocar o tocar cualquier QLineEdit editable."""
    def eventFilter(self, obj, event):
        if isinstance(obj, QLineEdit):
            etype = event.type()
            if etype in (QEvent.FocusIn, QEvent.MouseButtonPress, QEvent.TouchBegin):
                if obj.isEnabled() and not obj.isReadOnly() and not getattr(obj, "_no_auto_keyboard", False):
                    abrir_teclado_tactil_windows()
        return super().eventFilter(obj, event)


def crear_icono_ojo(mostrar_texto: bool = False) -> QIcon:
    """Genera un icono vectorial limpio (ojo / ojo tachado) mediante QPainter sin usar emojis."""
    sz = 48
    pix = QPixmap(sz, sz)
    pix.fill(Qt.transparent)

    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)

    color = QColor("#0284C7") if mostrar_texto else QColor("#475569")
    pen = QPen(color, 3.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # Contorno almendrado del ojo
    eye_path = QPainterPath()
    eye_path.moveTo(5.0, 24.0)
    eye_path.quadTo(24.0, 8.0, 43.0, 24.0)
    eye_path.quadTo(24.0, 40.0, 5.0, 24.0)
    p.drawPath(eye_path)

    # Iris y pupila central
    p.drawEllipse(QPointF(24.0, 24.0), 7.2, 7.2)
    p.setBrush(QBrush(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(24.0, 24.0), 3.0, 3.0)

    # Línea diagonal de tachado cuando la contraseña está visible (modo ocultar)
    if mostrar_texto:
        halo_pen = QPen(QColor("#FFFFFF"), 6.8, Qt.SolidLine, Qt.RoundCap)
        p.setPen(halo_pen)
        p.drawLine(QPointF(10.0, 38.0), QPointF(38.0, 10.0))

        slash_pen = QPen(color, 3.8, Qt.SolidLine, Qt.RoundCap)
        p.setPen(slash_pen)
        p.drawLine(QPointF(10.0, 38.0), QPointF(38.0, 10.0))

    p.end()
    return QIcon(pix)


# ==============================================================================
# PALETA OFICIAL DE COLORES KOSTAL (LIGHT INDUSTRIAL THEME)
# ==============================================================================
COLOR_BG_MAIN = "#EEF2F7"
COLOR_BG_CARD = "#FFFFFF"
COLOR_BG_SURFACE = "#F8FAFC"
COLOR_BORDER = "#CBD5E1"
COLOR_HEADER = "#0A192F"

COLOR_BRAND_PRIMARY = "#003A70"
COLOR_BRAND_HOVER = "#002850"
COLOR_ACCENT_CYAN = "#0284C7"

COLOR_OK = "#059669"
COLOR_OK_HOVER = "#047857"
COLOR_WARN = "#D97706"
COLOR_WARN_HOVER = "#B45309"
COLOR_ERR = "#DC2626"
COLOR_ERR_HOVER = "#B91C1C"

COLOR_TEXT_PRI = "#0F172A"
COLOR_TEXT_SEC = "#334155"
COLOR_TEXT_MUTED = "#475569"

COLOR_LED_OFF = "#E2E8F0"
COLOR_BTN_DISABLED = "#E2E8F0"
COLOR_TEXT_DISABLED = "#64748B"

# ==============================================================================
# LÍMITES FÍSICOS DE EJES (X: 0..88 mm, Y: 0..200 mm)
# ==============================================================================
COORD_MIN = 0.0
COORD_X_MAX = 88.0
COORD_Y_MAX = 200.0
COORD_MAX = COORD_Y_MAX


def limitar_coordenada(valor, max_val=COORD_Y_MAX):
    try:
        v = float(valor)
    except (ValueError, TypeError):
        v = 0.0
    return round(max(COORD_MIN, min(max_val, v)), 2)


def limitar_coord_x(valor):
    return limitar_coordenada(valor, COORD_X_MAX)


def limitar_coord_y(valor):
    return limitar_coordenada(valor, COORD_Y_MAX)


# ==============================================================================
# ARCHIVO DE CONFIGURACIÓN PERSISTENTE Y BITÁCORA DEBUG
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
CONFIG_FILE = os.path.join(BASE_DIR, "config_hmi.json")
DEBUG_LOG_FILE = os.path.join(BASE_DIR, "debug_hmi.log")


def log_debug_hmi(mensaje):
    """Registra eventos operativos y de comunicación en debug_hmi.log y consola."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    linea = f"[{ts}] {mensaje}"
    print(linea)
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except Exception:
        pass


CONFIG_DEFAULT = {
    "opc_ip": "192.168.1.69",
    "opc_port": "4840",
    "opc_server_bind_ip": "0.0.0.0",
    "opc_server_port": "4840",
    "plc_namespace": 4,
    "node_prefix": "|var|WAGO 750-8212 PFC200 G2 2ETH RS XTR.Application.GVL.",
    "estacion_nombre": "EST-KOSTAL-04",
    "home_x": 0.0,
    "home_y": 0.0,
    "cero_x": 0.0,
    "cero_y": 0.0,
    "set_0_confirmado": False,
    "vel_default": 100.0,
    "paso_jog_default": 5.0,
    "pasadas_tampo": 2,
    "patron_pintado": "vertical",
    "idioma": "ES",
    "receta_activa": "#1 - Palanca Limpiaparabrisas",
    "produccion_total": 0,
    "produccion_ok": 0,
    "produccion_nok": 0,
    "historial_horas": [],
    "recetas": [
        {
            "nombre": "#1 - Palanca Limpiaparabrisas",
            "pieza": "Palanca Limpiaparabrisas",
            "modelo": "M2-2026",
            "filas": 3,
            "columnas": 5,
            "piezas_por_jig": 15,
            "espacio_x": 20.0,
            "espacio_y": 35.0,
            "cero_x": 0.0,
            "cero_y": 0.0,
            "set_0_listo": True,
            "vel_x": 100.0,
            "vel_y": 100.0,
            "imagen": "pieza_default.png",
        },
        {
            "nombre": "#2 - Conmutador Luces",
            "pieza": "Conmutador Luces",
            "modelo": "K4-LUX",
            "filas": 2,
            "columnas": 4,
            "piezas_por_jig": 8,
            "espacio_x": 24.0,
            "espacio_y": 40.0,
            "cero_x": 0.0,
            "cero_y": 0.0,
            "set_0_listo": True,
            "vel_x": 85.0,
            "vel_y": 85.0,
            "imagen": "pieza_default.png",
        },
        {
            "nombre": "#3 - Botón Consola Central",
            "pieza": "Botón Consola",
            "modelo": "BC-900",
            "filas": 4,
            "columnas": 5,
            "piezas_por_jig": 20,
            "espacio_x": 18.0,
            "espacio_y": 30.0,
            "cero_x": 0.0,
            "cero_y": 0.0,
            "set_0_listo": True,
            "vel_x": 120.0,
            "vel_y": 120.0,
            "imagen": "pieza_default.png",
        },
        {
            "nombre": "#4 - Módulo Volante Sport",
            "pieza": "Módulo Volante",
            "modelo": "MV-S2",
            "filas": 2,
            "columnas": 2,
            "piezas_por_jig": 4,
            "espacio_x": 45.0,
            "espacio_y": 50.0,
            "cero_x": 0.0,
            "cero_y": 0.0,
            "set_0_listo": True,
            "vel_x": 90.0,
            "vel_y": 90.0,
            "imagen": "pieza_default.png",
        },
    ],
    "usuarios": [
        {"user": "operador", "pass": "1234", "rol": "OPERADOR", "nombre": "Juan Pérez"},
        {"user": "admin", "pass": "admin", "rol": "ADMINISTRADOR", "nombre": "Ing. Carlos Ruiz"},
        {"user": "mantenimiento", "pass": "mant2026", "rol": "ADMINISTRADOR", "nombre": "Téc. Roberto Gómez"},
    ],
}


def obtener_piezas_por_jig_receta(rec):
    if not isinstance(rec, dict):
        return 1
    try:
        f = max(1, min(5, int(rec.get("filas", 1))))
    except (ValueError, TypeError):
        f = 1
    try:
        c = max(1, min(5, int(rec.get("columnas", 1))))
    except (ValueError, TypeError):
        c = 1
    max_cap = f * c
    raw_pz = rec.get("piezas_por_jig", None)
    if raw_pz is None or str(raw_pz).strip() == "":
        return max_cap
    try:
        pz = int(float(raw_pz))
    except (ValueError, TypeError):
        pz = max_cap
    return max(1, min(max_cap, pz))


def buscar_recurso(nombre_archivo):
    if not nombre_archivo:
        return None
    if os.path.exists(nombre_archivo):
        return nombre_archivo
    archivo_base = os.path.basename(nombre_archivo)
    posibles_rutas = [
        os.path.join(BASE_DIR, nombre_archivo),
        os.path.join(BASE_DIR, "Assets", archivo_base),
        os.path.join(BASE_DIR, archivo_base),
        os.path.join(r"C:\Users\eliza\Desktop\Interfaz_KOSTAL\Assets", archivo_base),
    ]
    for r in posibles_rutas:
        if os.path.exists(r):
            return r
    return None


def cargar_logo_corporativo(nombre_archivo, max_ancho, max_alto):
    """Carga un logotipo corporativo (WebP/PNG/JPG) escalado con antialiasing y fallback PIL."""
    ruta = buscar_recurso(nombre_archivo)
    if not ruta:
        return None
    try:
        pix = QPixmap(ruta)
        if pix.isNull() and PIL_DISPONIBLE:
            img = PilImage.open(ruta).convert("RGBA")
            w, h = img.size
            raw = img.tobytes("raw", "RGBA")
            qimg = QImage(raw, w, h, w * 4, QImage.Format_RGBA8888).copy()
            pix = QPixmap.fromImage(qimg)
        if not pix.isNull():
            return pix.scaled(max_ancho, max_alto, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    except Exception as e:
        print(f"Aviso cargando logo {nombre_archivo}: {e}")
    return None


def cargar_configuracion_disco():
    cfg = json.loads(json.dumps(CONFIG_DEFAULT))
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
                if isinstance(datos, dict):
                    cfg.update(datos)
        except Exception as e:
            print(f"Error leyendo {CONFIG_FILE}: {e}")
    # Normalizar recetas (soportando ambos formatos de llaves en config_hmi.json)
    for idx, rec in enumerate(cfg.get("recetas", []), start=1):
        rec["filas"] = max(1, min(5, int(rec.get("filas", 3))))
        cols_val = rec.get("cols", rec.get("columnas", 5))
        rec["cols"] = max(1, min(5, int(cols_val)))
        rec["columnas"] = rec["cols"]
        rec["piezas_por_jig"] = obtener_piezas_por_jig_receta(rec)
        rec["piezas"] = rec["piezas_por_jig"]
        dx_val = float(rec.get("delta_x", rec.get("espacio_x", 20.0)))
        dy_val = float(rec.get("delta_y", rec.get("espacio_y", 35.0)))
        rec["delta_x"] = dx_val
        rec["espacio_x"] = dx_val
        rec["delta_y"] = dy_val
        rec["espacio_y"] = dy_val
        vel_val = float(rec.get("velocidad", rec.get("vel_x", 100.0)))
        rec["velocidad"] = vel_val
        rec["vel_x"] = vel_val
        rec["vel_y"] = vel_val
        cx_val = limitar_coord_x(rec.get("cero_guardado_x", rec.get("cero_x", 0.0)))
        cy_val = limitar_coord_y(rec.get("cero_guardado_y", rec.get("cero_y", 0.0)))
        rec["cero_guardado_x"] = cx_val
        rec["cero_x"] = cx_val
        rec["cero_guardado_y"] = cy_val
        rec["cero_y"] = cy_val
        cal_val = bool(rec.get("cero_calibrado", rec.get("set_0_listo", True)))
        rec["cero_calibrado"] = cal_val
        rec["set_0_listo"] = cal_val
    return cfg


def guardar_configuracion_disco(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error guardando {CONFIG_FILE}: {e}")
        return False


# ==============================================================================
# DICCIONARIO DE IDIOMAS (ES / EN) COMPLETO
# ==============================================================================
DICCIONARIO_IDIOMAS = {
    "ES": {
        "panel_admin": "Panel Admin",
        "cerrar_sesion": "Cerrar Sesión",
        "pantalla": "Pantalla Completa",
        "salir_pantalla": "Modo Ventana",
        "minimizar": "Minimizar",
        "info_proceso": "INFORMACIÓN DE PROCESO",
        "receta_activa": "Receta / Jig Activo:",
        "ver_jigs": "Ver Jigs",
        "pieza": "Pieza:",
        "modelo": "Modelo:",
        "matriz_fixture": "Matriz Fixture:",
        "dist_cc": "Dist. C-C:",
        "piezas_realizadas": "Piezas Realizadas",
        "cero_listo": "CERO CALIBRADO",
        "cero_pendiente": "SET 0 PENDIENTE (CALIBRAR PUNTO 1)",
        "tiempo_sesion": "Tiempo Sesión:",
        "total": "TOTAL",
        "ok": "OK",
        "reg_fin_hora": "Registrar Fin de Hora",
        "ctrl_pintado": "CONTROL PRINCIPAL DE PINTADO",
        "btn_pintar_off": "INICIO DE PINTADO [OFF]",
        "btn_pintar_on": "PINTANDO ACTIVO [ON]",
        "btn_pintar_bloq": "PINTADO BLOQUEADO (SET 0 PENDIENTE)",
        "estado_fixture": "ESTADO DEL FIXTURE",
        "recetario_admin": "Recetario Admin",
        "patron_vert": "Patrón: Vertical",
        "patron_horiz": "Patrón: Horizontal",
        "coords_btn": "Coords",
        "probar_mov": "Probar Movimiento",
        "ley_pend": "● Pendiente",
        "ley_proc": "● En Proceso (Activa)",
        "ley_ok": "● Completada OK",
        "ley_nok": "● Fallo / NOK",
        "ley_click": "Modo Manual: Clic en cavidad envía coordenada",
        "diag_plc": "DIAGNÓSTICO Y SEÑALES DEL PLC WAGO",
        "op_jog": "OPERACIÓN Y JOG",
        "plegar": "▲ Plegar",
        "desplegar": "▼ Desplegar",
        "modo_auto": "MODO: AUTO",
        "modo_manual": "MODO: MANUAL",
        "velocidad": "Velocidad:",
        "ciclos_tampo": "Ciclos Tampo:",
        "pasada_1": "1 Pasada",
        "pasada_2": "2 Pasadas",
        "jog_manual": "JOG MANUAL",
        "paso_jog": "Paso JOG:",
        "home": "Ir a Home",
        "activar_tampo": "Activar Tampo",
        "calibracion_show": "▼ Desplegar Calibración (Set 0 / Set Home)",
        "calibracion_hide": "▲ Ocultar Calibración (Set 0 / Set Home)",
        "set_0": "Set 0",
        "set_home": "Set Home",
        "cero_reg": "CERO REGISTRADO:",
        "mandos_ctrl": "MANDOS DE CONTROL",
        "inicio": "INICIO",
        "reset": "RESET",
        "paro": "PARO",
        "paro_activo": "PARO ACTIVO (LIBERAR)",
        "sistema_listo": "SISTEMA LISTO PARA OPERAR",
        "en_proceso": "CICLO DE PINTADO EN PROCESO",
        "modo_man_act": "MODO MANUAL / JOG ACTIVO",
        "paro_emergencia": "PARO ACTIVADO — PRESIONE RESET Y LUEGO INICIO",
        "req_reset": "PARO LIBERADO — PRESIONE [RESET] PARA REARMAR",
        "req_inicio": "RESET COMPLETADO — PRESIONE [INICIO] PARA HABILITAR",
    },
    "EN": {
        "panel_admin": "Admin Panel",
        "cerrar_sesion": "Sign Out",
        "pantalla": "Fullscreen",
        "salir_pantalla": "Windowed",
        "minimizar": "Minimize",
        "info_proceso": "PROCESS INFORMATION",
        "receta_activa": "Active Recipe / Jig:",
        "ver_jigs": "View Jigs",
        "pieza": "Part:",
        "modelo": "Model:",
        "matriz_fixture": "Fixture Matrix:",
        "dist_cc": "C-C Dist.:",
        "piezas_realizadas": "Completed Parts",
        "cero_listo": "ZERO CALIBRATED",
        "cero_pendiente": "SET 0 PENDING (CALIBRATE POINT 1)",
        "tiempo_sesion": "Session Time:",
        "total": "TOTAL",
        "ok": "OK",
        "reg_fin_hora": "Log Hourly Output",
        "ctrl_pintado": "MAIN PRINTING CONTROL",
        "btn_pintar_off": "START PRINTING [OFF]",
        "btn_pintar_on": "PRINTING ACTIVE [ON]",
        "btn_pintar_bloq": "PRINTING LOCKED (SET 0 PENDING)",
        "estado_fixture": "FIXTURE STATUS",
        "recetario_admin": "Recipe Admin",
        "patron_vert": "Pattern: Vertical",
        "patron_horiz": "Pattern: Horizontal",
        "coords_btn": "Coords",
        "probar_mov": "Test Movement",
        "ley_pend": "● Pending",
        "ley_proc": "● In Progress (Active)",
        "ley_ok": "● Completed OK",
        "ley_nok": "● Fault / NOK",
        "ley_click": "Manual Mode: Click cavity to send coordinate",
        "diag_plc": "WAGO PLC DIAGNOSTICS & SIGNALS",
        "op_jog": "OPERATION & JOG",
        "plegar": "▲ Collapse",
        "desplegar": "▼ Expand",
        "modo_auto": "MODE: AUTO",
        "modo_manual": "MODE: MANUAL",
        "velocidad": "Speed:",
        "ciclos_tampo": "Pad Cycles:",
        "pasada_1": "1 Pass",
        "pasada_2": "2 Passes",
        "jog_manual": "MANUAL JOG",
        "paso_jog": "JOG Step:",
        "home": "Go Home",
        "activar_tampo": "Trigger Pad",
        "calibracion_show": "▼ Show Calibration (Set 0 / Set Home)",
        "calibracion_hide": "▲ Hide Calibration (Set 0 / Set Home)",
        "set_0": "Set 0",
        "set_home": "Set Home",
        "cero_reg": "SAVED ZERO:",
        "mandos_ctrl": "CONTROL COMMANDS",
        "inicio": "START",
        "reset": "RESET",
        "paro": "STOP",
        "paro_activo": "STOP ACTIVE (RELEASE)",
        "sistema_listo": "SYSTEM READY TO OPERATE",
        "en_proceso": "PRINTING CYCLE IN PROGRESS",
        "modo_man_act": "MANUAL / JOG MODE ACTIVE",
        "paro_emergencia": "STOP ACTIVE — PRESS RESET THEN START",
        "req_reset": "STOP RELEASED — PRESS [RESET] TO RE-ARM",
        "req_inicio": "RESET DONE — PRESS [START] TO ENABLE",
    },
}


# ==============================================================================
# WORKER OPC UA EN HILO SECUNDARIO (QThread + Qt Signals)
# ==============================================================================
class OpcUaWorker(QThread):
    """
    Ejecuta toda la comunicación OPC UA (Cliente hacia PLC WAGO en Puerto 1 y
    Servidor OPC UA local en Puerto 2) en un hilo separado para mantener la
    interfaz PySide6 a 60 FPS constantes sin bloqueos por red.
    """
    telemetry_updated = Signal(dict)
    connection_Changed = Signal(bool, str)
    cycle_step_completed = Signal()
    splash_progress = Signal(int, str, bool, bool)  # pct, msg, finished, connected

    NODE_MAP = {
        "INICIO": "INICIO",
        "POSICION_INICIAL": "POSICION_INICIAL",
        "RESET": "RESET",
        "STOP": "STOP",
        "MANUAL": "MANUAL",
        "AUTO": "AUTO",
        "CRUZETA": "CRUZETA",
        "NUEVA_POSICION": "NUEVA_POSICION",
        "POS_X": "POS_X",
        "POS_Y": "POS_Y",
        "HOME_X": "HOME_X",
        "HOME_Y": "HOME_Y",
        "VELOCIDAD_X": "VELOCIDAD_X",
        "VELOCIDAD_Y": "VELOCIDAD_Y",
        "PINTAR": "PINTAR",
        "S_TAMPO": "S_TAMPO",
        "PINTADO": "PINTADO",
        "M_ENE": "M_ENE",
        "M_SET": "M_SET",
        "LISTO": "LISTO",
        "NO_LISTO": "NO_LISTO",
        "ALARM_X": "ALARM_X",
        "ALARM_Y": "ALARM_Y",
        "PE": "PE",
        "SENSOR_X": "SENSOR_X",
        "SENSOR_Y": "SENSOR_Y",
        "TAMPO": "TAMPO",
    }

    def __init__(self, config_ref, parent=None):
        super().__init__(parent)
        self.config_ref = config_ref
        self._running = True
        self._Simulation_mode = False
        self._do_initial_check = True
        self._retry_requested = False

        self.client = None
        self.connected = False
        self.nodes = {}
        self.write_queue = []

        self.server = None
        self.server_running = False
        self.server_nodes = {}

        # Estado interno de simulación para respuesta realista cuando no hay PLC
        self.sim_state = {
            "PINTADO": False,
            "M_ENE": True,
            "M_SET": True,
            "LISTO": True,
            "NO_LISTO": False,
            "ALARM_X": False,
            "ALARM_Y": False,
            "PE": False,
            "SENSOR_X": False,
            "SENSOR_Y": False,
            "TAMPO": False,
            "POS_X": 0.0,
            "POS_Y": 0.0,
            "PINTAR": False,
            "STOP": False,
            "MANUAL": False,
            "AUTO": True,
        }
        self._last_pintado = False

    def request_retry_connection(self):
        self._Simulation_mode = False
        self._retry_requested = True

    def activate_Simulation_mode(self):
        self._Simulation_mode = True
        self.connected = False
        self.connection_Changed.emit(False, "SIMULACIÓN")

    def enqueue_write(self, var_name, value):
        self.write_queue.append((var_name, value))

    def enqueue_pulse(self, var_name, duration_ms=350):
        self.write_queue.append(("__PULSE__", (var_name, duration_ms)))

    def enqueue_move_xy(self, x_mm, y_mm, vel_mm_s=None, trigger_var="NUEVA_POSICION", pulse_ms=500):
        if vel_mm_s is not None:
            self.write_queue.append(("VELOCIDAD_X", float(vel_mm_s)))
            self.write_queue.append(("VELOCIDAD_Y", float(vel_mm_s)))
        self.write_queue.append(("POS_X", float(x_mm)))
        self.write_queue.append(("POS_Y", float(y_mm)))
        if trigger_var:
            self.write_queue.append(("__PULSE__", (trigger_var, pulse_ms)))

    def _start_opc_server(self):
        if not OPCUA_DISPONIBLE or self.server_running:
            return
        try:
            bind_ip = self.config_ref.get("opc_server_bind_ip", "0.0.0.0")
            port = int(self.config_ref.get("opc_server_port", 4840))
            self.server = Server()
            self.server.set_endpoint(f"opc.tcp://{bind_ip}:{port}/kostal/hmi/")
            self.server.set_server_name("KOSTAL_HMI_OPCUA_Server")
            idx = self.server.register_namespace("http://kostal.com/hmi")
            obj = self.server.get_objects_node().add_object(idx, "HMI_KOSTAL")
            for k in self.NODE_MAP:
                init_val = 0 if k in ("POS_X", "POS_Y", "HOME_X", "HOME_Y", "VELOCIDAD_X", "VELOCIDAD_Y") else False
                vnode = obj.add_variable(idx, k, init_val)
                vnode.set_writable()
                self.server_nodes[k] = vnode
            for counter_key in ("Piezas_Totales", "Piezas_OK", "Piezas_NOK"):
                cnode = obj.add_variable(idx, counter_key, 0)
                cnode.set_writable()
                self.server_nodes[counter_key] = cnode
            self.server.start()
            self.server_running = True
            log_debug_hmi(f"Servidor OPC UA Puerto 2 iniciado en {bind_ip}:{port}")
        except Exception as e:
            log_debug_hmi(f"Aviso Servidor OPC UA local: {e}")

    def update_production_counters(self, total_pz, ok_pz, nok_pz):
        if not self.server_running:
            return
        for key, val in (
            ("Piezas_Totales", int(total_pz)),
            ("Piezas_OK", int(ok_pz)),
            ("Piezas_NOK", int(nok_pz)),
        ):
            if key in self.server_nodes:
                try:
                    self.server_nodes[key].set_value(int(val))
                except Exception:
                    pass

    def _ping_socket(self, ip, port, timeout=1.2):
        try:
            with socket.create_connection((ip, int(port)), timeout=timeout):
                return True
        except Exception:
            return False

    def _connect_plc(self):
        if not OPCUA_DISPONIBLE:
            return False
        ip = self.config_ref.get("opc_ip", "192.168.1.69").strip()
        port = self.config_ref.get("opc_port", "4840").strip()
        ns = int(self.config_ref.get("plc_namespace", 4))
        prefix = self.config_ref.get(
            "node_prefix",
            "|var|WAGO 750-8212 PFC200 G2 2ETH RS XTR.Application.GVL.",
        )
        if not self._ping_socket(ip, port, timeout=1.2):
            return False
        try:
            if self.client:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
            url = f"opc.tcp://{ip}:{port}"
            self.client = Client(url, timeout=2.5)
            self.client.connect()
            self.nodes.clear()
            for key, suffix in self.NODE_MAP.items():
                node_id = f"ns={ns};s={prefix}{suffix}"
                self.nodes[key] = self.client.get_node(node_id)
            self.connected = True
            log_debug_hmi(f"Conectado exitosamente al PLC WAGO en {url}")
            return True
        except Exception as e:
            log_debug_hmi(f"Error conectando a PLC OPC UA: {e}")
            self.connected = False
            return False

    def _write_single_plc_var(self, var_name, value):
        # Actualizar espejo en simulación y servidor local
        if var_name in ("POS_X", "POS_Y", "HOME_X", "HOME_Y"):
            self.sim_state[var_name] = float(value)
        elif var_name in ("VELOCIDAD_X", "VELOCIDAD_Y"):
            self.sim_state[var_name] = float(value)
        else:
            self.sim_state[var_name] = bool(value)

        if self.server_running and var_name in self.server_nodes:
            try:
                self.server_nodes[var_name].set_value(value)
            except Exception:
                pass

        if not self.connected or not self.client or var_name not in self.nodes:
            return

        try:
            node = self.nodes[var_name]
            if var_name in ("POS_X", "POS_Y", "HOME_X", "HOME_Y"):
                # El PLC WAGO recibe coordenadas en centésimas de milímetro (Int32)
                val_cent = int(round(float(value) * 100.0))
                dv = ua.DataValue(ua.Variant(val_cent, ua.VariantType.Int32))
            elif var_name in ("VELOCIDAD_X", "VELOCIDAD_Y"):
                val_vel = max(1, min(65535, int(round(float(value)))))
                dv = ua.DataValue(ua.Variant(val_vel, ua.VariantType.UInt16))
            else:
                dv = ua.DataValue(ua.Variant(bool(value), ua.VariantType.Boolean))
            node.set_value(dv)
        except Exception as e:
            log_debug_hmi(f"Error escribiendo {var_name}={value} en PLC: {e}")
            self.connected = False
            self.connection_Changed.emit(False, "DESCONECTADO")

    def run(self):
        # Paso inicial de Splash Screen
        self.splash_progress.emit(15, "Cargando configuración persistente y recetas...", False, False)
        self.msleep(180)
        self.splash_progress.emit(40, "Iniciando Servidor OPC UA Local (Puerto 2)...", False, False)
        self._start_opc_server()
        self.msleep(180)

        ip = self.config_ref.get("opc_ip", "192.168.1.69")
        port = self.config_ref.get("opc_port", "4840")
        self.splash_progress.emit(70, f"Verificando enlace con PLC WAGO ({ip}:{port})...", False, False)
        ok = self._connect_plc()
        if ok:
            self.connection_Changed.emit(True, "CONECTADO")
            self.splash_progress.emit(100, "Enlace OPC UA establecido con éxito.", True, True)
        else:
            self.connection_Changed.emit(False, "SIN CONEXIÓN")
            self.splash_progress.emit(100, f"No se detectó respuesta del PLC en {ip}:{port}.", True, False)

        active_pulses = []  # lista de (var_name, expire_timestamp)
        last_reconnect_attempt = time.time()

        while self._running:
            if self._retry_requested:
                self._retry_requested = False
                self.splash_progress.emit(50, f"Reintentando conexión a {ip}:{port}...", False, False)
                ok = self._connect_plc()
                if ok:
                    self.connection_Changed.emit(True, "CONECTADO")
                    self.splash_progress.emit(100, "Enlace OPC UA establecido con éxito.", True, True)
                else:
                    self.connection_Changed.emit(False, "SIN CONEXIÓN")
                    self.splash_progress.emit(100, f"PLC no disponible en {ip}:{port}.", True, False)

            # Procesar cola de escrituras
            while self.write_queue:
                item = self.write_queue.pop(0)
                if item[0] == "__PULSE__":
                    vname, dur_ms = item[1]
                    self._write_single_plc_var(vname, True)
                    active_pulses.append((vname, time.time() + (dur_ms / 1000.0)))
                else:
                    self._write_single_plc_var(item[0], item[1])

            # Apagar pulsos vencidos
            now = time.time()
            remaining_pulses = []
            for vname, exp in active_pulses:
                if now >= exp:
                    self._write_single_plc_var(vname, False)
                else:
                    remaining_pulses.append((vname, exp))
            active_pulses = remaining_pulses

            # Lectura cíclica
            if self.connected and self.client:
                try:
                    read_keys = [
                        "PINTADO", "M_ENE", "M_SET", "LISTO", "NO_LISTO",
                        "ALARM_X", "ALARM_Y", "PE", "SENSOR_X", "SENSOR_Y",
                        "TAMPO", "POS_X", "POS_Y",
                    ]
                    node_list = [self.nodes[k] for k in read_keys if k in self.nodes]
                    vals = self.client.get_values(node_list)
                    snap = {}
                    for k, v in zip(read_keys, vals):
                        if k in ("POS_X", "POS_Y"):
                            snap[k] = round(float(v or 0.0) / 100.0, 2)
                        else:
                            snap[k] = bool(v)
                    # Detectar flanco ascendente de PINTADO
                    if snap.get("PINTADO", False) and not self._last_pintado:
                        self.cycle_step_completed.emit()
                    self._last_pintado = snap.get("PINTADO", False)
                    self.telemetry_updated.emit(snap)
                except Exception as e:
                    log_debug_hmi(f"Pérdida de enlace OPC UA durante lectura: {e}")
                    self.connected = False
                    self.connection_Changed.emit(False, "DESCONECTADO")
            else:
                # Modo simulación o reconexión periódica
                if not self._Simulation_mode and (now - last_reconnect_attempt) > 6.0:
                    last_reconnect_attempt = now
                    if self._connect_plc():
                        self.connection_Changed.emit(True, "CONECTADO")
                snap = dict(self.sim_state)
                self.telemetry_updated.emit(snap)

            self.msleep(80)

        # Cierre limpio
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
        if self.server and self.server_running:
            try:
                self.server.stop()
            except Exception:
                pass

    def stop_worker(self):
        self._running = False
        self.wait(1500)


# ==============================================================================
# WIDGET COLAPSABLE CON ANIMACIÓN NATIVA DE 60 FPS (QPropertyAnimation)
# ==============================================================================
class AnimatedCollapsibleBox(QWidget):
    """Contenedor que pliega/despliega su altura suavemente a 60 FPS sin saltos."""

    def __init__(self, parent=None, expanded=True):
        super().__init__(parent)
        self._expanded = expanded
        self._content_layout = QVBoxLayout(self)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)

        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)

        if not expanded:
            self.setMaximumHeight(0)
            self.setVisible(False)

    @property
    def content_layout(self):
        return self._content_layout

    @property
    def is_expanded(self):
        return self._expanded

    def toggle(self, expand=None):
        if expand is None:
            expand = not self._expanded
        if expand == self._expanded:
            return
        self._expanded = expand
        self._anim.stop()
        target_h = max(self.sizeHint().height(), 10)
        if self._expanded:
            self.setVisible(True)
            self._anim.setStartValue(0)
            self._anim.setEndValue(target_h)
        else:
            self._anim.setStartValue(self.height())
            self._anim.setEndValue(0)
        self._anim.start()

    def _on_anim_finished(self):
        if self._expanded:
            self.setMaximumHeight(16777215)
        else:
            self.setVisible(False)


# ==============================================================================
# LIENZO VECTORIAL DE 60 FPS PARA MATRIZ DE FIXTURE (QPainter + Antialiasing)
# ==============================================================================
class FixtureCanvasWidget(QWidget):
    """
    Dibuja la matriz dinámica del Fixture (hasta 5x5) con antialiasing por
    hardware, miniaturas circulares de pieza cacheadas en QPixmap, píldoras de
    coordenadas, resaltado al pasar el cursor y clic directo en Modo Manual.
    """
    cavity_clicked = Signal(int)  # índice de pieza en orden de pintado (0..N-1)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 230)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.filas = 3
        self.cols = 5
        self.piezas_por_jig = 15
        self.patron = "vertical"
        self.mostrar_coords = True
        self.coordenadas = []       # [(x, y), ...] según orden de recorrido
        self.estados = []           # ["pendiente" | "activa" | "ok" | "nok"]
        self.indice_activo = -1
        self.pasada_actual = 1
        self.total_pasadas = 1
        self.imagen_pieza_path = ""
        self._pixmap_cache = {}
        self._hover_idx = -1
        self._card_rects = {}       # seq_idx -> QRectF
        self._pulse_phase = 0.0

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_timer.start(45)

    def _tick_pulse(self):
        if self.indice_activo >= 0:
            self._pulse_phase = (self._pulse_phase + 0.22) % (2.0 * math.pi)
            self.update()

    def configure_matrix(
        self, filas, cols, piezas_por_jig, patron, coordenadas, estados,
        indice_activo, mostrar_coords=True, imagen_pieza="", pasada_actual=1, total_pasadas=1
    ):
        self.filas = max(1, min(5, int(filas)))
        self.cols = max(1, min(5, int(cols)))
        self.piezas_por_jig = max(1, int(piezas_por_jig))
        self.patron = patron
        self.coordenadas = list(coordenadas)
        self.estados = list(estados)
        self.indice_activo = indice_activo
        self.mostrar_coords = bool(mostrar_coords)
        if imagen_pieza != self.imagen_pieza_path:
            self.imagen_pieza_path = imagen_pieza
            self._pixmap_cache.clear()
        self.pasada_actual = pasada_actual
        self.total_pasadas = total_pasadas
        self.update()

    def _get_circular_pixmap(self, diameter):
        if diameter <= 8:
            return None
        key = (self.imagen_pieza_path, diameter)
        if key in self._pixmap_cache:
            return self._pixmap_cache[key]
        real_path = buscar_recurso(self.imagen_pieza_path)
        if not real_path or not os.path.exists(real_path):
            return None
        src = QPixmap(real_path)
        if src.isNull():
            return None
        scaled = src.scaled(diameter, diameter, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        out = QPixmap(diameter, diameter)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        path = QPainterPath()
        path.addEllipse(1, 1, diameter - 2, diameter - 2)
        p.setClipPath(path)
        x_off = (diameter - scaled.width()) // 2
        y_off = (diameter - scaled.height()) // 2
        p.drawPixmap(x_off, y_off, scaled)
        p.end()
        self._pixmap_cache[key] = out
        return out

    def _grid_to_seq_index(self, r, c):
        """Convierte (fila r, col c) al índice de secuencia según el patrón vertical/horizontal."""
        if self.patron == "vertical":
            if c % 2 == 0:
                return c * self.filas + r
            else:
                return c * self.filas + ((self.filas - 1) - r)
        else:
            return r * self.cols + c

    def mouseMoveEvent(self, event):
        pos = event.position()
        found = -1
        for seq_idx, rect in self._card_rects.items():
            if rect.contains(pos) and seq_idx < self.piezas_por_jig:
                found = seq_idx
                break
        if found != self._hover_idx:
            self._hover_idx = found
            self.setCursor(Qt.PointingHandCursor if found >= 0 else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event):
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.setCursor(Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._hover_idx >= 0:
            self.cavity_clicked.emit(self._hover_idx)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        w = self.width()
        h = self.height()

        # Fondo limpio de placa de aluminio anodizado
        bg_rect = QRectF(2, 2, w - 4, h - 4)
        painter.setPen(QPen(QColor("#CBD5E1"), 1.5))
        painter.setBrush(QColor("#F8FAFC"))
        painter.drawRoundedRect(bg_rect, 10, 10)

        # Cuadrícula técnica sutil
        grid_pen = QPen(QColor("#E2E8F0"), 1, Qt.DotLine)
        painter.setPen(grid_pen)
        for gx in range(40, int(w - 10), 40):
            painter.drawLine(gx, 6, gx, int(h - 6))
        for gy in range(40, int(h - 10), 40):
            painter.drawLine(6, gy, int(w - 6), gy)

        pad_x = 16.0
        pad_y = 14.0
        gap_x = 12.0
        gap_y = 10.0

        avail_w = max(80.0, w - 2 * pad_x - (self.cols - 1) * gap_x)
        avail_h = max(80.0, h - 2 * pad_y - (self.filas - 1) * gap_y)
        cell_w = avail_w / self.cols
        cell_h = avail_h / self.filas

        self._card_rects.clear()
        seq_centers = {}

        # Primero calculamos rectángulos y centros para trazar la ruta en serpiente
        for r in range(self.filas):
            for c in range(self.cols):
                seq_idx = self._grid_to_seq_index(r, c)
                cx = pad_x + c * (cell_w + gap_x)
                cy = pad_y + r * (cell_h + gap_y)
                rect = QRectF(cx, cy, cell_w, cell_h)
                self._card_rects[seq_idx] = rect
                seq_centers[seq_idx] = rect.center()

        # Línea guía del patrón de recorrido entre cavidades activas
        if self.piezas_por_jig > 1:
            route_pen = QPen(QColor(148, 163, 184, 110), 2.0, Qt.DashLine)
            painter.setPen(route_pen)
            for idx in range(self.piezas_por_jig - 1):
                if idx in seq_centers and (idx + 1) in seq_centers:
                    painter.drawLine(seq_centers[idx], seq_centers[idx + 1])

        # Dibujar cada cavidad
        for r in range(self.filas):
            for c in range(self.cols):
                seq_idx = self._grid_to_seq_index(r, c)
                rect = self._card_rects[seq_idx]
                habilitada = seq_idx < self.piezas_por_jig

                if not habilitada:
                    painter.setPen(QPen(QColor("#E2E8F0"), 1, Qt.DashLine))
                    painter.setBrush(QColor(241, 245, 249, 130))
                    painter.drawRoundedRect(rect, 8, 8)
                    painter.setPen(QColor("#94A3B8"))
                    f_off = QFont("Segoe UI", 8, QFont.Bold)
                    painter.setFont(f_off)
                    painter.drawText(rect, Qt.AlignCenter, "INHABILITADA")
                    continue

                estado = self.estados[seq_idx] if seq_idx < len(self.estados) else "pendiente"
                es_activa = (seq_idx == self.indice_activo) or (estado == "activa")
                es_hover = (seq_idx == self._hover_idx)

                if estado == "ok":
                    bg_col = QColor("#ECFDF5")
                    border_col = QColor("#10B981")
                    badge_col = QColor("#059669")
                    status_txt = "OK"
                elif estado == "nok":
                    bg_col = QColor("#FEF2F2")
                    border_col = QColor("#EF4444")
                    badge_col = QColor("#DC2626")
                    status_txt = "NOK"
                elif es_activa:
                    bg_col = QColor("#E0F2FE")
                    border_col = QColor("#0284C7")
                    badge_col = QColor("#0284C7")
                    status_txt = f"P{self.pasada_actual}/{self.total_pasadas}" if self.total_pasadas > 1 else "ACTIVA"
                else:
                    bg_col = QColor("#FFFFFF")
                    border_col = QColor("#94A3B8") if es_hover else QColor("#CBD5E1")
                    badge_col = QColor("#64748B")
                    status_txt = "PEND"

                # Halo pulsante si es la cavidad activa
                if es_activa:
                    alpha_halo = int(85 + 60 * math.sin(self._pulse_phase))
                    halo_pen = QPen(QColor(2, 132, 199, alpha_halo), 4.0)
                    painter.setPen(halo_pen)
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 10, 10)

                painter.setPen(QPen(border_col, 2.2 if (es_activa or es_hover) else 1.4))
                painter.setBrush(bg_col)
                painter.drawRoundedRect(rect, 8, 8)

                # Encabezado de tarjeta: #Pieza a la izquierda y Punto 0 / Estado a la derecha
                top_bar = QRectF(rect.left() + 6, rect.top() + 4, rect.width() - 12, 18)
                painter.setPen(QColor("#0F172A"))
                painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
                lbl_num = f"#{seq_idx + 1:02d}" + (" [SET 0]" if seq_idx == 0 else "")
                painter.drawText(top_bar, Qt.AlignLeft | Qt.AlignVCenter, lbl_num)

                painter.setPen(badge_col)
                painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
                painter.drawText(top_bar, Qt.AlignRight | Qt.AlignVCenter, f"● {status_txt}")

                # Imagen circular central o icono vectorial de pieza
                bottom_reserved = 20 if self.mostrar_coords else 6
                avail_img_h = max(14.0, rect.height() - 24 - bottom_reserved)
                diam = int(min(rect.width() * 0.56, avail_img_h * 0.88, 68))
                cx_center = rect.center().x()
                cy_center = rect.top() + 22 + avail_img_h / 2.0

                if diam >= 16:
                    circle_rect = QRectF(cx_center - diam / 2.0, cy_center - diam / 2.0, diam, diam)
                    pix = self._get_circular_pixmap(diam)
                    if pix:
                        painter.drawPixmap(int(circle_rect.left()), int(circle_rect.top()), pix)
                    else:
                        painter.setBrush(QColor("#E2E8F0"))
                        painter.setPen( Qt.NoPen)
                        painter.drawEllipse(circle_rect)
                    painter.setBrush(Qt.NoBrush)
                    painter.setPen(QPen(border_col, 1.8))
                    painter.drawEllipse(circle_rect)

                # Píldora inferior de coordenadas (X, Y)
                if self.mostrar_coords and seq_idx < len(self.coordenadas):
                    px, py = self.coordenadas[seq_idx]
                    pill_h = 16.0
                    pill_rect = QRectF(
                        rect.left() + 5,
                        rect.bottom() - pill_h - 4,
                        rect.width() - 10,
                        pill_h,
                    )
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor("#0F172A") if es_activa else QColor("#F1F5F9"))
                    painter.drawRoundedRect(pill_rect, 4, 4)

                    painter.setPen(QColor("#38BDF8") if es_activa else QColor("#334155"))
                    painter.setFont(QFont("Consolas", 7, QFont.Bold))
                    painter.drawText(pill_rect, Qt.AlignCenter, f"X:{px:.1f} Y:{py:.1f}")

        painter.end()


# ==============================================================================
# SISTEMA DE OVERLAYS MODALES INTEGRADOS EN EL MISMO MARCO (IN-WINDOW OVERLAYS)
# ==============================================================================
class InWindowOverlayLayer(QWidget):
    """
    Capa superpuesta semitransparente que se renderiza DENTRO de la ventana
    principal de PySide6. Sustituye todos los CTkToplevel y messagebox de
    Windows sin crear ventanas secundarias y sin heredar fondos grises en QLabel.
    """

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.main_win = parent_window
        self.setObjectName("OverlayBackdrop")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setVisible(False)

        # Estilo exclusivo por ID para no contaminar QLabels hijos con cajas grises
        self.setStyleSheet("""
            QWidget#OverlayBackdrop {
                background-color: rgba(15, 23, 42, 155);
            }
            QFrame#ModalCard {
                background-color: #FFFFFF;
                border: 2px solid #CBD5E1;
                border-radius: 14px;
            }
            QFrame#ModalSubCard {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 10px;
            }
            QLabel {
                background-color: transparent;
                border: none;
            }
            QComboBox {
                background-color: #FFFFFF;
                color: #0F172A;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 12px;
                font-weight: 700;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #0F172A;
                border: 1px solid #CBD5E1;
                selection-background-color: #E0F2FE;
                selection-color: #0369A1;
                outline: 0;
                padding: 4px;
            }
            QComboBox QAbstractItemView::item {
                background-color: #FFFFFF;
                color: #0F172A;
                min-height: 28px;
                padding: 4px 8px;
            }
            QComboBox QAbstractItemView::item:selected,
            QComboBox QAbstractItemView::item:hover {
                background-color: #E0F2FE;
                color: #0369A1;
            }
        """)

        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(24, 24, 24, 24)
        self.root_layout.setAlignment(Qt.AlignCenter)

        self.active_card = None

    def _clear_card(self):
        if self.active_card is not None:
            self.root_layout.removeWidget(self.active_card)
            self.active_card.deleteLater()
            self.active_card = None

    def close_overlay(self):
        self._clear_card()
        self.setVisible(False)

    def _prepare_card(self, width=500, min_height=220):
        self._clear_card()
        self.setGeometry(self.main_win.rect())
        card = QFrame(self)
        card.setObjectName("ModalCard")
        card.setFixedWidth(width)
        card.setMinimumHeight(min_height)
        self.root_layout.addWidget(card, 0, Qt.AlignCenter)
        self.active_card = card
        self.setVisible(True)
        self.raise_()
        return card

    # 1. Alerta / Confirmación integrada
    def show_message(self, titulo, mensaje, tipo="info", on_confirm=None):
        card = self._prepare_card(width=480, min_height=210)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)

        col_map = {
            "info": ("[INFO]", COLOR_BRAND_PRIMARY),
            "ok": ("[OK]", COLOR_OK),
            "warn": ("[AVISO]", COLOR_WARN),
            "error": ("[ERROR]", COLOR_ERR),
            "confirm": ("[CONFIRMAR]", COLOR_BRAND_PRIMARY),
        }
        ico, color_hex = col_map.get(tipo, ("[INFO]", COLOR_BRAND_PRIMARY))

        lbl_tit = QLabel(f"{ico}  {titulo}")
        lbl_tit.setStyleSheet(f"color: {color_hex}; font-size: 16px; font-weight: 800; background: transparent;")
        lbl_tit.setWordWrap(True)
        lay.addWidget(lbl_tit)

        sep = QFrame()
        sep.setFixedHeight(2)
        sep.setStyleSheet("background-color: #E2E8F0; border: none;")
        lay.addWidget(sep)

        lbl_msg = QLabel(mensaje)
        lbl_msg.setStyleSheet("color: #1E293B; font-size: 13px; font-weight: 500; background: transparent;")
        lbl_msg.setWordWrap(True)
        lay.addWidget(lbl_msg)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        if tipo == "confirm" or callable(on_confirm):
            btn_cancel = QPushButton("Cancelar")
            btn_cancel.setMinimumHeight(40)
            btn_cancel.setStyleSheet(
                "QPushButton { background-color: #E2E8F0; color: #1E293B; font-weight: 700; border-radius: 8px; }"
                "QPushButton:hover { background-color: #CBD5E1; }"
            )
            btn_cancel.clicked.connect(self.close_overlay)
            btn_row.addWidget(btn_cancel)

            btn_ok = QPushButton("Confirmar")
            btn_ok.setMinimumHeight(40)
            btn_ok.setStyleSheet(
                f"QPushButton {{ background-color: {color_hex}; color: #FFFFFF; font-weight: 800; border-radius: 8px; }}"
            )

            def _confirm_and_close():
                self.close_overlay()
                if callable(on_confirm):
                    on_confirm()

            btn_ok.clicked.connect(_confirm_and_close)
            btn_row.addWidget(btn_ok)
        else:
            btn_ok = QPushButton("ENTENDIDO")
            btn_ok.setMinimumHeight(42)
            btn_ok.setStyleSheet(
                f"QPushButton {{ background-color: {color_hex}; color: #FFFFFF; font-size: 13px; font-weight: 800; border-radius: 8px; }}"
            )
            btn_ok.clicked.connect(self.close_overlay)
            btn_row.addWidget(btn_ok)

        lay.addLayout(btn_row)

    # 2. Selector interactivo de Receta / Jig con vista previa en vivo
    def show_jig_selector(self, recetas, receta_activa_nombre, on_select_callback):
        card = self._prepare_card(width=620, min_height=420)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)

        lbl_tit = QLabel("SELECCIÓN DE JIG / NÚMERO DE PARTE")
        lbl_tit.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 17px; font-weight: 800;")
        lay.addWidget(lbl_tit)

        lbl_sub = QLabel("Seleccione el Jig o receta de producción que desea cargar en la estación:")
        lbl_sub.setStyleSheet("color: #475569; font-size: 12px;")
        lay.addWidget(lbl_sub)

        combo = QComboBox()
        combo.setView(QListView())
        combo.setMinimumHeight(38)
        nombres = [r.get("nombre", "") for r in recetas]
        combo.addItems(nombres)
        if receta_activa_nombre in nombres:
            combo.setCurrentText(receta_activa_nombre)
        lay.addWidget(combo)

        preview_card = QFrame()
        preview_card.setObjectName("ModalSubCard")
        p_lay = QHBoxLayout(preview_card)
        p_lay.setContentsMargins(14, 14, 14, 14)
        p_lay.setSpacing(16)

        lbl_img = QLabel()
        lbl_img.setFixedSize(115, 95)
        lbl_img.setAlignment(Qt.AlignCenter)
        lbl_img.setStyleSheet("background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 8px;")
        p_lay.addWidget(lbl_img)

        info_col = QVBoxLayout()
        info_col.setSpacing(4)
        lbl_p_nom = QLabel()
        lbl_p_nom.setStyleSheet("font-size: 14px; font-weight: 800; color: #0F172A;")
        lbl_p_mod = QLabel()
        lbl_p_mod.setStyleSheet("font-size: 12px; color: #334155;")
        lbl_p_mat = QLabel()
        lbl_p_mat.setStyleSheet("font-size: 12px; font-weight: 700; color: #0284C7;")
        lbl_p_dist = QLabel()
        lbl_p_dist.setStyleSheet("font-size: 12px; color: #475569;")
        lbl_p_set0 = QLabel()
        info_col.addWidget(lbl_p_nom)
        info_col.addWidget(lbl_p_mod)
        info_col.addWidget(lbl_p_mat)
        info_col.addWidget(lbl_p_dist)
        info_col.addWidget(lbl_p_set0)
        p_lay.addLayout(info_col, 1)
        lay.addWidget(preview_card)

        def _update_preview(nom):
            rec = next((r for r in recetas if r.get("nombre") == nom), None)
            if not rec:
                return
            pz = obtener_piezas_por_jig_receta(rec)
            lbl_p_nom.setText(f"Pieza: {rec.get('pieza', '-')}")
            lbl_p_mod.setText(f"Modelo: {rec.get('modelo', '-')}  |  Velocidad: {rec.get('vel_x', 100):.0f} mm/s")
            lbl_p_mat.setText(f"Matriz Fixture: {rec.get('filas', 1)} × {rec.get('columnas', 1)} ({pz} Piezas/Jig)")
            lbl_p_dist.setText(f"Dist. C-C: X={rec.get('espacio_x', 0):.1f} mm | Y={rec.get('espacio_y', 0):.1f} mm")
            cx = float(rec.get("cero_x", 0.0))
            cy = float(rec.get("cero_y", 0.0))
            if rec.get("set_0_listo", True):
                lbl_p_set0.setText(f"Cero Predeterminado Calibrado (X:{cx:.1f}, Y:{cy:.1f})")
                lbl_p_set0.setStyleSheet("color: #059669; font-size: 12px; font-weight: 700;")
            else:
                lbl_p_set0.setText("Set 0 Pendiente de Calibrar")
                lbl_p_set0.setStyleSheet("color: #D97706; font-size: 12px; font-weight: 700;")

            ruta = buscar_recurso(rec.get("imagen", ""))
            if ruta and os.path.exists(ruta):
                pix = QPixmap(ruta).scaled(108, 88, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                lbl_img.setPixmap(pix)
                lbl_img.setText("")
            else:
                lbl_img.setPixmap(QPixmap())
                lbl_img.setText("[Sin imagen]")

        combo.currentTextChanged.connect(_update_preview)
        _update_preview(combo.currentText())

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancelar")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setStyleSheet("background-color: #E2E8F0; color: #1E293B; font-weight: 700; border-radius: 8px;")
        btn_cancel.clicked.connect(self.close_overlay)
        btn_row.addWidget(btn_cancel)

        btn_load = QPushButton("Cargar Jig Seleccionado")
        btn_load.setMinimumHeight(40)
        btn_load.setStyleSheet(
            f"background-color: {COLOR_BRAND_PRIMARY}; color: #FFFFFF; font-weight: 800; border-radius: 8px;"
        )

        def _apply():
            sel = combo.currentText()
            self.close_overlay()
            on_select_callback(sel)

        btn_load.clicked.connect(_apply)
        btn_row.addWidget(btn_load)
        lay.addLayout(btn_row)

    # 3. Modal de Registro de Fin de Hora (Conteo Total Automático + NO OK Manual)
    def show_hourly_log_modal(self, receta_nom, total_actual, nok_actual, on_save_callback):
        card = self._prepare_card(width=520, min_height=390)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)

        lbl_tit = QLabel("CORTE Y REGISTRO DE FIN DE HORA")
        lbl_tit.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 17px; font-weight: 800;")
        lay.addWidget(lbl_tit)

        lbl_sub = QLabel("El conteo total se realiza automáticamente. Ingrese manualmente las piezas NO OK.")
        lbl_sub.setStyleSheet("color: #475569; font-size: 11px; font-weight: 600;")
        lbl_sub.setWordWrap(True)
        lay.addWidget(lbl_sub)

        lbl_rec = QLabel(f"Receta Activa: {receta_nom}")
        lbl_rec.setStyleSheet("color: #334155; font-size: 12px; font-weight: 700;")
        lay.addWidget(lbl_rec)

        total_fijo = max(0, int(total_actual))

        form_box = QFrame()
        form_box.setObjectName("ModalSubCard")
        flay = QGridLayout(form_box)
        flay.setContentsMargins(14, 14, 14, 14)
        flay.setSpacing(10)

        lbl_tot_caption = QLabel("Total Piezas (Conteo Automático):")
        lbl_tot_caption.setStyleSheet("font-size: 12px; font-weight: 700; color: #334155;")
        flay.addWidget(lbl_tot_caption, 0, 0)

        lbl_tot_val = QLabel(str(total_fijo))
        lbl_tot_val.setAlignment(Qt.AlignCenter)
        lbl_tot_val.setMinimumHeight(34)
        lbl_tot_val.setStyleSheet(
            f"background-color: #E2E8F0; color: {COLOR_BRAND_PRIMARY}; font-family: Consolas; "
            "font-size: 16px; font-weight: 900; border: 1px solid #CBD5E1; border-radius: 6px;"
        )
        flay.addWidget(lbl_tot_val, 0, 1, 1, 3)

        lbl_nok_caption = QLabel("Piezas NO OK (Ingreso Manual):")
        lbl_nok_caption.setStyleSheet(f"font-size: 12px; font-weight: 800; color: {COLOR_ERR};")
        flay.addWidget(lbl_nok_caption, 1, 0)

        btn_minus = QPushButton("-")
        btn_minus.setFixedSize(38, 34)
        btn_minus.setStyleSheet(
            f"background-color: #FEE2E2; color: {COLOR_ERR}; font-size: 16px; font-weight: 900; border-radius: 6px;"
        )
        inp_nok = QLineEdit(str(max(0, min(total_fijo, int(nok_actual)))))
        inp_nok.setAlignment(Qt.AlignCenter)
        inp_nok.setMinimumHeight(34)
        inp_nok.setStyleSheet(
            f"background-color: #FFFFFF; color: {COLOR_ERR}; font-family: Consolas; font-size: 15px; font-weight: 900; "
            f"border: 2px solid {COLOR_ERR}; border-radius: 6px;"
        )
        btn_plus = QPushButton("+")
        btn_plus.setFixedSize(38, 34)
        btn_plus.setStyleSheet(
            f"background-color: #FEE2E2; color: {COLOR_ERR}; font-size: 16px; font-weight: 900; border-radius: 6px;"
        )
        flay.addWidget(btn_minus, 1, 1)
        flay.addWidget(inp_nok, 1, 2)
        flay.addWidget(btn_plus, 1, 3)

        lbl_ok_caption = QLabel("Piezas OK (Cálculo Automático):")
        lbl_ok_caption.setStyleSheet(f"font-size: 12px; font-weight: 700; color: {COLOR_OK};")
        flay.addWidget(lbl_ok_caption, 2, 0)

        lbl_ok_val = QLabel()
        lbl_ok_val.setAlignment(Qt.AlignCenter)
        lbl_ok_val.setMinimumHeight(34)
        lbl_ok_val.setStyleSheet(
            f"background-color: #ECFDF5; color: {COLOR_OK}; font-family: Consolas; "
            "font-size: 16px; font-weight: 900; border: 1px solid #10B981; border-radius: 6px;"
        )
        flay.addWidget(lbl_ok_val, 2, 1, 1, 3)

        lbl_calc = QLabel()
        lbl_calc.setAlignment(Qt.AlignCenter)
        lbl_calc.setStyleSheet("font-size: 13px; font-weight: 800; color: #059669;")
        flay.addWidget(lbl_calc, 3, 0, 1, 4)

        lay.addWidget(form_box)

        def _recalc():
            try:
                n = max(0, min(total_fijo, int(float(inp_nok.text() or 0))))
            except ValueError:
                n = 0
            ok_v = max(0, total_fijo - n)
            cal = (ok_v / total_fijo * 100.0) if total_fijo > 0 else 100.0
            lbl_ok_val.setText(str(ok_v))
            lbl_calc.setText(f"Calidad del Corte: {cal:.1f}%   (Total Auto: {total_fijo} | OK: {ok_v} | NO OK: {n})")

        def _adj_nok(delta):
            try:
                cur = int(float(inp_nok.text() or 0))
            except ValueError:
                cur = 0
            nuevo_nok = max(0, min(total_fijo, cur + delta))
            inp_nok.setText(str(nuevo_nok))
            _recalc()

        btn_minus.clicked.connect(lambda: _adj_nok(-1))
        btn_plus.clicked.connect(lambda: _adj_nok(1))
        inp_nok.textChanged.connect(lambda _: _recalc())
        _recalc()

        btn_kb = QPushButton("Abrir Teclado en Pantalla")
        btn_kb.setMinimumHeight(32)
        btn_kb.clicked.connect(abrir_teclado_tactil_windows)
        lay.addWidget(btn_kb)

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancelar")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setStyleSheet("background-color: #E2E8F0; color: #1E293B; font-weight: 700; border-radius: 8px;")
        btn_cancel.clicked.connect(self.close_overlay)
        btn_row.addWidget(btn_cancel)

        btn_save = QPushButton("Guardar Corte de Hora")
        btn_save.setMinimumHeight(40)
        btn_save.setStyleSheet(f"background-color: {COLOR_OK}; color: #FFFFFF; font-weight: 800; border-radius: 8px;")

        def _commit():
            try:
                n = max(0, min(total_fijo, int(float(inp_nok.text() or 0))))
            except ValueError:
                n = 0
            self.close_overlay()
            on_save_callback(total_fijo, n)

        btn_save.clicked.connect(_commit)
        btn_row.addWidget(btn_save)
        lay.addLayout(btn_row)

    # 4. Teclado Numérico Táctil para Paso JOG
    def show_jog_numpad(self, current_val, on_apply_callback):
        card = self._prepare_card(width=400, min_height=390)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)

        lbl_tit = QLabel("PASO JOG MANUAL (0.1 — 200.0 mm)")
        lbl_tit.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 15px; font-weight: 800;")
        lay.addWidget(lbl_tit)

        disp = QLineEdit(str(current_val))
        disp.setAlignment(Qt.AlignRight)
        disp.setMinimumHeight(42)
        disp.setStyleSheet("font-size: 18px; font-weight: 800; padding-right: 10px;")
        lay.addWidget(disp)

        # Presets rápidos
        preset_row = QHBoxLayout()
        for pv in ("0.5", "1", "2", "5", "10"):
            b = QPushButton(f"{pv}mm")
            b.setMinimumHeight(30)
            b.setStyleSheet("background-color: #E0F2FE; color: #0369A1; font-weight: 700; border-radius: 6px;")
            b.clicked.connect(lambda _, v=pv: disp.setText(v))
            preset_row.addWidget(b)
        lay.addLayout(preset_row)

        grid = QGridLayout()
        grid.setSpacing(6)
        keys = [
            ("7", 0, 0), ("8", 0, 1), ("9", 0, 2),
            ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
            ("1", 2, 0), ("2", 2, 1), ("3", 2, 2),
            (".", 3, 0), ("0", 3, 1), ("DEL", 3, 2),
        ]

        def _press(k):
            cur = disp.text()
            if k == "DEL":
                disp.setText(cur[:-1])
            elif k == ".":
                if "." not in cur:
                    disp.setText((cur or "0") + ".")
            else:
                disp.setText(cur + k if cur != "0" else k)

        for k, r, c in keys:
            btn = QPushButton(k)
            btn.setMinimumHeight(40)
            btn.setStyleSheet(
                "QPushButton { background-color: #F1F5F9; color: #0F172A; font-size: 15px; font-weight: 800; border-radius: 8px; }"
                "QPushButton:hover { background-color: #E2E8F0; }"
            )
            btn.clicked.connect(lambda _, key=k: _press(key))
            grid.addWidget(btn, r, c)
        lay.addLayout(grid)

        act_row = QHBoxLayout()
        btn_c = QPushButton("Cancelar")
        btn_c.setMinimumHeight(38)
        btn_c.clicked.connect(self.close_overlay)
        act_row.addWidget(btn_c)

        btn_ok = QPushButton("Aplicar")
        btn_ok.setMinimumHeight(38)
        btn_ok.setStyleSheet(f"background-color: {COLOR_OK}; color: white; font-weight: 800; border-radius: 8px;")

        def _done():
            try:
                v = round(max(0.1, min(200.0, float(disp.text() or 1.0))), 2)
            except ValueError:
                v = 5.0
            self.close_overlay()
            on_apply_callback(v)

        btn_ok.clicked.connect(_done)
        act_row.addWidget(btn_ok)
        lay.addLayout(act_row)


# ==============================================================================
# HOJA DE ESTILOS INDUSTRIAL QSS (SIN CAJAS GRISES EN ETIQUETAS)
# ==============================================================================
KOSTAL_GLOBAL_QSS = f"""
QMainWindow, QWidget#RootCentral {{
    background-color: {COLOR_BG_MAIN};
    color: {COLOR_TEXT_PRI};
    font-family: "Segoe UI", Arial, sans-serif;
}}
QLabel {{
    background-color: transparent;
    border: none;
    color: {COLOR_TEXT_PRI};
}}
QFrame#HeaderBar {{
    background-color: {COLOR_HEADER};
    border-bottom: 2px solid #1E293B;
}}
QFrame#IndustrialCard {{
    background-color: {COLOR_BG_CARD};
    border: 1px solid {COLOR_BORDER};
    border-radius: 10px;
}}
QFrame#SubSurface {{
    background-color: {COLOR_BG_SURFACE};
    border: 1px solid #E2E8F0;
    border-radius: 8px;
}}
QFrame#DarkControlBox {{
    background-color: #0F172A;
    border: 1px solid #1E293B;
    border-radius: 8px;
}}
QLineEdit, QComboBox {{
    background-color: #FFFFFF;
    color: {COLOR_TEXT_PRI};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
    font-weight: 600;
}}
QLineEdit:focus, QComboBox:focus {{
    border: 2px solid {COLOR_ACCENT_CYAN};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid #E2E8F0;
    background-color: #F8FAFC;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: #FFFFFF;
    color: {COLOR_TEXT_PRI};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: #E0F2FE;
    selection-color: #0369A1;
    outline: 0;
    padding: 4px;
}}
QComboBox QAbstractItemView::item {{
    background-color: #FFFFFF;
    color: {COLOR_TEXT_PRI};
    min-height: 28px;
    padding: 4px 8px;
}}
QComboBox QAbstractItemView::item:selected,
QComboBox QAbstractItemView::item:hover {{
    background-color: #E0F2FE;
    color: #0369A1;
}}
QPushButton {{
    background-color: {COLOR_BRAND_PRIMARY};
    color: #FFFFFF;
    border: none;
    border-radius: 7px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 700;
}}
QPushButton:hover {{
    background-color: {COLOR_BRAND_HOVER};
}}
QPushButton:disabled {{
    background-color: {COLOR_BTN_DISABLED};
    color: {COLOR_TEXT_DISABLED};
}}
QProgressBar {{
    background-color: #E2E8F0;
    border: 1px solid #CBD5E1;
    border-radius: 5px;
    text-align: center;
    font-size: 10px;
    font-weight: 800;
    color: #0F172A;
}}
QProgressBar::chunk {{
    background-color: {COLOR_ACCENT_CYAN};
    border-radius: 4px;
}}
QTableWidget {{
    background-color: #FFFFFF;
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    gridline-color: #E2E8F0;
    font-size: 12px;
}}
QHeaderView::section {{
    background-color: #F1F5F9;
    color: #0F172A;
    font-weight: 800;
    padding: 6px;
    border: none;
    border-bottom: 1px solid #CBD5E1;
}}
"""


# ==============================================================================
# VENTANA PRINCIPAL KOSTAL HMI (SPLASH -> LOGIN -> OPERADOR -> ADMIN)
# ==============================================================================
class KostalHmiWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KOSTAL — Sistema de Control de Tampografía Industrial (PySide6 60 FPS)")
        self.resize(1366, 780)
        self.setMinimumSize(1120, 680)
        self.setStyleSheet(KOSTAL_GLOBAL_QSS)

        # 1. Cargar configuración y estado operativo idéntico al original
        self.config = cargar_configuracion_disco()
        self.idioma = self.config.get("idioma", "ES")
        if self.idioma not in ("ES", "EN"):
            self.idioma = "ES"

        self.usuario_actual = {"user": "operador", "rol": "OPERADOR", "nombre": "Juan Pérez"}
        self.sesion_segundos = 0

        # Variables de estado de máquina y seguridad post-PARO
        self.estado_pintando = False
        self.estado_stop = False
        self.paro_requiere_reset = False
        self.paro_requiere_inicio = False
        self.estado_manual = False
        self.estado_auto = True
        self.set_0_confirmado = bool(self.config.get("set_0_confirmado", True))
        self.mostrar_coords_matriz = True
        self.tampo_manual_activo = False
        self.tampo_countdown_sec = 0

        # Posiciones y parámetros
        self.pos_actual_x = 0.0
        self.pos_actual_y = 0.0
        self.ultimo_plc_rx_x = 0.0
        self.ultimo_plc_rx_y = 0.0
        self.plc_conectado = False
        self.modo_conexion_str = "VERIFICANDO..."

        self.home_guardado_x = limitar_coord_x(self.config.get("home_x", 0.0))
        self.home_guardado_y = limitar_coord_y(self.config.get("home_y", 0.0))
        self.cero_guardado_x = limitar_coord_x(self.config.get("cero_x", 0.0))
        self.cero_guardado_y = limitar_coord_y(self.config.get("cero_y", 0.0))

        self.velocidad_actual = float(self.config.get("vel_default", 100.0))
        self.paso_jog = float(self.config.get("paso_jog_default", 5.0))
        self.pasadas_tampo = 2
        self.config["pasadas_tampo"] = 2
        self.patron_pintado = self.config.get("patron_pintado", "vertical")

        # Matriz dinámica
        self.matriz_filas = 3
        self.matriz_cols = 5
        self.piezas_por_jig_actual = 15
        self.matriz_delta_x = 20.0
        self.matriz_delta_y = 35.0
        self.matriz_dist_diag = 40.31
        self.matriz_coordenadas = []
        self.estados_piezas = []
        self.indice_matriz_actual = 0
        self.pasada_actual_pieza = 1
        self.piezas_realizadas_lote = 0

        self.produccion_total = int(self.config.get("produccion_total", 0))
        self.produccion_ok = int(self.config.get("produccion_ok", 0))
        self.produccion_nok = int(self.config.get("produccion_nok", 0))

        # Señales del PLC
        self.plc_signals = {
            "PINTADO": False, "M_ENE": False, "M_SET": False,
            "LISTO": False, "NO_LISTO": False, "ALARM_X": False,
            "ALARM_Y": False, "PE": False, "SENSOR_X": False,
            "SENSOR_Y": False, "TAMPO": False,
        }
        self.led_widgets = {}

        # Contenedor raíz y pila de vistas
        self.central_root = QWidget()
        self.central_root.setObjectName("RootCentral")
        self.setCentralWidget(self.central_root)
        root_lay = QVBoxLayout(self.central_root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self.stack = QStackedWidget()
        root_lay.addWidget(self.stack)

        # Construir las 4 vistas principales dentro del QStackedWidget
        self.page_splash = self._build_splash_page()
        self.page_login = self._build_login_page()
        self.page_operator = self._build_operator_page()
        self.page_admin = self._build_admin_page()

        self.stack.addWidget(self.page_splash)    # index 0
        self.stack.addWidget(self.page_login)     # index 1
        self.stack.addWidget(self.page_operator)  # index 2
        self.stack.addWidget(self.page_admin)     # index 3
        self.stack.setCurrentIndex(0)

        # Overlay modal integrado sobre toda la ventana
        self.overlay = InWindowOverlayLayer(self)

        # Cargar receta inicial
        rec_ini = self.config.get("receta_activa", "")
        if not rec_ini and self.config.get("recetas"):
            rec_ini = self.config["recetas"][0]["nombre"]
        self.cargar_receta_por_nombre(rec_ini, silencioso=True)
        self.cambiar_idioma(self.idioma)

        # Timers de sesión y simulación de avance de ciclo en modo desconectado
        self.session_timer = QTimer(self)
        self.session_timer.timeout.connect(self._tick_session_clock)
        self.session_timer.start(1000)

        self.sim_cycle_timer = QTimer(self)
        self.sim_cycle_timer.timeout.connect(self._tick_sim_cycle)

        # Iniciar hilo OPC UA
        self.opc_worker = OpcUaWorker(self.config, self)
        self.opc_worker.splash_progress.connect(self._on_splash_progress)
        self.opc_worker.connection_Changed.connect(self._on_plc_connection_Changed)
        self.opc_worker.telemetry_updated.connect(self._on_plc_telemetry)
        self.opc_worker.cycle_step_completed.connect(self._avanzar_siguiente_pieza_ciclo)
        self.opc_worker.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "overlay") and self.overlay.isVisible():
            self.overlay.setGeometry(self.rect())

    def closeEvent(self, event):
        guardar_configuracion_disco(self.config)
        if hasattr(self, "opc_worker"):
            self.opc_worker.stop_worker()
        super().closeEvent(event)

    def _tr(self, key):
        return DICCIONARIO_IDIOMAS.get(self.idioma, DICCIONARIO_IDIOMAS["ES"]).get(key, key)

    # ==========================================================================
    # PÁGINA 0: SPLASH SCREEN DE VERIFICACIÓN DE CONEXIÓN AL PLC
    # ==========================================================================
    def _build_splash_page(self):
        page = QWidget()
        page.setStyleSheet(f"background-color: {COLOR_BG_MAIN};")
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setFixedSize(680, 410)
        card.setStyleSheet(
            "QFrame { background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 16px; }"
        )
        clay = QVBoxLayout(card)
        clay.setContentsMargins(36, 30, 36, 28)
        clay.setSpacing(12)

        # Contenedor de Logos Corporativos Oficiales (KOSTAL & Baluarte)
        logos_row = QHBoxLayout()
        logos_row.setAlignment(Qt.AlignCenter)
        logos_row.setSpacing(16)

        pix_kostal_sp = cargar_logo_corporativo("LogoKostal.webp", 280, 58)
        lbl_brand = QLabel()
        lbl_brand.setStyleSheet("border: none; background: transparent;")
        if pix_kostal_sp:
            lbl_brand.setPixmap(pix_kostal_sp)
        else:
            lbl_brand.setText("KOSTAL")
            lbl_brand.setStyleSheet(
                f"color: {COLOR_BRAND_PRIMARY}; font-size: 36px; font-weight: 900; letter-spacing: 3px; border: none;"
            )
        logos_row.addWidget(lbl_brand, 0, Qt.AlignVCenter)

        pix_baluarte_sp = cargar_logo_corporativo("LOGO_BALUARTE.png", 210, 50)
        if pix_baluarte_sp:
            div_sp = QFrame()
            div_sp.setFixedSize(2, 48)
            div_sp.setStyleSheet("background-color: #E2E8F0; border: none;")
            logos_row.addWidget(div_sp, 0, Qt.AlignVCenter)

            lbl_bal_sp = QLabel()
            lbl_bal_sp.setStyleSheet("border: none; background: transparent;")
            lbl_bal_sp.setPixmap(pix_baluarte_sp)
            logos_row.addWidget(lbl_bal_sp, 0, Qt.AlignVCenter)

        clay.addLayout(logos_row)
        clay.addSpacing(4)

        lbl_title_sp = QLabel("SMC — ESTACIÓN DE INSERCIÓN Y TAMPOGRAFÍA")
        lbl_title_sp.setAlignment(Qt.AlignCenter)
        lbl_title_sp.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 17px; font-weight: 900; border: none; background: transparent;"
        )
        clay.addWidget(lbl_title_sp)

        lbl_sub = QLabel("KOSTAL AUTOMOTIVE ELECTRICAL SYSTEMS | PROYECTOS DE INTEGRACIÓN BALUARTE")
        lbl_sub.setAlignment(Qt.AlignCenter)
        lbl_sub.setStyleSheet("color: #475569; font-size: 11px; font-weight: 700; border: none; background: transparent;")
        clay.addWidget(lbl_sub)

        clay.addSpacing(10)

        self.splash_bar = QProgressBar()
        self.splash_bar.setRange(0, 100)
        self.splash_bar.setValue(5)
        self.splash_bar.setFixedHeight(22)
        clay.addWidget(self.splash_bar)

        self.lbl_splash_status = QLabel("Inicializando subsistemas...")
        self.lbl_splash_status.setAlignment(Qt.AlignCenter)
        self.lbl_splash_status.setWordWrap(True)
        self.lbl_splash_status.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 13px; font-weight: 700; border: none; background: transparent;"
        )
        clay.addWidget(self.lbl_splash_status)

        clay.addStretch(1)

        self.splash_btn_box = QWidget()
        self.splash_btn_box.setStyleSheet("background: transparent; border: none;")
        sblay = QHBoxLayout(self.splash_btn_box)
        sblay.setContentsMargins(0, 0, 0, 0)
        sblay.setSpacing(12)

        btn_retry = QPushButton("Reintentar Conexión al PLC")
        btn_retry.setMinimumHeight(42)
        btn_retry.setStyleSheet(
            f"background-color: {COLOR_BRAND_PRIMARY}; color: white; font-weight: 800; border-radius: 8px;"
        )
        btn_retry.clicked.connect(self._splash_retry_plc)
        sblay.addWidget(btn_retry)

        btn_sim = QPushButton("Iniciar en Modo Simulación")
        btn_sim.setMinimumHeight(42)
        btn_sim.setStyleSheet(
            f"background-color: {COLOR_WARN}; color: white; font-weight: 800; border-radius: 8px;"
        )
        btn_sim.clicked.connect(self._splash_enter_Simulation)
        sblay.addWidget(btn_sim)

        self.splash_btn_box.setVisible(False)
        clay.addWidget(self.splash_btn_box)

        lay.addWidget(card)
        return page

    def _activate_login_page(self):
        """Activa la vista de Login, enfoca la contraseña y despliega el teclado por defecto."""
        self.stack.setCurrentIndex(1)
        if hasattr(self, "inp_login_pass"):
            QTimer.singleShot(150, self._focus_and_open_login_keyboard)

    def _focus_and_open_login_keyboard(self):
        if hasattr(self, "inp_login_pass") and self.stack.currentIndex() == 1:
            self.inp_login_pass.setFocus()
            abrir_teclado_tactil_windows()

    @Slot(int, str, bool, bool)
    def _on_splash_progress(self, pct, msg, finished, connected):
        self.splash_bar.setValue(pct)
        self.lbl_splash_status.setText(msg)
        if finished:
            if connected:
                self.splash_btn_box.setVisible(False)
                QTimer.singleShot(450, self._activate_login_page)
            else:
                self.splash_btn_box.setVisible(True)

    def _splash_retry_plc(self):
        self.splash_btn_box.setVisible(False)
        self.lbl_splash_status.setText("Reintentando enlace OPC UA con el PLC WAGO...")
        self.opc_worker.request_retry_connection()

    def _splash_enter_Simulation(self):
        self.opc_worker.activate_Simulation_mode()
        self._activate_login_page()

    # ==========================================================================
    # PÁGINA 1: PANTALLA DE LOGIN INDUSTRIAL
    # ==========================================================================
    def _build_login_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 18, 24, 18)

        # Barra superior de ventana en Login
        top_bar = QHBoxLayout()
        lbl_est = QLabel(f"ESTACIÓN: {self.config.get('estacion_nombre', 'EST-KOSTAL-04')}")
        lbl_est.setStyleSheet("font-size: 13px; font-weight: 800; color: #334155;")
        top_bar.addWidget(lbl_est)
        top_bar.addStretch(1)

        btn_kb_top = QPushButton("Teclado")
        btn_kb_top.setStyleSheet("background-color: #334155; color: white; padding: 6px 12px;")
        btn_kb_top.clicked.connect(lambda: abrir_teclado_tactil_windows(forzar=True))
        top_bar.addWidget(btn_kb_top)

        btn_min = QPushButton("Minimizar")
        btn_min.setStyleSheet("background-color: #475569; color: white; padding: 6px 12px;")
        btn_min.clicked.connect(self.showMinimized)
        top_bar.addWidget(btn_min)

        btn_fs = QPushButton("Pantalla Completa")
        btn_fs.setStyleSheet("background-color: #1E293B; color: white; padding: 6px 12px;")
        btn_fs.clicked.connect(self.alternar_pantalla_completa)
        top_bar.addWidget(btn_fs)
        lay.addLayout(top_bar)

        lay.addStretch(1)

        card = QFrame()
        card.setObjectName("IndustrialCard")
        card.setFixedWidth(500)
        clay = QVBoxLayout(card)
        clay.setContentsMargins(36, 30, 36, 26)
        clay.setSpacing(12)

        # Logotipo Oficial KOSTAL (LogoKostal.webp)
        pix_kostal_login = cargar_logo_corporativo("LogoKostal.webp", 280, 58)
        lbl_logo = QLabel()
        lbl_logo.setAlignment(Qt.AlignCenter)
        if pix_kostal_login:
            lbl_logo.setPixmap(pix_kostal_login)
        else:
            lbl_logo.setText("KOSTAL")
            lbl_logo.setStyleSheet(
                f"color: {COLOR_BRAND_PRIMARY}; font-size: 34px; font-weight: 900; letter-spacing: 3px;"
            )
        clay.addWidget(lbl_logo)

        lbl_sys = QLabel("SISTEMA HMI DE INSPECCIÓN")
        lbl_sys.setAlignment(Qt.AlignCenter)
        lbl_sys.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 18px; font-weight: 900;")
        clay.addWidget(lbl_sys)

        lbl_title = QLabel("Línea de Ensamblado & Calidad KOSTAL")
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet("color: #475569; font-size: 13px; font-weight: 600;")
        clay.addWidget(lbl_title)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #CBD5E1;")
        clay.addWidget(sep)

        clay.addWidget(QLabel("Seleccionar Usuario / Turno:"))
        self.combo_login_user = QComboBox()
        self.combo_login_user.setView(QListView())
        self.combo_login_user.setMinimumHeight(42)
        self._refresh_login_users_combo()
        clay.addWidget(self.combo_login_user)

        clay.addWidget(QLabel("Contraseña / PIN de Acceso:"))
        self._login_pass_visible = False
        self.inp_login_pass = QLineEdit()
        self.inp_login_pass.setEchoMode(QLineEdit.Password)
        self.inp_login_pass.setPlaceholderText("Ingrese contraseña...")
        self.inp_login_pass.setMinimumHeight(44)
        self.inp_login_pass.setTextMargins(0, 0, 40, 0)
        self.inp_login_pass.returnPressed.connect(self._attempt_login)

        # Icono vectorial de ojo integrado dentro de la misma barra de input
        in_pass_lay = QHBoxLayout(self.inp_login_pass)
        in_pass_lay.setContentsMargins(0, 0, 6, 0)
        in_pass_lay.setSpacing(0)
        in_pass_lay.addStretch(1)

        self.btn_login_eye = QPushButton(self.inp_login_pass)
        self.btn_login_eye.setFixedSize(32, 32)
        self.btn_login_eye.setCursor(Qt.PointingHandCursor)
        self.btn_login_eye.setFocusPolicy(Qt.NoFocus)
        self.btn_login_eye.setToolTip("Mostrar u ocultar contraseña")
        self.btn_login_eye.setIcon(crear_icono_ojo(False))
        self.btn_login_eye.setIconSize(QSize(20, 20))
        self.btn_login_eye.setStyleSheet(
            "QPushButton { background-color: transparent; border: none; border-radius: 6px; padding: 0px; }"
            "QPushButton:hover { background-color: #E2E8F0; }"
        )
        self.btn_login_eye.clicked.connect(self._toggle_login_password_visibility)
        in_pass_lay.addWidget(self.btn_login_eye, 0, Qt.AlignRight | Qt.AlignVCenter)

        clay.addWidget(self.inp_login_pass)

        self.lbl_login_err = QLabel("")
        self.lbl_login_err.setStyleSheet(f"color: {COLOR_ERR}; font-size: 12px; font-weight: 700;")
        self.lbl_login_err.setAlignment(Qt.AlignCenter)
        clay.addWidget(self.lbl_login_err)

        btn_enter = QPushButton("INICIAR SESIÓN EN ESTACIÓN")
        btn_enter.setMinimumHeight(46)
        btn_enter.setStyleSheet(
            f"background-color: {COLOR_BRAND_PRIMARY}; color: white; font-size: 14px; font-weight: 800; border-radius: 8px;"
        )
        btn_enter.clicked.connect(self._attempt_login)
        clay.addWidget(btn_enter)

        # Pie corporativo Baluarte (fiel a kostalcode.py)
        pix_bal_login = cargar_logo_corporativo("LOGO_BALUARTE.png", 190, 44)
        if pix_bal_login:
            foot_row = QHBoxLayout()
            foot_row.setAlignment(Qt.AlignCenter)
            foot_row.setSpacing(10)
            lbl_alianza = QLabel("Desarrollado en alianza con")
            lbl_alianza.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600;")
            foot_row.addWidget(lbl_alianza, 0, Qt.AlignVCenter)

            lbl_bal_logo = QLabel()
            lbl_bal_logo.setPixmap(pix_bal_login)
            foot_row.addWidget(lbl_bal_logo, 0, Qt.AlignVCenter)
            clay.addLayout(foot_row)

        lay.addWidget(card, 0, Qt.AlignCenter)
        lay.addStretch(1)
        return page

    def _toggle_login_password_visibility(self):
        self._login_pass_visible = not getattr(self, "_login_pass_visible", False)
        if self._login_pass_visible:
            self.inp_login_pass.setEchoMode(QLineEdit.Normal)
            self.btn_login_eye.setIcon(crear_icono_ojo(True))
            self.btn_login_eye.setStyleSheet(
                "QPushButton { background-color: #E0F2FE; border: none; border-radius: 6px; padding: 0px; }"
                "QPushButton:hover { background-color: #BAE6FD; }"
            )
        else:
            self.inp_login_pass.setEchoMode(QLineEdit.Password)
            self.btn_login_eye.setIcon(crear_icono_ojo(False))
            self.btn_login_eye.setStyleSheet(
                "QPushButton { background-color: transparent; border: none; border-radius: 6px; padding: 0px; }"
                "QPushButton:hover { background-color: #E2E8F0; }"
            )
        self.inp_login_pass.style().unpolish(self.inp_login_pass)
        self.inp_login_pass.style().polish(self.inp_login_pass)
        self.inp_login_pass.update()
        self.inp_login_pass.setFocus()

    def _refresh_login_users_combo(self):
        if not hasattr(self, "combo_login_user"):
            return
        self.combo_login_user.clear()
        for u in self.config.get("usuarios", []):
            etiqueta = f"{u.get('nombre', u.get('user'))} ({u.get('rol', 'OPERADOR')}) — [{u.get('user')}]"
            self.combo_login_user.addItem(etiqueta, u.get("user"))

    def _attempt_login(self):
        user_id = self.combo_login_user.currentData()
        pwd = self.inp_login_pass.text().strip()
        usuarios = self.config.get("usuarios", [])
        encontrado = next((u for u in usuarios if u.get("user") == user_id and u.get("pass") == pwd), None)
        if not encontrado:
            self.lbl_login_err.setText("Contraseña incorrecta. Verifique sus credenciales.")
            return
        ocultar_teclado_tactil_windows()
        self.lbl_login_err.setText("")
        self.inp_login_pass.clear()
        self._login_pass_visible = False
        self.inp_login_pass.setEchoMode(QLineEdit.Password)
        self.btn_login_eye.setIcon(crear_icono_ojo(False))
        self.btn_login_eye.setStyleSheet(
            "QPushButton { background-color: transparent; border: none; border-radius: 6px; padding: 0px; }"
            "QPushButton:hover { background-color: #E2E8F0; }"
        )
        self.usuario_actual = dict(encontrado)
        self.sesion_segundos = 0
        self._update_user_header_ui()
        self.stack.setCurrentIndex(2)
        log_debug_hmi(f"Sesión iniciada por {self.usuario_actual.get('nombre')} ({self.usuario_actual.get('rol')})")


    # ==========================================================================
    # PÁGINA 2: PANTALLA PRINCIPAL DEL OPERADOR (3 COLUMNAS RESPONSIVAS)
    # ==========================================================================
    def _build_operator_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- HEADER SUPERIOR ----------------
        header = QFrame()
        header.setObjectName("HeaderBar")
        header.setFixedHeight(54)
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(14, 6, 14, 6)
        hlay.setSpacing(10)

        pix_kostal_hdr = cargar_logo_corporativo("LogoKostal.webp", 145, 28)
        lbl_logo = QLabel()
        if pix_kostal_hdr:
            lbl_logo.setPixmap(pix_kostal_hdr)
        else:
            lbl_logo.setText("KOSTAL")
            lbl_logo.setStyleSheet("color: #FFFFFF; font-size: 22px; font-weight: 900; letter-spacing: 2px;")
        hlay.addWidget(lbl_logo)

        self.lbl_header_estacion = QLabel(self.config.get("estacion_nombre", "EST-KOSTAL-04"))
        self.lbl_header_estacion.setStyleSheet(
            "background-color: #1E293B; color: #38BDF8; font-size: 11px; font-weight: 800; "
            "padding: 4px 8px; border-radius: 5px;"
        )
        hlay.addWidget(self.lbl_header_estacion)

        pix_bal_hdr = cargar_logo_corporativo("LOGO_BALUARTE.png", 130, 32)
        if pix_bal_hdr:
            bal_badge = QFrame()
            bal_badge.setStyleSheet("background-color: #FFFFFF; border-radius: 6px;")
            bal_lay = QHBoxLayout(bal_badge)
            bal_lay.setContentsMargins(8, 2, 8, 2)
            lbl_bal_hdr = QLabel()
            lbl_bal_hdr.setPixmap(pix_bal_hdr)
            bal_lay.addWidget(lbl_bal_hdr)
            hlay.addWidget(bal_badge)

        hlay.addStretch(1)

        # Banner de Estado General Central
        self.lbl_banner_estado = QLabel("SISTEMA LISTO PARA OPERAR")
        self.lbl_banner_estado.setAlignment(Qt.AlignCenter)
        self.lbl_banner_estado.setMinimumWidth(340)
        self.lbl_banner_estado.setFixedHeight(36)
        self.lbl_banner_estado.setStyleSheet(
            f"background-color: {COLOR_OK}; color: #FFFFFF; font-size: 12px; font-weight: 900; "
            "padding: 0 16px; border-radius: 7px;"
        )
        hlay.addWidget(self.lbl_banner_estado)

        hlay.addStretch(1)

        # Info de Usuario y Rol
        self.lbl_header_user = QLabel("USUARIO: Juan Pérez")
        self.lbl_header_user.setStyleSheet("color: #F8FAFC; font-size: 12px; font-weight: 800;")
        hlay.addWidget(self.lbl_header_user)

        self.lbl_header_rol = QLabel("OPERADOR")
        self.lbl_header_rol.setStyleSheet(
            "background-color: #0284C7; color: #FFFFFF; font-size: 10px; font-weight: 800; "
            "padding: 3px 7px; border-radius: 4px;"
        )
        hlay.addWidget(self.lbl_header_rol)

        self.btn_header_admin = QPushButton("Panel Admin")
        self.btn_header_admin.setFixedHeight(32)
        self.btn_header_admin.setStyleSheet(
            "background-color: #7C3AED; color: white; font-size: 11px; font-weight: 800; padding: 0 10px; border-radius: 6px;"
        )
        self.btn_header_admin.clicked.connect(self._open_admin_panel)
        hlay.addWidget(self.btn_header_admin)

        # Selector de idioma [ES | EN]
        lang_box = QFrame()
        lang_box.setStyleSheet("background-color: #1E293B; border-radius: 6px;")
        llay = QHBoxLayout(lang_box)
        llay.setContentsMargins(3, 2, 3, 2)
        llay.setSpacing(2)

        self.btn_lang_es = QPushButton("ES")
        self.btn_lang_es.setFixedSize(32, 26)
        self.btn_lang_es.clicked.connect(lambda: self.cambiar_idioma("ES"))
        llay.addWidget(self.btn_lang_es)

        self.btn_lang_en = QPushButton("EN")
        self.btn_lang_en.setFixedSize(32, 26)
        self.btn_lang_en.clicked.connect(lambda: self.cambiar_idioma("EN"))
        llay.addWidget(self.btn_lang_en)
        hlay.addWidget(lang_box)

        self.lbl_header_timer = QLabel("00:00:00")
        self.lbl_header_timer.setStyleSheet("color: #94A3B8; font-family: Consolas; font-size: 12px; font-weight: 700;")
        hlay.addWidget(self.lbl_header_timer)

        self.btn_header_logout = QPushButton("Cerrar Sesión")
        self.btn_header_logout.setFixedHeight(32)
        self.btn_header_logout.setStyleSheet(
            "background-color: #334155; color: #F8FAFC; font-size: 11px; font-weight: 700; padding: 0 10px; border-radius: 6px;"
        )
        self.btn_header_logout.clicked.connect(self._cerrar_sesion)
        hlay.addWidget(self.btn_header_logout)

        self.btn_header_fs = QPushButton("Pantalla Completa")
        self.btn_header_fs.setFixedHeight(32)
        self.btn_header_fs.setStyleSheet(
            "background-color: #1E293B; color: #E2E8F0; font-size: 11px; font-weight: 700; padding: 0 8px; border-radius: 6px;"
        )
        self.btn_header_fs.clicked.connect(self.alternar_pantalla_completa)
        hlay.addWidget(self.btn_header_fs)

        self.btn_header_min = QPushButton("Minimizar")
        self.btn_header_min.setFixedHeight(32)
        self.btn_header_min.setStyleSheet(
            "background-color: #1E293B; color: #E2E8F0; font-size: 11px; font-weight: 700; padding: 0 8px; border-radius: 6px;"
        )
        self.btn_header_min.clicked.connect(self.showMinimized)
        hlay.addWidget(self.btn_header_min)

        root.addWidget(header)

        # ---------------- CUERPO DE 3 COLUMNAS ----------------
        body = QWidget()
        blay = QHBoxLayout(body)
        blay.setContentsMargins(10, 8, 10, 8)
        blay.setSpacing(10)

        col1 = self._build_col1_process()
        col2 = self._build_col2_fixture()
        col3 = self._build_col3_controls()

        blay.addWidget(col1, 27)
        blay.addWidget(col2, 46)
        blay.addWidget(col3, 27)

        root.addWidget(body, 1)
        return page

    # ---------------- COLUMNA 1: INFORMACIÓN DE PROCESO Y PINTADO ----------------
    def _build_col1_process(self):
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # Card 1: Información de Proceso
        card_proc = QFrame()
        card_proc.setObjectName("IndustrialCard")
        cplay = QVBoxLayout(card_proc)
        cplay.setContentsMargins(12, 10, 12, 10)
        cplay.setSpacing(6)

        self.lbl_title_info_proc = QLabel("INFORMACIÓN DE PROCESO")
        self.lbl_title_info_proc.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 12px; font-weight: 900; letter-spacing: 0.5px;"
        )
        cplay.addWidget(self.lbl_title_info_proc)

        row_rec_hdr = QHBoxLayout()
        self.lbl_rec_activa_title = QLabel("Receta / Jig Activo:")
        self.lbl_rec_activa_title.setStyleSheet("font-size: 11px; font-weight: 700; color: #334155;")
        row_rec_hdr.addWidget(self.lbl_rec_activa_title)
        row_rec_hdr.addStretch(1)

        self.btn_ver_jigs = QPushButton("Ver Jigs")
        self.btn_ver_jigs.setFixedHeight(26)
        self.btn_ver_jigs.setStyleSheet(
            f"background-color: {COLOR_ACCENT_CYAN}; color: white; font-size: 11px; font-weight: 800; padding: 0 8px; border-radius: 5px;"
        )
        self.btn_ver_jigs.clicked.connect(self.abrir_dialogo_seleccion_jig)
        row_rec_hdr.addWidget(self.btn_ver_jigs)
        cplay.addLayout(row_rec_hdr)

        self.combo_receta_op = QComboBox()
        self.combo_receta_op.setView(QListView())
        self.combo_receta_op.setMinimumHeight(32)
        self.combo_receta_op.activated.connect(self._on_operator_recipe_combo_activated)
        cplay.addWidget(self.combo_receta_op)

        # Ficha técnica con miniatura de pieza
        part_card = QFrame()
        part_card.setObjectName("SubSurface")
        pgrid = QHBoxLayout(part_card)
        pgrid.setContentsMargins(8, 6, 8, 6)
        pgrid.setSpacing(8)

        info_left = QVBoxLayout()
        info_left.setSpacing(2)
        self.lbl_proc_pieza = QLabel("Pieza: -")
        self.lbl_proc_pieza.setStyleSheet("font-size: 11px; font-weight: 800; color: #0F172A;")
        self.lbl_proc_modelo = QLabel("Modelo: -")
        self.lbl_proc_modelo.setStyleSheet("font-size: 11px; color: #334155;")
        self.lbl_proc_matriz_dim = QLabel("Matriz Fixture: 3 × 5 (15 PZ)")
        self.lbl_proc_matriz_dim.setStyleSheet("font-size: 11px; font-weight: 700; color: #0284C7;")
        self.lbl_proc_matriz_dist = QLabel("Dist. C-C: X:20.0 | Y:35.0 mm")
        self.lbl_proc_matriz_dist.setStyleSheet("font-size: 10px; color: #475569;")

        info_left.addWidget(self.lbl_proc_pieza)
        info_left.addWidget(self.lbl_proc_modelo)
        info_left.addWidget(self.lbl_proc_matriz_dim)
        info_left.addWidget(self.lbl_proc_matriz_dist)
        pgrid.addLayout(info_left, 1)

        self.lbl_proc_img_pieza = QLabel("[Sin imagen]")
        self.lbl_proc_img_pieza.setFixedSize(86, 68)
        self.lbl_proc_img_pieza.setAlignment(Qt.AlignCenter)
        self.lbl_proc_img_pieza.setStyleSheet(
            "background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 6px; font-size: 9px; color: #64748B;"
        )
        pgrid.addWidget(self.lbl_proc_img_pieza)
        cplay.addWidget(part_card)

        # Contador grande de Piezas Realizadas en el Jig
        lote_box = QFrame()
        lote_box.setObjectName("SubSurface")
        llay = QHBoxLayout(lote_box)
        llay.setContentsMargins(10, 6, 10, 6)
        self.lbl_piezas_realizadas_tit = QLabel("Piezas Realizadas:")
        self.lbl_piezas_realizadas_tit.setStyleSheet("font-size: 12px; font-weight: 800; color: #1E293B;")
        llay.addWidget(self.lbl_piezas_realizadas_tit)
        llay.addStretch(1)
        self.lbl_piezas_realizadas_val = QLabel("00 / 15")
        self.lbl_piezas_realizadas_val.setStyleSheet(
            f"font-family: Consolas; font-size: 20px; font-weight: 900; color: {COLOR_BRAND_PRIMARY};"
        )
        llay.addWidget(self.lbl_piezas_realizadas_val)
        cplay.addWidget(lote_box)

        # Banner de estado de Set 0
        self.lbl_set0_banner = QLabel("CERO CALIBRADO")
        self.lbl_set0_banner.setAlignment(Qt.AlignCenter)
        self.lbl_set0_banner.setFixedHeight(28)
        cplay.addWidget(self.lbl_set0_banner)

        # Tiempo de sesión
        sess_row = QHBoxLayout()
        self.lbl_tiempo_sesion_tit = QLabel("Tiempo Sesión:")
        self.lbl_tiempo_sesion_tit.setStyleSheet("font-size: 11px; font-weight: 700; color: #475569;")
        sess_row.addWidget(self.lbl_tiempo_sesion_tit)
        sess_row.addStretch(1)
        self.lbl_tiempo_sesion_val = QLabel("00:00:00")
        self.lbl_tiempo_sesion_val.setStyleSheet("font-family: Consolas; font-size: 13px; font-weight: 800; color: #0F172A;")
        sess_row.addWidget(self.lbl_tiempo_sesion_val)
        cplay.addLayout(sess_row)

        # Cajas KPI: TOTAL (Automático), OK (Automático) y NO OK (Manual)
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(6)

        kpi_tot_box = QFrame()
        kpi_tot_box.setObjectName("SubSurface")
        ktlay = QVBoxLayout(kpi_tot_box)
        ktlay.setContentsMargins(6, 4, 6, 4)
        ktlay.setSpacing(1)
        self.lbl_kpi_tot_tit = QLabel("TOTAL (AUTO)")
        self.lbl_kpi_tot_tit.setAlignment(Qt.AlignCenter)
        self.lbl_kpi_tot_tit.setStyleSheet("font-size: 9px; font-weight: 800; color: #475569;")
        self.lbl_kpi_tot_val = QLabel("0")
        self.lbl_kpi_tot_val.setAlignment(Qt.AlignCenter)
        self.lbl_kpi_tot_val.setStyleSheet("font-family: Consolas; font-size: 17px; font-weight: 900; color: #0F172A;")
        ktlay.addWidget(self.lbl_kpi_tot_tit)
        ktlay.addWidget(self.lbl_kpi_tot_val)
        kpi_row.addWidget(kpi_tot_box, 1)

        kpi_ok_box = QFrame()
        kpi_ok_box.setObjectName("SubSurface")
        kolay = QVBoxLayout(kpi_ok_box)
        kolay.setContentsMargins(6, 4, 6, 4)
        kolay.setSpacing(1)
        self.lbl_kpi_ok_tit = QLabel("OK (AUTO)")
        self.lbl_kpi_ok_tit.setAlignment(Qt.AlignCenter)
        self.lbl_kpi_ok_tit.setStyleSheet(f"font-size: 9px; font-weight: 800; color: {COLOR_OK};")
        self.lbl_kpi_ok_val = QLabel("0")
        self.lbl_kpi_ok_val.setAlignment(Qt.AlignCenter)
        self.lbl_kpi_ok_val.setStyleSheet(f"font-family: Consolas; font-size: 17px; font-weight: 900; color: {COLOR_OK};")
        kolay.addWidget(self.lbl_kpi_ok_tit)
        kolay.addWidget(self.lbl_kpi_ok_val)
        kpi_row.addWidget(kpi_ok_box, 1)

        kpi_nok_box = QFrame()
        kpi_nok_box.setObjectName("SubSurface")
        knlay = QVBoxLayout(kpi_nok_box)
        knlay.setContentsMargins(6, 4, 6, 4)
        knlay.setSpacing(2)
        self.lbl_kpi_nok_tit = QLabel("NO OK (MANUAL)")
        self.lbl_kpi_nok_tit.setAlignment(Qt.AlignCenter)
        self.lbl_kpi_nok_tit.setStyleSheet(f"font-size: 9px; font-weight: 800; color: {COLOR_ERR};")
        knlay.addWidget(self.lbl_kpi_nok_tit)

        nok_ctrl_row = QHBoxLayout()
        nok_ctrl_row.setContentsMargins(0, 0, 0, 0)
        nok_ctrl_row.setSpacing(4)
        btn_nok_m = QPushButton("-")
        btn_nok_m.setFixedSize(22, 22)
        btn_nok_m.setStyleSheet(
            f"background-color: #FEE2E2; color: {COLOR_ERR}; font-size: 12px; font-weight: 900; padding: 0; border-radius: 4px;"
        )
        btn_nok_m.clicked.connect(lambda: self.ajustar_nok_manual(-1))
        self.lbl_kpi_nok_val = QLabel("0")
        self.lbl_kpi_nok_val.setAlignment(Qt.AlignCenter)
        self.lbl_kpi_nok_val.setStyleSheet(f"font-family: Consolas; font-size: 16px; font-weight: 900; color: {COLOR_ERR};")
        btn_nok_p = QPushButton("+")
        btn_nok_p.setFixedSize(22, 22)
        btn_nok_p.setStyleSheet(
            f"background-color: #FEE2E2; color: {COLOR_ERR}; font-size: 12px; font-weight: 900; padding: 0; border-radius: 4px;"
        )
        btn_nok_p.clicked.connect(lambda: self.ajustar_nok_manual(1))
        nok_ctrl_row.addWidget(btn_nok_m)
        nok_ctrl_row.addWidget(self.lbl_kpi_nok_val, 1)
        nok_ctrl_row.addWidget(btn_nok_p)
        knlay.addLayout(nok_ctrl_row)
        kpi_row.addWidget(kpi_nok_box, 1)

        cplay.addLayout(kpi_row)

        self.btn_reg_fin_hora = QPushButton("Registrar Fin de Hora")
        self.btn_reg_fin_hora.setMinimumHeight(34)
        self.btn_reg_fin_hora.setStyleSheet(
            f"background-color: {COLOR_BRAND_PRIMARY}; color: white; font-size: 12px; font-weight: 800; border-radius: 7px;"
        )
        self.btn_reg_fin_hora.clicked.connect(self.abrir_dialogo_fin_de_hora)
        cplay.addWidget(self.btn_reg_fin_hora)

        lay.addWidget(card_proc, 1)

        # Card 2: Control Principal de Pintado
        card_paint = QFrame()
        card_paint.setObjectName("IndustrialCard")
        cplay2 = QVBoxLayout(card_paint)
        cplay2.setContentsMargins(12, 10, 12, 10)
        cplay2.setSpacing(6)

        self.lbl_title_ctrl_pintado = QLabel("CONTROL PRINCIPAL DE PINTADO")
        self.lbl_title_ctrl_pintado.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 12px; font-weight: 900;"
        )
        cplay2.addWidget(self.lbl_title_ctrl_pintado)

        self.btn_pintar_main = QPushButton("INICIO DE PINTADO [OFF]")
        self.btn_pintar_main.setMinimumHeight(56)
        self.btn_pintar_main.clicked.connect(self.alternar_pintar)
        cplay2.addWidget(self.btn_pintar_main)

        lay.addWidget(card_paint, 0)
        return col


    # ---------------- COLUMNA 2: ESTADO DEL FIXTURE Y DIAGNÓSTICO PLC ----------------
    def _build_col2_fixture(self):
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # Card Central Superior: Estado del Fixture
        card_fix = QFrame()
        card_fix.setObjectName("IndustrialCard")
        flay = QVBoxLayout(card_fix)
        flay.setContentsMargins(12, 10, 12, 10)
        flay.setSpacing(6)

        # Encabezado de Fixture
        hdr_row = QHBoxLayout()
        self.lbl_title_fixture = QLabel("ESTADO DEL FIXTURE (MATRIZ 3 × 5)")
        self.lbl_title_fixture.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 13px; font-weight: 900;"
        )
        hdr_row.addWidget(self.lbl_title_fixture)
        hdr_row.addStretch(1)

        self.btn_recetario_admin_shortcut = QPushButton("Recetario Admin")
        self.btn_recetario_admin_shortcut.setFixedHeight(26)
        self.btn_recetario_admin_shortcut.setStyleSheet(
            "background-color: #E0F2FE; color: #0369A1; font-size: 11px; font-weight: 800; padding: 0 8px; border-radius: 5px;"
        )
        self.btn_recetario_admin_shortcut.clicked.connect(self._open_admin_panel)
        hdr_row.addWidget(self.btn_recetario_admin_shortcut)
        flay.addLayout(hdr_row)

        # Barra de badges de Matriz, Distancias, Patrón Vertical/Horizontal y Coords
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)

        self.lbl_matriz_dim_badge = QLabel("MATRIZ: 3 × 5 (15 PZ/JIG)")
        self.lbl_matriz_dim_badge.setStyleSheet(
            "background-color: #F1F5F9; color: #0F172A; font-size: 10px; font-weight: 800; padding: 4px 7px; border-radius: 5px;"
        )
        badge_row.addWidget(self.lbl_matriz_dim_badge)

        self.lbl_matriz_dist_badge = QLabel("X: 20.0 mm | Y: 35.0 mm | Diag: 40.3 mm")
        self.lbl_matriz_dist_badge.setStyleSheet(
            "background-color: #F1F5F9; color: #334155; font-size: 10px; font-weight: 700; padding: 4px 7px; border-radius: 5px;"
        )
        badge_row.addWidget(self.lbl_matriz_dist_badge, 1)

        self.btn_patron_toggle = QPushButton("Patrón: Vertical")
        self.btn_patron_toggle.setFixedHeight(26)
        self.btn_patron_toggle.setStyleSheet(
            f"background-color: {COLOR_BRAND_PRIMARY}; color: white; font-size: 10px; font-weight: 800; padding: 0 8px; border-radius: 5px;"
        )
        self.btn_patron_toggle.clicked.connect(self.alternar_patron_pintado)
        badge_row.addWidget(self.btn_patron_toggle)

        self.btn_coords_toggle = QPushButton("Coords: ON")
        self.btn_coords_toggle.setFixedHeight(26)
        self.btn_coords_toggle.setStyleSheet(
            "background-color: #0284C7; color: white; font-size: 10px; font-weight: 800; padding: 0 8px; border-radius: 5px;"
        )
        self.btn_coords_toggle.clicked.connect(self.alternar_mostrar_coords)
        badge_row.addWidget(self.btn_coords_toggle)

        flay.addLayout(badge_row)

        # Barra de Telemetría PLC en vivo + Botón Probar Movimiento
        tele_bar = QFrame()
        tele_bar.setObjectName("SubSurface")
        tlay = QHBoxLayout(tele_bar)
        tlay.setContentsMargins(8, 4, 8, 4)
        tlay.setSpacing(8)

        self.lbl_tele_destino = QLabel("DESTINO P01: X=0.0 Y=0.0")
        self.lbl_tele_destino.setStyleSheet("font-family: Consolas; font-size: 11px; font-weight: 800; color: #0369A1;")
        tlay.addWidget(self.lbl_tele_destino)

        self.lbl_tele_plcrx = QLabel("PLC RX: X=0.0 Y=0.0")
        self.lbl_tele_plcrx.setStyleSheet("font-family: Consolas; font-size: 11px; font-weight: 800; color: #0F172A;")
        tlay.addWidget(self.lbl_tele_plcrx)

        self.lbl_tele_conn = QLabel("○ SIMULACIÓN")
        self.lbl_tele_conn.setStyleSheet("font-size: 10px; font-weight: 900; color: #D97706;")
        tlay.addWidget(self.lbl_tele_conn)

        tlay.addStretch(1)

        self.btn_probar_mov = QPushButton("Probar Movimiento")
        self.btn_probar_mov.setFixedHeight(25)
        self.btn_probar_mov.setStyleSheet(
            "background-color: #0F172A; color: #38BDF8; font-size: 10px; font-weight: 800; padding: 0 8px; border-radius: 5px;"
        )
        self.btn_probar_mov.clicked.connect(self.probar_movimiento_pieza_actual)
        tlay.addWidget(self.btn_probar_mov)

        flay.addLayout(tlay) if False else flay.addWidget(tele_bar)

        # Barra de progreso del lote actual
        self.prog_ciclo = QProgressBar()
        self.prog_ciclo.setRange(0, 100)
        self.prog_ciclo.setValue(0)
        self.prog_ciclo.setFixedHeight(14)
        flay.addWidget(self.prog_ciclo)

        # Lienzo vectorial de 60 FPS
        self.fixture_canvas = FixtureCanvasWidget(self)
        self.fixture_canvas.cavity_clicked.connect(self._on_fixture_cavity_clicked)
        flay.addWidget(self.fixture_canvas, 1)

        # Leyenda inferior
        leg_row = QHBoxLayout()
        leg_row.setSpacing(10)
        self.lbl_ley_pend = QLabel("● Pendiente")
        self.lbl_ley_pend.setStyleSheet("color: #64748B; font-size: 10px; font-weight: 700;")
        self.lbl_ley_proc = QLabel("● En Proceso (Activa)")
        self.lbl_ley_proc.setStyleSheet("color: #0284C7; font-size: 10px; font-weight: 700;")
        self.lbl_ley_ok = QLabel("● Completada OK")
        self.lbl_ley_ok.setStyleSheet(f"color: {COLOR_OK}; font-size: 10px; font-weight: 700;")
        self.lbl_ley_nok = QLabel("● Fallo / NOK")
        self.lbl_ley_nok.setStyleSheet(f"color: {COLOR_ERR}; font-size: 10px; font-weight: 700;")
        self.lbl_ley_click = QLabel("Modo Manual: Clic en cavidad envía coordenada")
        self.lbl_ley_click.setStyleSheet("color: #475569; font-size: 10px; font-style: italic;")

        leg_row.addWidget(self.lbl_ley_pend)
        leg_row.addWidget(self.lbl_ley_proc)
        leg_row.addWidget(self.lbl_ley_ok)
        leg_row.addWidget(self.lbl_ley_nok)
        leg_row.addStretch(1)
        leg_row.addWidget(self.lbl_ley_click)
        flay.addLayout(leg_row)

        lay.addWidget(card_fix, 1)

        # Card Central Inferior: Diagnóstico & Señales del PLC WAGO (11 indicadores)
        card_diag = QFrame()
        card_diag.setObjectName("IndustrialCard")
        dlay = QVBoxLayout(card_diag)
        dlay.setContentsMargins(12, 8, 12, 8)
        dlay.setSpacing(6)

        self.lbl_title_diag = QLabel("DIAGNÓSTICO & SEÑALES DEL PLC WAGO")
        self.lbl_title_diag.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 11px; font-weight: 900;"
        )
        dlay.addWidget(self.lbl_title_diag)

        leds_grid = QGridLayout()
        leds_grid.setSpacing(5)
        signal_defs = [
            ("PINTADO", "ok"), ("M_ENE", "ok"), ("M_SET", "ok"),
            ("LISTO", "ok"), ("NO_LISTO", "warn"), ("ALARM_X", "err"),
            ("ALARM_Y", "err"), ("PE", "err"), ("SENSOR_X", "info"),
            ("SENSOR_Y", "info"), ("TAMPO", "info"),
        ]
        for idx, (sig_name, kind) in enumerate(signal_defs):
            r = idx // 6
            c = idx % 6
            pill = QLabel(f"○ {sig_name}")
            pill.setAlignment(Qt.AlignCenter)
            pill.setFixedHeight(24)
            pill.setStyleSheet(
                "background-color: #F1F5F9; color: #64748B; font-size: 10px; font-weight: 800; "
                "border: 1px solid #CBD5E1; border-radius: 5px; padding: 0 4px;"
            )
            self.led_widgets[sig_name] = (pill, kind)
            leds_grid.addWidget(pill, r, c)

        dlay.addLayout(leds_grid)
        lay.addWidget(card_diag, 0)
        return col

    # ---------------- COLUMNA 3: OPERACIÓN & JOG (PLEGABLE) Y MANDOS ----------------
    def _build_col3_controls(self):
        col = QWidget()
        self.col3_layout = QVBoxLayout(col)
        self.col3_layout.setContentsMargins(0, 0, 0, 0)
        self.col3_layout.setSpacing(8)

        # Card Superior: Operación & JOG (con plegado suave a 60 FPS)
        self.card_jog = QFrame()
        self.card_jog.setObjectName("IndustrialCard")
        jlay = QVBoxLayout(self.card_jog)
        jlay.setContentsMargins(12, 10, 12, 10)
        jlay.setSpacing(6)

        jhdr = QHBoxLayout()
        self.lbl_title_op_jog = QLabel("OPERACIÓN & JOG")
        self.lbl_title_op_jog.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 12px; font-weight: 900;"
        )
        jhdr.addWidget(self.lbl_title_op_jog)
        jhdr.addStretch(1)

        self.btn_fold_jog = QPushButton("▲ Plegar")
        self.btn_fold_jog.setFixedHeight(24)
        self.btn_fold_jog.setStyleSheet(
            "background-color: #E2E8F0; color: #1E293B; font-size: 10px; font-weight: 800; padding: 0 8px; border-radius: 5px;"
        )
        self.btn_fold_jog.clicked.connect(self.alternar_panel_jog)
        jhdr.addWidget(self.btn_fold_jog)
        jlay.addLayout(jhdr)

        self.jog_collapsible = AnimatedCollapsibleBox(self.card_jog, expanded=True)
        c_lay = self.jog_collapsible.content_layout

        # Botón MODO AUTO / MANUAL + Velocidad
        mode_vel_row = QHBoxLayout()
        mode_vel_row.setSpacing(6)
        self.btn_modo = QPushButton("MODO: AUTO")
        self.btn_modo.setMinimumHeight(34)
        self.btn_modo.clicked.connect(self.alternar_modo)
        mode_vel_row.addWidget(self.btn_modo, 1)

        self.lbl_vel_tit = QLabel("Velocidad:")
        self.lbl_vel_tit.setStyleSheet("font-size: 11px; font-weight: 700;")
        mode_vel_row.addWidget(self.lbl_vel_tit)

        self.inp_velocidad = QLineEdit(str(int(self.velocidad_actual)))
        self.inp_velocidad.setFixedWidth(52)
        self.inp_velocidad.setAlignment(Qt.AlignCenter)
        self.inp_velocidad.editingFinished.connect(self._on_velocidad_edited)
        mode_vel_row.addWidget(self.inp_velocidad)

        lbl_mms = QLabel("mm/s")
        lbl_mms.setStyleSheet("font-size: 10px; color: #475569; font-weight: 700;")
        mode_vel_row.addWidget(lbl_mms)
        c_lay.addLayout(mode_vel_row)

        # Sub-caja JOG Manual + Paso JOG + Presets
        jog_sub = QFrame()
        jog_sub.setObjectName("SubSurface")
        jslay = QVBoxLayout(jog_sub)
        jslay.setContentsMargins(8, 6, 8, 6)
        jslay.setSpacing(5)

        paso_row = QHBoxLayout()
        paso_row.setSpacing(4)
        self.lbl_paso_jog_tit = QLabel("Paso JOG:")
        self.lbl_paso_jog_tit.setStyleSheet("font-size: 11px; font-weight: 800;")
        paso_row.addWidget(self.lbl_paso_jog_tit)

        btn_p_minus = QPushButton("-")
        btn_p_minus.setFixedSize(26, 26)
        btn_p_minus.clicked.connect(lambda: self.ajustar_paso_jog(-0.5))
        paso_row.addWidget(btn_p_minus)

        self.inp_paso_jog = QLineEdit(f"{self.paso_jog:.1f}")
        self.inp_paso_jog.setFixedWidth(48)
        self.inp_paso_jog.setAlignment(Qt.AlignCenter)
        self.inp_paso_jog.editingFinished.connect(self._on_paso_jog_edited)
        paso_row.addWidget(self.inp_paso_jog)

        btn_p_plus = QPushButton("+")
        btn_p_plus.setFixedSize(26, 26)
        btn_p_plus.clicked.connect(lambda: self.ajustar_paso_jog(0.5))
        paso_row.addWidget(btn_p_plus)

        btn_numpad = QPushButton("NUM")
        btn_numpad.setFixedSize(38, 26)
        btn_numpad.setStyleSheet("background-color: #E0F2FE; color: #0369A1; font-size: 10px; font-weight: 800; padding: 0;")
        btn_numpad.clicked.connect(self.abrir_teclado_numerico_paso)
        paso_row.addWidget(btn_numpad)

        for pv in (0.5, 1.0, 2.0, 5.0):
            bp = QPushButton(f"{pv:g}")
            bp.setFixedSize(28, 24)
            bp.setStyleSheet("background-color: #E2E8F0; color: #0F172A; font-size: 10px; font-weight: 800; padding: 0;")
            bp.clicked.connect(lambda _, val=pv: self.set_paso_jog_directo(val))
            paso_row.addWidget(bp)

        jslay.addLayout(paso_row)

        # Coordenadas + Cruceta Direccional (D-Pad)
        dpad_row = QHBoxLayout()
        dpad_row.setSpacing(8)

        coord_box = QFrame()
        coord_box.setObjectName("DarkControlBox")
        cblay = QVBoxLayout(coord_box)
        cblay.setContentsMargins(8, 6, 8, 6)
        cblay.setSpacing(4)

        lbl_c_hdr = QLabel("COORD (X:0-88 | Y:0-200)")
        lbl_c_hdr.setStyleSheet("color: #94A3B8; font-size: 9px; font-weight: 800;")
        cblay.addWidget(lbl_c_hdr)

        rx = QHBoxLayout()
        lbl_x = QLabel("X:")
        lbl_x.setStyleSheet("color: #38BDF8; font-family: Consolas; font-size: 13px; font-weight: 900;")
        self.inp_coord_x = QLineEdit("0.0")
        self.inp_coord_x.setAlignment(Qt.AlignCenter)
        self.inp_coord_x.setFixedHeight(26)
        self.inp_coord_x.editingFinished.connect(self.enviar_coordenadas_manuales)
        rx.addWidget(lbl_x)
        rx.addWidget(self.inp_coord_x)
        cblay.addLayout(rx)

        ry = QHBoxLayout()
        lbl_y = QLabel("Y:")
        lbl_y.setStyleSheet("color: #38BDF8; font-family: Consolas; font-size: 13px; font-weight: 900;")
        self.inp_coord_y = QLineEdit("0.0")
        self.inp_coord_y.setAlignment(Qt.AlignCenter)
        self.inp_coord_y.setFixedHeight(26)
        self.inp_coord_y.editingFinished.connect(self.enviar_coordenadas_manuales)
        ry.addWidget(lbl_y)
        ry.addWidget(self.inp_coord_y)
        cblay.addLayout(ry)

        dpad_row.addWidget(coord_box, 1)

        # Cruceta D-Pad
        dpad_grid = QGridLayout()
        dpad_grid.setSpacing(3)
        self.btn_jog_up = QPushButton("▲")
        self.btn_jog_down = QPushButton("▼")
        self.btn_jog_left = QPushButton("◄")
        self.btn_jog_right = QPushButton("►")
        for b in (self.btn_jog_up, self.btn_jog_down, self.btn_jog_left, self.btn_jog_right):
            b.setFixedSize(36, 32)
            b.setStyleSheet(
                f"QPushButton {{ background-color: {COLOR_BRAND_PRIMARY}; color: white; font-size: 13px; font-weight: 900; border-radius: 6px; }}"
                "QPushButton:disabled { background-color: #CBD5E1; color: #64748B; }"
            )
        self.btn_jog_up.clicked.connect(lambda: self.mover_cruzeta("Y", 1))
        self.btn_jog_down.clicked.connect(lambda: self.mover_cruzeta("Y", -1))
        self.btn_jog_left.clicked.connect(lambda: self.mover_cruzeta("X", -1))
        self.btn_jog_right.clicked.connect(lambda: self.mover_cruzeta("X", 1))

        dpad_grid.addWidget(self.btn_jog_up, 0, 1)
        dpad_grid.addWidget(self.btn_jog_left, 1, 0)
        dpad_grid.addWidget(self.btn_jog_right, 1, 2)
        dpad_grid.addWidget(self.btn_jog_down, 2, 1)
        dpad_row.addLayout(dpad_grid)

        jslay.addLayout(dpad_row)
        c_lay.addWidget(jog_sub)

        # Botones Home y Activar Tampo Manual
        hm_row = QHBoxLayout()
        hm_row.setSpacing(6)
        self.btn_home = QPushButton("Ir a Home")
        self.btn_home.setMinimumHeight(34)
        self.btn_home.setStyleSheet(
            "background-color: #0284C7; color: white; font-size: 12px; font-weight: 800; border-radius: 6px;"
        )
        self.btn_home.clicked.connect(self.ejecutar_home_general)
        hm_row.addWidget(self.btn_home)

        self.btn_tampo_manual = QPushButton("Activar Tampo")
        self.btn_tampo_manual.setMinimumHeight(34)
        self.btn_tampo_manual.setStyleSheet(
            "background-color: #475569; color: white; font-size: 12px; font-weight: 800; border-radius: 6px;"
        )
        self.btn_tampo_manual.clicked.connect(self.activar_tampo_manual)
        hm_row.addWidget(self.btn_tampo_manual)
        c_lay.addLayout(hm_row)

        # Sección colapsable de Calibración (Set 0 / Set Home)
        self.btn_toggle_cal = QPushButton("▼ Desplegar Calibración (Set 0 / Set Home)")
        self.btn_toggle_cal.setFixedHeight(26)
        self.btn_toggle_cal.setStyleSheet(
            "background-color: #F1F5F9; color: #334155; font-size: 10px; font-weight: 800; border: 1px solid #CBD5E1; border-radius: 5px;"
        )
        self.btn_toggle_cal.clicked.connect(self.alternar_seccion_calibracion)
        c_lay.addWidget(self.btn_toggle_cal)

        self.cal_collapsible = AnimatedCollapsibleBox(self.card_jog, expanded=False)
        cal_lay = QHBoxLayout()
        cal_lay.setSpacing(6)
        self.btn_set_0 = QPushButton("Set 0")
        self.btn_set_0.setMinimumHeight(32)
        self.btn_set_0.setStyleSheet(
            f"background-color: {COLOR_WARN}; color: white; font-size: 11px; font-weight: 800; border-radius: 6px;"
        )
        self.btn_set_0.clicked.connect(self.guardar_posicion_como_cero)
        cal_lay.addWidget(self.btn_set_0)

        self.btn_set_home = QPushButton("Set Home")
        self.btn_set_home.setMinimumHeight(32)
        self.btn_set_home.setStyleSheet(
            "background-color: #475569; color: white; font-size: 11px; font-weight: 800; border-radius: 6px;"
        )
        self.btn_set_home.clicked.connect(self.guardar_posicion_como_home)
        cal_lay.addWidget(self.btn_set_home)
        self.cal_collapsible.content_layout.addLayout(cal_lay)
        c_lay.addWidget(self.cal_collapsible)

        # Caja de Cero Registrado
        cero_box = QFrame()
        cero_box.setObjectName("SubSurface")
        zlay = QHBoxLayout(cero_box)
        zlay.setContentsMargins(8, 4, 8, 4)
        self.lbl_cero_reg_tit = QLabel("CERO REGISTRADO:")
        self.lbl_cero_reg_tit.setStyleSheet("font-size: 10px; font-weight: 800; color: #475569;")
        zlay.addWidget(self.lbl_cero_reg_tit)
        zlay.addStretch(1)
        self.lbl_cero_reg_val = QLabel("X: 0.00 | Y: 0.00")
        self.lbl_cero_reg_val.setStyleSheet("font-family: Consolas; font-size: 11px; font-weight: 900; color: #0F172A;")
        zlay.addWidget(self.lbl_cero_reg_val)
        c_lay.addWidget(cero_box)

        jlay.addWidget(self.jog_collapsible)
        self.col3_layout.addWidget(self.card_jog, 0)

        # Card Inferior: MANDOS DE CONTROL (INICIO, RESET, PARO)
        self.card_mandos = QFrame()
        self.card_mandos.setObjectName("IndustrialCard")
        mlay = QVBoxLayout(self.card_mandos)
        mlay.setContentsMargins(12, 10, 12, 10)
        mlay.setSpacing(6)

        self.lbl_title_mandos = QLabel("MANDOS DE CONTROL")
        self.lbl_title_mandos.setStyleSheet(
            f"color: {COLOR_BRAND_PRIMARY}; font-size: 12px; font-weight: 900;"
        )
        mlay.addWidget(self.lbl_title_mandos)

        ir_row = QHBoxLayout()
        ir_row.setSpacing(8)
        self.btn_inicio = QPushButton("INICIO")
        self.btn_inicio.setMinimumHeight(46)
        self.btn_inicio.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.btn_inicio.setStyleSheet(
            f"background-color: {COLOR_OK}; color: white; font-size: 14px; font-weight: 900; border-radius: 8px;"
        )
        self.btn_inicio.clicked.connect(self.iniciar_ciclo_general)
        ir_row.addWidget(self.btn_inicio)

        self.btn_reset = QPushButton("RESET")
        self.btn_reset.setMinimumHeight(46)
        self.btn_reset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.btn_reset.setStyleSheet(
            f"background-color: {COLOR_WARN}; color: white; font-size: 14px; font-weight: 900; border-radius: 8px;"
        )
        self.btn_reset.clicked.connect(self.ejecutar_reset_general)
        ir_row.addWidget(self.btn_reset)
        mlay.addLayout(ir_row, 1)

        self.btn_paro = QPushButton("PARO")
        self.btn_paro.setMinimumHeight(54)
        self.btn_paro.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.btn_paro.setStyleSheet(
            f"background-color: {COLOR_ERR}; color: white; font-size: 16px; font-weight: 900; border-radius: 8px;"
        )
        self.btn_paro.clicked.connect(self.alternar_stop)
        mlay.addWidget(self.btn_paro, 1)

        self.col3_layout.addWidget(self.card_mandos, 1)
        return col


    # ==========================================================================
    # LÓGICA DE MATRIZ, INTERLOCKS, IDIOMA, CICLO DE PINTADO Y MANDOS
    # ==========================================================================
    def actualizar_coordenadas_matriz(self):
        """
        Calcula las coordenadas de cada cavidad de la matriz (patrón vertical en
        serpiente u horizontal) llevando cada cavidad al Punto 0 (SET 0) dentro
        de los límites físicos X:[0..88] mm, Y:[0..200] mm.
        """
        self.matriz_coordenadas = []
        x0 = self.cero_guardado_x
        y0 = self.cero_guardado_y
        dx = abs(self.matriz_delta_x)
        dy = abs(self.matriz_delta_y)

        recorrido_x = (self.matriz_cols - 1) * dx
        if (x0 - recorrido_x) < 0.0 and (x0 + recorrido_x) <= COORD_X_MAX:
            dir_x = 1.0
        elif (x0 + recorrido_x) > COORD_X_MAX and (x0 - recorrido_x) >= 0.0:
            dir_x = -1.0
        elif x0 < (COORD_X_MAX / 2.0):
            dir_x = 1.0
        else:
            dir_x = -1.0

        recorrido_y = (self.matriz_filas - 1) * dy
        if (y0 - recorrido_y) < 0.0 and (y0 + recorrido_y) <= COORD_Y_MAX:
            dir_y = 1.0
        elif (y0 + recorrido_y) > COORD_Y_MAX and (y0 - recorrido_y) >= 0.0:
            dir_y = -1.0
        elif y0 < 100.0:
            dir_y = 1.0
        else:
            dir_y = -1.0

        if self.patron_pintado == "vertical":
            for c in range(self.matriz_cols):
                filas_iter = range(self.matriz_filas) if (c % 2 == 0) else reversed(range(self.matriz_filas))
                for r in filas_iter:
                    x = limitar_coord_x(x0 + dir_x * (c * dx))
                    y = limitar_coord_y(y0 + dir_y * (r * dy))
                    self.matriz_coordenadas.append((round(x, 2), round(y, 2)))
        else:
            for r in range(self.matriz_filas):
                for c in range(self.matriz_cols):
                    x = limitar_coord_x(x0 + dir_x * (c * dx))
                    y = limitar_coord_y(y0 + dir_y * (r * dy))
                    self.matriz_coordenadas.append((round(x, 2), round(y, 2)))

        total_cells = self.matriz_filas * self.matriz_cols
        if len(self.estados_piezas) != total_cells:
            self.estados_piezas = ["pendiente"] * total_cells

    def cargar_receta_por_nombre(self, nombre, silencioso=False):
        recetas = self.config.get("recetas", [])
        rec = next((r for r in recetas if r.get("nombre") == nombre), None)
        if not rec and recetas:
            rec = recetas[0]
            nombre = rec.get("nombre", "")
        if not rec:
            return

        self.config["receta_activa"] = nombre
        self.matriz_filas = max(1, min(5, int(rec.get("filas", 3))))
        self.matriz_cols = max(1, min(5, int(rec.get("columnas", 5))))
        self.piezas_por_jig_actual = obtener_piezas_por_jig_receta(rec)
        self.matriz_delta_x = float(rec.get("espacio_x", 20.0))
        self.matriz_delta_y = float(rec.get("espacio_y", 35.0))
        self.matriz_dist_diag = round((self.matriz_delta_x ** 2 + self.matriz_delta_y ** 2) ** 0.5, 2)

        self.cero_guardado_x = limitar_coord_x(rec.get("cero_x", 0.0))
        self.cero_guardado_y = limitar_coord_y(rec.get("cero_y", 0.0))
        self.set_0_confirmado = bool(rec.get("set_0_listo", True))
        self.velocidad_actual = float(rec.get("vel_x", 100.0))

        self.indice_matriz_actual = 0
        self.pasada_actual_pieza = 1
        self.piezas_realizadas_lote = 0
        self.estados_piezas = ["pendiente"] * (self.matriz_filas * self.matriz_cols)

        self.actualizar_coordenadas_matriz()

        # Refrescar ComboBox de recetas sin disparar evento recursivo
        self.combo_receta_op.blockSignals(True)
        self.combo_receta_op.clear()
        for r in recetas:
            self.combo_receta_op.addItem(r.get("nombre", ""))
        self.combo_receta_op.setCurrentText(nombre)
        self.combo_receta_op.blockSignals(False)

        # Miniatura de pieza en ficha técnica
        img_name = rec.get("imagen", "")
        ruta_img = buscar_recurso(img_name)
        if ruta_img and os.path.exists(ruta_img):
            pix = QPixmap(ruta_img).scaled(82, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_proc_img_pieza.setPixmap(pix)
            self.lbl_proc_img_pieza.setText("")
        else:
            self.lbl_proc_img_pieza.setPixmap(QPixmap())
            self.lbl_proc_img_pieza.setText("[Sin imagen]")

        self.inp_velocidad.setText(str(int(self.velocidad_actual)))
        self._refresh_all_operator_ui()
        guardar_configuracion_disco(self.config)

        if not silencioso:
            log_debug_hmi(f"Receta cargada: {nombre} ({self.matriz_filas}x{self.matriz_cols}, {self.piezas_por_jig_actual} pz)")
            self._highlight_calibration_buttons()

    def _highlight_calibration_buttons(self):
        self.btn_home.setStyleSheet(
            "background-color: #0284C7; color: white; font-size: 12px; font-weight: 900; border: 2px solid #38BDF8; border-radius: 6px;"
        )
        QTimer.singleShot(
            4000,
            lambda: self.btn_home.setStyleSheet(
                "background-color: #0284C7; color: white; font-size: 12px; font-weight: 800; border-radius: 6px;"
            ),
        )

    def _on_operator_recipe_combo_activated(self, index):
        nom = self.combo_receta_op.itemText(index)
        if self.estado_pintando:
            self.overlay.show_message(
                "Ciclo Activo",
                "Detenga el ciclo de pintado antes de cambiar de Jig o Receta.",
                "warn",
            )
            self.combo_receta_op.setCurrentText(self.config.get("receta_activa", ""))
            return
        self.cargar_receta_por_nombre(nom, silencioso=False)

    def abrir_dialogo_seleccion_jig(self):
        if self.estado_pintando:
            self.overlay.show_message(
                "Ciclo Activo",
                "Detenga el ciclo de pintado antes de cambiar de Jig o Receta.",
                "warn",
            )
            return
        self.overlay.show_jig_selector(
            self.config.get("recetas", []),
            self.config.get("receta_activa", ""),
            lambda sel: self.cargar_receta_por_nombre(sel, silencioso=False),
        )

    def cambiar_idioma(self, nuevo_idioma):
        if nuevo_idioma not in ("ES", "EN"):
            return
        self.idioma = nuevo_idioma
        self.config["idioma"] = nuevo_idioma
        guardar_configuracion_disco(self.config)

        # Estilo activo en botones [ES | EN]
        if self.idioma == "ES":
            self.btn_lang_es.setStyleSheet("background-color: #0284C7; color: white; font-size: 10px; font-weight: 900; border-radius: 4px; padding: 0;")
            self.btn_lang_en.setStyleSheet("background-color: transparent; color: #94A3B8; font-size: 10px; font-weight: 700; border-radius: 4px; padding: 0;")
        else:
            self.btn_lang_en.setStyleSheet("background-color: #0284C7; color: white; font-size: 10px; font-weight: 900; border-radius: 4px; padding: 0;")
            self.btn_lang_es.setStyleSheet("background-color: transparent; color: #94A3B8; font-size: 10px; font-weight: 700; border-radius: 4px; padding: 0;")

        self.btn_header_admin.setText(self._tr("panel_admin"))
        self.btn_header_logout.setText(self._tr("cerrar_sesion"))
        self.btn_header_fs.setText(self._tr("salir_pantalla") if self.isFullScreen() else self._tr("pantalla"))
        self.btn_header_min.setText(self._tr("minimizar"))

        self.lbl_title_info_proc.setText(self._tr("info_proceso"))
        self.lbl_rec_activa_title.setText(self._tr("receta_activa"))
        self.btn_ver_jigs.setText(self._tr("ver_jigs"))
        self.lbl_piezas_realizadas_tit.setText(self._tr("piezas_realizadas") + ":")
        self.lbl_tiempo_sesion_tit.setText(self._tr("tiempo_sesion"))
        self.lbl_kpi_tot_tit.setText("TOTAL (AUTO)")
        self.lbl_kpi_ok_tit.setText("OK (AUTO)")
        if hasattr(self, "lbl_kpi_nok_tit"):
            self.lbl_kpi_nok_tit.setText("NO OK (MANUAL)")
        self.btn_reg_fin_hora.setText(self._tr("reg_fin_hora"))
        self.lbl_title_ctrl_pintado.setText(self._tr("ctrl_pintado"))

        self.btn_recetario_admin_shortcut.setText(self._tr("recetario_admin"))
        self.btn_probar_mov.setText(self._tr("probar_mov"))
        self.lbl_ley_pend.setText(self._tr("ley_pend"))
        self.lbl_ley_proc.setText(self._tr("ley_proc"))
        self.lbl_ley_ok.setText(self._tr("ley_ok"))
        self.lbl_ley_nok.setText(self._tr("ley_nok"))
        self.lbl_ley_click.setText(self._tr("ley_click"))
        self.lbl_title_diag.setText(self._tr("diag_plc"))

        self.lbl_title_op_jog.setText(self._tr("op_jog"))
        self.btn_fold_jog.setText(self._tr("plegar") if self.jog_collapsible.is_expanded else self._tr("desplegar"))
        self.lbl_vel_tit.setText(self._tr("velocidad"))
        self.lbl_paso_jog_tit.setText(self._tr("paso_jog"))
        self.btn_home.setText(self._tr("home"))
        if not self.tampo_manual_activo:
            self.btn_tampo_manual.setText(self._tr("activar_tampo"))
        self.btn_toggle_cal.setText(
            self._tr("calibracion_hide") if self.cal_collapsible.is_expanded else self._tr("calibracion_show")
        )
        self.btn_set_0.setText(self._tr("set_0"))
        self.btn_set_home.setText(self._tr("set_home"))
        self.lbl_cero_reg_tit.setText(self._tr("cero_reg"))
        self.lbl_title_mandos.setText(self._tr("mandos_ctrl"))
        self.btn_inicio.setText(self._tr("inicio"))
        self.btn_reset.setText(self._tr("reset"))

        self._refresh_all_operator_ui()

    def _refresh_all_operator_ui(self):
        rec_nom = self.config.get("receta_activa", "")
        rec = next((r for r in self.config.get("recetas", []) if r.get("nombre") == rec_nom), {})
        pieza_txt = rec.get("pieza", "-")
        modelo_txt = rec.get("modelo", "-")
        img_txt = rec.get("imagen", "")

        self.lbl_proc_pieza.setText(f"{self._tr('pieza')} {pieza_txt}")
        self.lbl_proc_modelo.setText(f"{self._tr('modelo')} {modelo_txt}")
        self.lbl_proc_matriz_dim.setText(
            f"{self._tr('matriz_fixture')} {self.matriz_filas} × {self.matriz_cols} ({self.piezas_por_jig_actual} PZ/Jig)"
        )
        self.lbl_proc_matriz_dist.setText(
            f"{self._tr('dist_cc')} X:{self.matriz_delta_x:.1f} mm | Y:{self.matriz_delta_y:.1f} mm"
        )
        self.lbl_piezas_realizadas_val.setText(
            f"{self.piezas_realizadas_lote:02d} / {self.piezas_por_jig_actual:02d}"
        )

        # Progreso del lote
        pct = int(round((self.piezas_realizadas_lote / max(1, self.piezas_por_jig_actual)) * 100.0))
        self.prog_ciclo.setValue(max(0, min(100, pct)))

        # Banner Set 0
        if self.set_0_confirmado:
            self.lbl_set0_banner.setText(f"{self._tr('cero_listo')} (X:{self.cero_guardado_x:.1f}, Y:{self.cero_guardado_y:.1f})")
            self.lbl_set0_banner.setStyleSheet(
                "background-color: #ECFDF5; color: #059669; font-size: 11px; font-weight: 800; border: 1px solid #10B981; border-radius: 6px;"
            )
        else:
            self.lbl_set0_banner.setText(self._tr("cero_pendiente"))
            self.lbl_set0_banner.setStyleSheet(
                "background-color: #FFFBEB; color: #D97706; font-size: 11px; font-weight: 800; border: 1px solid #F59E0B; border-radius: 6px;"
            )

        self.produccion_ok = max(0, self.produccion_total - self.produccion_nok)
        self.lbl_kpi_tot_val.setText(str(self.produccion_total))
        self.lbl_kpi_ok_val.setText(str(self.produccion_ok))
        if hasattr(self, "lbl_kpi_nok_val"):
            self.lbl_kpi_nok_val.setText(str(self.produccion_nok))
        if hasattr(self, "opc_worker"):
            self.opc_worker.update_production_counters(
                self.produccion_total, self.produccion_ok, self.produccion_nok
            )

        # Botón Principal de Pintado
        if not self.set_0_confirmado:
            self.btn_pintar_main.setText(self._tr("btn_pintar_bloq"))
            self.btn_pintar_main.setStyleSheet(
                "background-color: #94A3B8; color: #FFFFFF; font-size: 13px; font-weight: 900; border-radius: 8px;"
            )
        elif self.estado_pintando:
            self.btn_pintar_main.setText(self._tr("btn_pintar_on"))
            self.btn_pintar_main.setStyleSheet(
                f"background-color: {COLOR_OK}; color: #FFFFFF; font-size: 14px; font-weight: 900; border: 2px solid #34D399; border-radius: 8px;"
            )
        else:
            self.btn_pintar_main.setText(self._tr("btn_pintar_off"))
            self.btn_pintar_main.setStyleSheet(
                f"background-color: {COLOR_BRAND_PRIMARY}; color: #FFFFFF; font-size: 14px; font-weight: 900; border-radius: 8px;"
            )

        # Badges del Fixture
        self.lbl_title_fixture.setText(
            f"{self._tr('estado_fixture')} ({self.matriz_filas} × {self.matriz_cols})"
        )
        self.lbl_matriz_dim_badge.setText(
            f"MATRIZ: {self.matriz_filas} × {self.matriz_cols} ({self.piezas_por_jig_actual} PZ/JIG)"
        )
        self.lbl_matriz_dist_badge.setText(
            f"X: {self.matriz_delta_x:.1f} mm | Y: {self.matriz_delta_y:.1f} mm | Diag: {self.matriz_dist_diag:.1f} mm"
        )
        self.btn_patron_toggle.setText(
            self._tr("patron_vert") if self.patron_pintado == "vertical" else self._tr("patron_horiz")
        )
        self.btn_coords_toggle.setText(
            f"{self._tr('coords_btn')}: {'ON' if self.mostrar_coords_matriz else 'OFF'}"
        )

        # Destino de pieza actual
        idx_dest = min(self.indice_matriz_actual, max(0, len(self.matriz_coordenadas) - 1))
        if self.matriz_coordenadas:
            dx, dy = self.matriz_coordenadas[idx_dest]
            self.lbl_tele_destino.setText(f"DESTINO P{idx_dest + 1:02d}: X={dx:.1f} Y={dy:.1f}")

        # Modo AUTO / MANUAL y Cruceta
        if self.estado_manual:
            self.btn_modo.setText(self._tr("modo_manual"))
            self.btn_modo.setStyleSheet(
                f"background-color: {COLOR_ACCENT_CYAN}; color: white; font-size: 12px; font-weight: 900; border-radius: 6px;"
            )
        else:
            self.btn_modo.setText(self._tr("modo_auto"))
            self.btn_modo.setStyleSheet(
                f"background-color: {COLOR_BRAND_PRIMARY}; color: white; font-size: 12px; font-weight: 900; border-radius: 6px;"
            )

        jog_enabled = self.estado_manual and (not self.estado_stop) and (not self.paro_requiere_reset)
        for b in (self.btn_jog_up, self.btn_jog_down, self.btn_jog_left, self.btn_jog_right):
            b.setEnabled(jog_enabled)
        self.inp_coord_x.setEnabled(jog_enabled)
        self.inp_coord_y.setEnabled(jog_enabled)

        self.lbl_cero_reg_val.setText(f"X: {self.cero_guardado_x:.2f} | Y: {self.cero_guardado_y:.2f}")

        # Botón PARO y Banner Superior de Estado
        if self.estado_stop:
            self.btn_paro.setText(self._tr("paro_activo"))
            self.btn_paro.setStyleSheet(
                "background-color: #7F1D1D; color: #FCA5A5; font-size: 15px; font-weight: 900; border: 2px solid #EF4444; border-radius: 8px;"
            )
            self.lbl_banner_estado.setText(self._tr("paro_emergencia"))
            self.lbl_banner_estado.setStyleSheet(
                f"background-color: {COLOR_ERR}; color: white; font-size: 12px; font-weight: 900; padding: 0 16px; border-radius: 7px;"
            )
        else:
            self.btn_paro.setText(self._tr("paro"))
            self.btn_paro.setStyleSheet(
                f"background-color: {COLOR_ERR}; color: white; font-size: 16px; font-weight: 900; border-radius: 8px;"
            )
            if self.paro_requiere_reset:
                self.lbl_banner_estado.setText(self._tr("req_reset"))
                self.lbl_banner_estado.setStyleSheet(
                    f"background-color: {COLOR_WARN}; color: white; font-size: 12px; font-weight: 900; padding: 0 16px; border-radius: 7px;"
                )
            elif self.paro_requiere_inicio:
                self.lbl_banner_estado.setText(self._tr("req_inicio"))
                self.lbl_banner_estado.setStyleSheet(
                    f"background-color: {COLOR_ACCENT_CYAN}; color: white; font-size: 12px; font-weight: 900; padding: 0 16px; border-radius: 7px;"
                )
            elif self.estado_pintando:
                self.lbl_banner_estado.setText(self._tr("en_proceso"))
                self.lbl_banner_estado.setStyleSheet(
                    f"background-color: {COLOR_ACCENT_CYAN}; color: white; font-size: 12px; font-weight: 900; padding: 0 16px; border-radius: 7px;"
                )
            elif self.estado_manual:
                self.lbl_banner_estado.setText(self._tr("modo_man_act"))
                self.lbl_banner_estado.setStyleSheet(
                    f"background-color: {COLOR_WARN}; color: white; font-size: 12px; font-weight: 900; padding: 0 16px; border-radius: 7px;"
                )
            else:
                self.lbl_banner_estado.setText(self._tr("sistema_listo"))
                self.lbl_banner_estado.setStyleSheet(
                    f"background-color: {COLOR_OK}; color: white; font-size: 12px; font-weight: 900; padding: 0 16px; border-radius: 7px;"
                )

        # Actualizar Lienzo de 60 FPS
        active_idx = self.indice_matriz_actual if self.estado_pintando else -1
        self.fixture_canvas.configure_matrix(
            filas=self.matriz_filas,
            cols=self.matriz_cols,
            piezas_por_jig=self.piezas_por_jig_actual,
            patron=self.patron_pintado,
            coordenadas=self.matriz_coordenadas,
            estados=self.estados_piezas,
            indice_activo=active_idx,
            mostrar_coords=self.mostrar_coords_matriz,
            imagen_pieza=img_txt,
            pasada_actual=self.pasada_actual_pieza,
            total_pasadas=self.pasadas_tampo,
        )


    # ==========================================================================
    # TELEMETRÍA PLC, ACCIONES DE OPERADOR Y CICLO DE PINTADO
    # ==========================================================================
    @Slot(bool, str)
    def _on_plc_connection_Changed(self, connected, mode_str):
        self.plc_conectado = connected
        self.modo_conexion_str = mode_str
        if connected:
            self.lbl_tele_conn.setText("● PLC CONECTADO")
            self.lbl_tele_conn.setStyleSheet("font-size: 10px; font-weight: 900; color: #059669;")
        else:
            self.lbl_tele_conn.setText(f"○ {mode_str}")
            self.lbl_tele_conn.setStyleSheet("font-size: 10px; font-weight: 900; color: #D97706;")

    @Slot(dict)
    def _on_plc_telemetry(self, snap):
        for k in self.plc_signals:
            if k in snap:
                self.plc_signals[k] = bool(snap[k])

        if "POS_X" in snap:
            self.ultimo_plc_rx_x = float(snap["POS_X"])
        if "POS_Y" in snap:
            self.ultimo_plc_rx_y = float(snap["POS_Y"])

        self.lbl_tele_plcrx.setText(f"PLC RX: X={self.ultimo_plc_rx_x:.1f} Y={self.ultimo_plc_rx_y:.1f}")

        # Actualizar indicadores LED del PLC
        for sig_name, (pill, kind) in self.led_widgets.items():
            on = self.plc_signals.get(sig_name, False)
            if on:
                if kind == "err":
                    bg, fg, bd = "#FEF2F2", "#DC2626", "#EF4444"
                elif kind == "warn":
                    bg, fg, bd = "#FFFBEB", "#D97706", "#F59E0B"
                elif kind == "info":
                    bg, fg, bd = "#E0F2FE", "#0284C7", "#38BDF8"
                else:
                    bg, fg, bd = "#ECFDF5", "#059669", "#10B981"
                pill.setText(f"● {sig_name}")
                pill.setStyleSheet(
                    f"background-color: {bg}; color: {fg}; font-size: 10px; font-weight: 900; "
                    f"border: 1px solid {bd}; border-radius: 5px; padding: 0 4px;"
                )
            else:
                pill.setText(f"○ {sig_name}")
                pill.setStyleSheet(
                    "background-color: #F1F5F9; color: #64748B; font-size: 10px; font-weight: 800; "
                    "border: 1px solid #CBD5E1; border-radius: 5px; padding: 0 4px;"
                )

    def _verificar_seguridad_antes_de_operar(self):
        if self.estado_stop:
            self.overlay.show_message(
                "Paro Activo",
                "El sistema se encuentra en PARO. Libere el botón PARO, presione RESET y luego INICIO.",
                "error",
            )
            return False
        if self.paro_requiere_reset:
            self.overlay.show_message(
                "Secuencia de Rearme Requerida",
                "Tras liberar el PARO es obligatorio presionar [RESET] y posteriormente [INICIO].",
                "warn",
            )
            return False
        if self.paro_requiere_inicio:
            self.overlay.show_message(
                "Confirmación de Inicio Requerida",
                "Presione el botón [INICIO] para habilitar nuevamente los movimientos de la estación.",
                "warn",
            )
            return False
        return True

    def alternar_pintar(self):
        if not self.estado_pintando:
            if not self._verificar_seguridad_antes_de_operar():
                return
            if not self.set_0_confirmado:
                self.overlay.show_message(
                    "Set 0 Pendiente",
                    "Debe calibrar o confirmar el Punto 0 (Set 0) de la receta antes de iniciar el ciclo de pintado.",
                    "warn",
                )
                return
            if self.estado_manual:
                self.overlay.show_message(
                    "Modo Manual Activo",
                    "Cambie a MODO AUTO antes de iniciar el ciclo automático de pintado.",
                    "warn",
                )
                return

            # Iniciar ciclo desde pieza actual (o reiniciar si ya terminó lote)
            if self.piezas_realizadas_lote >= self.piezas_por_jig_actual:
                self.indice_matriz_actual = 0
                self.piezas_realizadas_lote = 0
                self.estados_piezas = ["pendiente"] * (self.matriz_filas * self.matriz_cols)

            self.estado_pintando = True
            self.pasada_actual_pieza = 1
            if self.indice_matriz_actual < len(self.estados_piezas):
                self.estados_piezas[self.indice_matriz_actual] = "activa"

            self.opc_worker.enqueue_write("PINTAR", True)
            self._enviar_pieza_actual_al_plc()

            if not self.plc_conectado:
                self.sim_cycle_timer.start(1100)

            log_debug_hmi(f"Ciclo de pintado INICIADO en pieza #{self.indice_matriz_actual + 1:02d}")
        else:
            self.estado_pintando = False
            self.sim_cycle_timer.stop()
            self.opc_worker.enqueue_write("PINTAR", False)
            log_debug_hmi("Ciclo de pintado PAUSADO/DETENIDO por el operador.")

        self._refresh_all_operator_ui()

    def _enviar_pieza_actual_al_plc(self):
        if not self.matriz_coordenadas:
            return
        idx = max(0, min(self.indice_matriz_actual, len(self.matriz_coordenadas) - 1))
        tx, ty = self.matriz_coordenadas[idx]
        self.pos_actual_x = tx
        self.pos_actual_y = ty
        self.inp_coord_x.setText(f"{tx:.1f}")
        self.inp_coord_y.setText(f"{ty:.1f}")
        self.opc_worker.enqueue_move_xy(tx, ty, self.velocidad_actual, trigger_var="NUEVA_POSICION", pulse_ms=500)
        log_debug_hmi(
            f"Enviando pieza #{idx + 1:02d} (Pasada {self.pasada_actual_pieza}/{self.pasadas_tampo}) -> X={tx:.2f}, Y={ty:.2f} mm"
        )

    def _tick_sim_cycle(self):
        if self.estado_pintando and not self.plc_conectado:
            self._avanzar_siguiente_pieza_ciclo()

    @Slot()
    def _avanzar_siguiente_pieza_ciclo(self):
        if not self.estado_pintando:
            return

        # Verificar si faltan pasadas de tampo en la misma pieza
        if self.pasada_actual_pieza < self.pasadas_tampo:
            self.pasada_actual_pieza += 1
            self._enviar_pieza_actual_al_plc()
            self._refresh_all_operator_ui()
            return

        # Marcar pieza actual como completada OK y contabilizar automáticamente
        if self.indice_matriz_actual < len(self.estados_piezas):
            self.estados_piezas[self.indice_matriz_actual] = "ok"

        self.piezas_realizadas_lote += 1
        self.produccion_total += 1
        self.produccion_ok = max(0, self.produccion_total - self.produccion_nok)
        self.config["produccion_total"] = self.produccion_total
        self.config["produccion_ok"] = self.produccion_ok

        # Verificar si se completó el Jig entero (retorno automático a Home y reinicio de matriz)
        if self.piezas_realizadas_lote >= self.piezas_por_jig_actual:
            self.estado_pintando = False
            self.sim_cycle_timer.stop()
            self.opc_worker.enqueue_write("PINTAR", False)
            guardar_configuracion_disco(self.config)
            self._refresh_all_operator_ui()
            log_debug_hmi(
                f"Fin de ciclo automático: +{self.piezas_por_jig_actual} piezas (Total={self.produccion_total}, OK={self.produccion_ok}, NOK={self.produccion_nok}). Retornando a Home."
            )
            self.ejecutar_home_general()
            QTimer.singleShot(1200, self._reiniciar_matriz_post_ciclo)
            return

        # Avanzar a la siguiente cavidad
        self.indice_matriz_actual += 1
        self.pasada_actual_pieza = 1
        if self.indice_matriz_actual < len(self.estados_piezas):
            self.estados_piezas[self.indice_matriz_actual] = "activa"

        self._enviar_pieza_actual_al_plc()
        self._refresh_all_operator_ui()

    def _reiniciar_matriz_post_ciclo(self):
        if self.estado_pintando:
            return
        self.indice_matriz_actual = 0
        self.pasada_actual_pieza = 1
        self.piezas_realizadas_lote = 0
        self.estados_piezas = ["pendiente"] * (self.matriz_filas * self.matriz_cols)
        self._refresh_all_operator_ui()

    def probar_movimiento_pieza_actual(self):
        if not self._verificar_seguridad_antes_de_operar():
            return
        self._enviar_pieza_actual_al_plc()
        self._refresh_all_operator_ui()

    @Slot(int)
    def _on_fixture_cavity_clicked(self, seq_idx):
        if seq_idx >= self.piezas_por_jig_actual:
            return
        if not self.estado_manual:
            self.indice_matriz_actual = seq_idx
            self._refresh_all_operator_ui()
            return
        if not self._verificar_seguridad_antes_de_operar():
            return
        self.indice_matriz_actual = seq_idx
        self._enviar_pieza_actual_al_plc()
        self._refresh_all_operator_ui()

    def alternar_patron_pintado(self):
        self.patron_pintado = "horizontal" if self.patron_pintado == "vertical" else "vertical"
        self.config["patron_pintado"] = self.patron_pintado
        self.actualizar_coordenadas_matriz()
        guardar_configuracion_disco(self.config)
        self._refresh_all_operator_ui()

    def alternar_mostrar_coords(self):
        self.mostrar_coords_matriz = not self.mostrar_coords_matriz
        self._refresh_all_operator_ui()

    def alternar_panel_jog(self):
        self.jog_collapsible.toggle()
        expanded = self.jog_collapsible.is_expanded
        self.btn_fold_jog.setText(self._tr("plegar") if expanded else self._tr("desplegar"))

    def alternar_seccion_calibracion(self):
        self.cal_collapsible.toggle()
        expanded = self.cal_collapsible.is_expanded
        self.btn_toggle_cal.setText(self._tr("calibracion_hide") if expanded else self._tr("calibracion_show"))

    def alternar_modo(self):
        if self.estado_pintando:
            self.overlay.show_message(
                "Ciclo en Proceso",
                "Detenga el ciclo de pintado antes de cambiar entre Modo AUTO y MANUAL.",
                "warn",
            )
            return
        self.estado_manual = not self.estado_manual
        self.estado_auto = not self.estado_manual
        self.opc_worker.enqueue_write("MANUAL", self.estado_manual)
        self.opc_worker.enqueue_write("AUTO", self.estado_auto)
        self._refresh_all_operator_ui()

    def seleccionar_pasadas_tampo(self, num_pasadas):
        self.pasadas_tampo = 1 if int(num_pasadas) == 1 else 2
        self.config["pasadas_tampo"] = self.pasadas_tampo
        guardar_configuracion_disco(self.config)
        self._refresh_all_operator_ui()

    def ajustar_paso_jog(self, delta):
        self.paso_jog = round(max(0.1, min(200.0, self.paso_jog + delta)), 2)
        self.inp_paso_jog.setText(f"{self.paso_jog:.1f}")
        self.config["paso_jog_default"] = self.paso_jog

    def set_paso_jog_directo(self, val):
        self.paso_jog = round(max(0.1, min(200.0, float(val))), 2)
        self.inp_paso_jog.setText(f"{self.paso_jog:.1f}")
        self.config["paso_jog_default"] = self.paso_jog

    def _on_paso_jog_edited(self):
        try:
            v = float(self.inp_paso_jog.text())
        except ValueError:
            v = 5.0
        self.set_paso_jog_directo(v)

    def abrir_teclado_numerico_paso(self):
        self.overlay.show_jog_numpad(self.paso_jog, self.set_paso_jog_directo)

    def _on_velocidad_edited(self):
        try:
            v = max(1.0, min(500.0, float(self.inp_velocidad.text())))
        except ValueError:
            v = 100.0
        self.velocidad_actual = v
        self.inp_velocidad.setText(str(int(v)))
        self.opc_worker.enqueue_write("VELOCIDAD_X", v)
        self.opc_worker.enqueue_write("VELOCIDAD_Y", v)

    def mover_cruzeta(self, eje, direccion):
        if not self.estado_manual:
            self.overlay.show_message("Modo Automático", "Active el MODO MANUAL para usar la cruceta JOG.", "warn")
            return
        if not self._verificar_seguridad_antes_de_operar():
            return

        # Dirección invertida física idéntica a kostalcode.py (dir_efectiva = -direccion)
        dir_efectiva = -float(direccion)
        if eje.upper() == "X":
            self.pos_actual_x = limitar_coord_x(self.pos_actual_x + self.paso_jog * dir_efectiva)
        else:
            self.pos_actual_y = limitar_coord_y(self.pos_actual_y + self.paso_jog * dir_efectiva)

        self.inp_coord_x.setText(f"{self.pos_actual_x:.1f}")
        self.inp_coord_y.setText(f"{self.pos_actual_y:.1f}")
        self.opc_worker.enqueue_move_xy(
            self.pos_actual_x, self.pos_actual_y, self.velocidad_actual, trigger_var="CRUZETA", pulse_ms=350
        )

    def enviar_coordenadas_manuales(self):
        if not self.estado_manual or not self._verificar_seguridad_antes_de_operar():
            return
        self.pos_actual_x = limitar_coord_x(self.inp_coord_x.text())
        self.pos_actual_y = limitar_coord_y(self.inp_coord_y.text())
        self.inp_coord_x.setText(f"{self.pos_actual_x:.1f}")
        self.inp_coord_y.setText(f"{self.pos_actual_y:.1f}")
        self.opc_worker.enqueue_move_xy(
            self.pos_actual_x, self.pos_actual_y, self.velocidad_actual, trigger_var="NUEVA_POSICION", pulse_ms=500
        )

    def ejecutar_home_general(self):
        if not self._verificar_seguridad_antes_de_operar():
            return
        self.pos_actual_x = self.home_guardado_x
        self.pos_actual_y = self.home_guardado_y
        self.inp_coord_x.setText(f"{self.pos_actual_x:.1f}")
        self.inp_coord_y.setText(f"{self.pos_actual_y:.1f}")
        self.opc_worker.enqueue_move_xy(
            self.pos_actual_x, self.pos_actual_y, self.velocidad_actual, trigger_var="POSICION_INICIAL", pulse_ms=450
        )
        log_debug_hmi(f"Comando HOME ejecutado -> X:{self.pos_actual_x:.2f}, Y:{self.pos_actual_y:.2f}")

    def guardar_posicion_como_cero(self):
        self.cero_guardado_x = limitar_coord_x(self.pos_actual_x)
        self.cero_guardado_y = limitar_coord_y(self.pos_actual_y)
        self.set_0_confirmado = True
        self.config["cero_x"] = self.cero_guardado_x
        self.config["cero_y"] = self.cero_guardado_y
        self.config["set_0_confirmado"] = True

        rec_nom = self.config.get("receta_activa", "")
        for r in self.config.get("recetas", []):
            if r.get("nombre") == rec_nom:
                r["cero_guardado_x"] = self.cero_guardado_x
                r["cero_x"] = self.cero_guardado_x
                r["cero_guardado_y"] = self.cero_guardado_y
                r["cero_y"] = self.cero_guardado_y
                r["cero_calibrado"] = True
                r["set_0_listo"] = True
                break

        self.actualizar_coordenadas_matriz()
        guardar_configuracion_disco(self.config)
        self._refresh_all_operator_ui()
        self.overlay.show_message(
            "Punto 0 (Set 0) Calibrado",
            f"Se registró exitosamente el Punto 0 para '{rec_nom}' en X={self.cero_guardado_x:.2f} mm, Y={self.cero_guardado_y:.2f} mm.",
            "ok",
        )

    def guardar_posicion_como_home(self):
        self.home_guardado_x = limitar_coord_x(self.pos_actual_x)
        self.home_guardado_y = limitar_coord_y(self.pos_actual_y)
        self.config["home_x"] = self.home_guardado_x
        self.config["home_y"] = self.home_guardado_y
        self.opc_worker.enqueue_write("HOME_X", self.home_guardado_x)
        self.opc_worker.enqueue_write("HOME_Y", self.home_guardado_y)
        guardar_configuracion_disco(self.config)
        self.overlay.show_message(
            "Set Home Guardado",
            f"Posición Home actualizada a X={self.home_guardado_x:.2f} mm, Y={self.home_guardado_y:.2f} mm.",
            "ok",
        )

    def activar_tampo_manual(self):
        if not self._verificar_seguridad_antes_de_operar():
            return
        if self.tampo_manual_activo:
            return
        self.tampo_manual_activo = True
        self.btn_tampo_manual.setEnabled(False)
        self.btn_tampo_manual.setText("Tampo Activo (2s)...")
        self.opc_worker.enqueue_pulse("S_TAMPO", duration_ms=2000)
        QTimer.singleShot(2100, self._finalizar_tampo_manual)

    def _finalizar_tampo_manual(self):
        self.tampo_manual_activo = False
        self.btn_tampo_manual.setEnabled(True)
        self.btn_tampo_manual.setText(self._tr("activar_tampo"))

    def iniciar_ciclo_general(self):
        if self.estado_stop:
            self.overlay.show_message("Paro Activo", "Libere primero el botón de PARO.", "error")
            return
        if self.paro_requiere_reset:
            self.overlay.show_message("Reset Requerido", "Presione primero [RESET] antes de [INICIO].", "warn")
            return
        self.paro_requiere_inicio = False
        self.opc_worker.enqueue_pulse("INICIO", duration_ms=400)
        self._refresh_all_operator_ui()
        log_debug_hmi("Pulso de INICIO enviado al PLC.")

    def ejecutar_reset_general(self):
        if self.estado_stop:
            self.overlay.show_message("Paro Activo", "Libere primero el botón de PARO antes de hacer RESET.", "warn")
            return
        self.estado_pintando = False
        self.sim_cycle_timer.stop()
        if self.paro_requiere_reset:
            self.paro_requiere_reset = False
            self.paro_requiere_inicio = True

        self.indice_matriz_actual = 0
        self.pasada_actual_pieza = 1
        self.piezas_realizadas_lote = 0
        self.estados_piezas = ["pendiente"] * (self.matriz_filas * self.matriz_cols)

        self.opc_worker.enqueue_write("PINTAR", False)
        self.opc_worker.enqueue_pulse("RESET", duration_ms=400)
        self._refresh_all_operator_ui()
        log_debug_hmi("Comando RESET ejecutado.")

    def alternar_stop(self):
        self.estado_stop = not self.estado_stop
        if self.estado_stop:
            self.estado_pintando = False
            self.sim_cycle_timer.stop()
            self.paro_requiere_reset = True
            self.paro_requiere_inicio = True
            self.opc_worker.enqueue_write("PINTAR", False)
            self.opc_worker.enqueue_write("STOP", True)
            log_debug_hmi("PARO ACTIVADO por el usuario.")
        else:
            self.opc_worker.enqueue_write("STOP", False)
            log_debug_hmi("PARO liberado -> Esperando secuencia RESET -> INICIO.")
        self._refresh_all_operator_ui()

    def ajustar_nok_manual(self, delta):
        """Permite integrar o ajustar manualmente las piezas NO OK descontándolas automáticamente de OK."""
        nuevo_nok = max(0, min(self.produccion_total, self.produccion_nok + int(delta)))
        self.produccion_nok = nuevo_nok
        self.produccion_ok = max(0, self.produccion_total - self.produccion_nok)
        self.config["produccion_nok"] = self.produccion_nok
        self.config["produccion_ok"] = self.produccion_ok
        guardar_configuracion_disco(self.config)
        self._refresh_all_operator_ui()

    def abrir_dialogo_fin_de_hora(self):
        rec_nom = self.config.get("receta_activa", "")
        self.overlay.show_hourly_log_modal(
            rec_nom,
            self.produccion_total,
            self.produccion_nok,
            self._guardar_corte_fin_de_hora,
        )

    def _guardar_corte_fin_de_hora(self, total_h, nok_h):
        total_auto = self.produccion_total
        nok_manual = max(0, min(total_auto, int(nok_h)))
        ok_auto = max(0, total_auto - nok_manual)
        cal = round((ok_auto / total_auto * 100.0), 1) if total_auto > 0 else 100.0
        registro = {
            "fecha": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usuario": self.usuario_actual.get("nombre", "Operador"),
            "receta": self.config.get("receta_activa", ""),
            "total": total_auto,
            "ok": ok_auto,
            "nok": nok_manual,
            "calidad": f"{cal:.1f}%",
        }
        self.config.setdefault("historial_horas", []).append(registro)
        self.produccion_ok = ok_auto
        self.produccion_nok = nok_manual
        self.config["produccion_total"] = self.produccion_total
        self.config["produccion_ok"] = self.produccion_ok
        self.config["produccion_nok"] = self.produccion_nok
        guardar_configuracion_disco(self.config)
        self._refresh_all_operator_ui()
        self._refresh_admin_stats_table()
        self.overlay.show_message(
            "Corte de Hora Registrado",
            f"Se guardó el registro horario: Total Automático={total_auto}, OK={ok_auto}, NO OK Manual={nok_manual} ({cal:.1f}%).",
            "ok",
        )


    # ==========================================================================
    # RELOJ DE SESIÓN, VENTANA Y NAVEGACIÓN A PANEL DE ADMINISTRACIÓN
    # ==========================================================================
    def _tick_session_clock(self):
        self.sesion_segundos += 1
        hrs = self.sesion_segundos // 3600
        mins = (self.sesion_segundos % 3600) // 60
        secs = self.sesion_segundos % 60
        t_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"
        self.lbl_header_timer.setText(t_str)
        self.lbl_tiempo_sesion_val.setText(t_str)

    def _update_user_header_ui(self):
        nom = self.usuario_actual.get("nombre", "Operador")
        rol = self.usuario_actual.get("rol", "OPERADOR").upper()
        self.lbl_header_user.setText(f"USUARIO: {nom}")
        self.lbl_header_rol.setText(rol)
        es_admin = (rol == "ADMINISTRADOR")
        self.btn_header_admin.setVisible(es_admin)
        self.btn_recetario_admin_shortcut.setVisible(es_admin)

    def alternar_pantalla_completa(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.btn_header_fs.setText(self._tr("salir_pantalla") if self.isFullScreen() else self._tr("pantalla"))

    def _cerrar_sesion(self):
        if self.estado_pintando:
            self.overlay.show_message(
                "Ciclo en Proceso",
                "Detenga el ciclo de pintado antes de cerrar la sesión activa.",
                "warn",
            )
            return
        self._activate_login_page()

    def _open_admin_panel(self):
        rol = self.usuario_actual.get("rol", "OPERADOR").upper()
        if rol != "ADMINISTRADOR":
            self.overlay.show_message(
                "Acceso Restringido",
                "Solo los usuarios con rol ADMINISTRADOR pueden acceder al Panel de Ingeniería y Recetario.",
                "warn",
            )
            return
        self._refresh_admin_recipes_list()
        self._refresh_admin_stats_table()
        self._refresh_admin_users_table()
        self._load_admin_global_config_fields()
        self.stack.setCurrentIndex(3)

    # ==========================================================================
    # PÁGINA 3: PANEL DE ADMINISTRACIÓN E INGENIERÍA (4 PESTAÑAS)
    # ==========================================================================
    def _build_admin_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header Admin
        hdr = QFrame()
        hdr.setObjectName("HeaderBar")
        hdr.setFixedHeight(54)
        hlay = QHBoxLayout(hdr)
        hlay.setContentsMargins(16, 6, 16, 6)
        hlay.setSpacing(12)

        pix_kostal_adm = cargar_logo_corporativo("LogoKostal.webp", 140, 28)
        if pix_kostal_adm:
            lbl_k_adm = QLabel()
            lbl_k_adm.setPixmap(pix_kostal_adm)
            hlay.addWidget(lbl_k_adm)

        lbl_t = QLabel("PANEL DE INGENIERÍA Y ADMINISTRACIÓN")
        lbl_t.setStyleSheet("color: #FFFFFF; font-size: 16px; font-weight: 900;")
        hlay.addWidget(lbl_t)
        hlay.addStretch(1)

        pix_bal_adm = cargar_logo_corporativo("LOGO_BALUARTE.png", 130, 32)
        if pix_bal_adm:
            bal_adm_badge = QFrame()
            bal_adm_badge.setStyleSheet("background-color: #FFFFFF; border-radius: 6px;")
            bal_adm_lay = QHBoxLayout(bal_adm_badge)
            bal_adm_lay.setContentsMargins(8, 2, 8, 2)
            lbl_bal_adm = QLabel()
            lbl_bal_adm.setPixmap(pix_bal_adm)
            bal_adm_lay.addWidget(lbl_bal_adm)
            hlay.addWidget(bal_adm_badge)

        btn_back = QPushButton("Volver a Pantalla de Operación")
        btn_back.setFixedHeight(34)
        btn_back.setStyleSheet(
            f"background-color: {COLOR_OK}; color: white; font-size: 12px; font-weight: 800; padding: 0 14px; border-radius: 6px;"
        )
        btn_back.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        hlay.addWidget(btn_back)
        root.addWidget(hdr)

        body = QHBoxLayout()
        body.setContentsMargins(12, 10, 12, 10)
        body.setSpacing(12)

        # Sidebar de navegación
        sidebar = QFrame()
        sidebar.setObjectName("IndustrialCard")
        sidebar.setFixedWidth(230)
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(10, 12, 10, 12)
        slay.setSpacing(8)

        lbl_nav = QLabel("MÓDULOS DE GESTIÓN")
        lbl_nav.setStyleSheet("color: #64748B; font-size: 10px; font-weight: 900;")
        slay.addWidget(lbl_nav)

        self.admin_stack = QStackedWidget()

        nav_items = [
            ("Recetario de Producción", 0),
            ("Estadísticas y Reportes", 1),
            ("Control de Usuarios", 2),
            ("Configuración Global PLC", 3),
        ]
        self.admin_nav_btns = []
        for text, idx in nav_items:
            b = QPushButton(text)
            b.setMinimumHeight(40)
            b.setStyleSheet(
                "QPushButton { text-align: left; padding-left: 12px; background-color: #F1F5F9; color: #0F172A; font-weight: 800; border-radius: 7px; }"
                "QPushButton:hover { background-color: #E2E8F0; }"
            )
            b.clicked.connect(lambda _, i=idx: self._switch_admin_tab(i))
            slay.addWidget(b)
            self.admin_nav_btns.append(b)

        slay.addStretch(1)

        btn_kb_adm = QPushButton("Teclado en Pantalla")
        btn_kb_adm.setMinimumHeight(36)
        btn_kb_adm.clicked.connect(abrir_teclado_tactil_windows)
        slay.addWidget(btn_kb_adm)

        body.addWidget(sidebar)

        # Páginas internas del Admin
        self.admin_stack.addWidget(self._build_admin_tab_recipes())
        self.admin_stack.addWidget(self._build_admin_tab_stats())
        self.admin_stack.addWidget(self._build_admin_tab_users())
        self.admin_stack.addWidget(self._build_admin_tab_config())

        body.addWidget(self.admin_stack, 1)
        root.addLayout(body, 1)
        self._switch_admin_tab(0)
        return page

    def _switch_admin_tab(self, idx):
        self.admin_stack.setCurrentIndex(idx)
        for i, b in enumerate(self.admin_nav_btns):
            if i == idx:
                b.setStyleSheet(
                    f"text-align: left; padding-left: 12px; background-color: {COLOR_BRAND_PRIMARY}; color: white; font-weight: 800; border-radius: 7px;"
                )
            else:
                b.setStyleSheet(
                    "text-align: left; padding-left: 12px; background-color: #F1F5F9; color: #0F172A; font-weight: 800; border-radius: 7px;"
                )

    # ---------------- TAB 0: RECETARIO DE PRODUCCIÓN ----------------
    def _build_admin_tab_recipes(self):
        tab = QFrame()
        tab.setObjectName("IndustrialCard")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        lbl = QLabel("Gestión de Recetas, Matrices de Fixture y Punto Cero (Set 0)")
        lbl.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 15px; font-weight: 900;")
        top.addWidget(lbl)
        top.addStretch(1)

        self.combo_admin_rec = QComboBox()
        self.combo_admin_rec.setView(QListView())
        self.combo_admin_rec.setMinimumWidth(260)
        self.combo_admin_rec.setMinimumHeight(34)
        self.combo_admin_rec.currentTextChanged.connect(self._on_admin_recipe_selected)
        top.addWidget(self.combo_admin_rec)

        btn_new = QPushButton("Nueva Receta")
        btn_new.setMinimumHeight(34)
        btn_new.setStyleSheet(f"background-color: {COLOR_ACCENT_CYAN}; color: white; font-weight: 800;")
        btn_new.clicked.connect(self._admin_new_recipe)
        top.addWidget(btn_new)

        btn_del = QPushButton("Eliminar")
        btn_del.setMinimumHeight(34)
        btn_del.setStyleSheet(f"background-color: {COLOR_ERR}; color: white; font-weight: 800;")
        btn_del.clicked.connect(self._admin_delete_recipe)
        top.addWidget(btn_del)
        lay.addLayout(top)

        form = QGridLayout()
        form.setSpacing(10)

        self.adm_rec_nombre = QLineEdit()
        self.adm_rec_pieza = QLineEdit()
        self.adm_rec_modelo = QLineEdit()
        self.adm_rec_filas = QLineEdit("3")
        self.adm_rec_cols = QLineEdit("5")
        self.adm_rec_pz_jig = QLineEdit("15")
        self.adm_rec_dx = QLineEdit("20.0")
        self.adm_rec_dy = QLineEdit("35.0")
        self.adm_rec_cx = QLineEdit("0.0")
        self.adm_rec_cy = QLineEdit("0.0")
        self.adm_rec_vel = QLineEdit("100.0")
        self.adm_rec_img = QLineEdit("pieza_default.png")

        fields = [
            ("Nombre de Receta / Jig:", self.adm_rec_nombre, 0, 0),
            ("Nombre de Pieza:", self.adm_rec_pieza, 0, 2),
            ("Modelo:", self.adm_rec_modelo, 1, 0),
            ("Velocidad Ejes (mm/s):", self.adm_rec_vel, 1, 2),
            ("Filas de Matriz (1-5):", self.adm_rec_filas, 2, 0),
            ("Columnas de Matriz (1-5):", self.adm_rec_cols, 2, 2),
            ("Piezas Activas por Jig:", self.adm_rec_pz_jig, 3, 0),
            ("Imagen de Pieza:", self.adm_rec_img, 3, 2),
            ("Distancia C-C X (mm):", self.adm_rec_dx, 4, 0),
            ("Distancia C-C Y (mm):", self.adm_rec_dy, 4, 2),
            ("Punto 0 — Cero X (0-88 mm):", self.adm_rec_cx, 5, 0),
            ("Punto 0 — Cero Y (0-200 mm):", self.adm_rec_cy, 5, 2),
        ]
        for label_txt, widget, r, c in fields:
            l = QLabel(label_txt)
            l.setStyleSheet("font-size: 12px; font-weight: 700; color: #334155;")
            form.addWidget(l, r, c)
            widget.setMinimumHeight(34)
            form.addWidget(widget, r, c + 1)

        lay.addLayout(form)

        img_row = QHBoxLayout()
        btn_browse = QPushButton("Seleccionar Imagen de Pieza...")
        btn_browse.clicked.connect(self._admin_browse_image)
        img_row.addWidget(btn_browse)

        btn_use_curr_pos = QPushButton("Tomar Posición Actual del PLC como Punto 0")
        btn_use_curr_pos.setStyleSheet(f"background-color: {COLOR_WARN}; color: white; font-weight: 800;")
        btn_use_curr_pos.clicked.connect(self._admin_take_current_pos_as_zero)
        img_row.addWidget(btn_use_curr_pos)
        img_row.addStretch(1)
        lay.addLayout(img_row)

        lay.addStretch(1)

        btn_save = QPushButton("GUARDAR CAMBIOS EN RECETA Y ACTIVAR")
        btn_save.setMinimumHeight(44)
        btn_save.setStyleSheet(f"background-color: {COLOR_OK}; color: white; font-size: 13px; font-weight: 900;")
        btn_save.clicked.connect(self._admin_save_recipe)
        lay.addWidget(btn_save)
        return tab

    def _refresh_admin_recipes_list(self):
        self.combo_admin_rec.blockSignals(True)
        self.combo_admin_rec.clear()
        for r in self.config.get("recetas", []):
            self.combo_admin_rec.addItem(r.get("nombre", ""))
        act = self.config.get("receta_activa", "")
        if act:
            self.combo_admin_rec.setCurrentText(act)
        self.combo_admin_rec.blockSignals(False)
        self._on_admin_recipe_selected(self.combo_admin_rec.currentText())

    def _on_admin_recipe_selected(self, nom):
        rec = next((r for r in self.config.get("recetas", []) if r.get("nombre") == nom), None)
        if not rec:
            return
        self.adm_rec_nombre.setText(rec.get("nombre", ""))
        self.adm_rec_pieza.setText(rec.get("pieza", ""))
        self.adm_rec_modelo.setText(rec.get("modelo", ""))
        self.adm_rec_filas.setText(str(rec.get("filas", 3)))
        self.adm_rec_cols.setText(str(rec.get("columnas", 5)))
        self.adm_rec_pz_jig.setText(str(obtener_piezas_por_jig_receta(rec)))
        self.adm_rec_dx.setText(str(rec.get("espacio_x", 20.0)))
        self.adm_rec_dy.setText(str(rec.get("espacio_y", 35.0)))
        self.adm_rec_cx.setText(str(rec.get("cero_x", 0.0)))
        self.adm_rec_cy.setText(str(rec.get("cero_y", 0.0)))
        self.adm_rec_vel.setText(str(rec.get("vel_x", 100.0)))
        self.adm_rec_img.setText(rec.get("imagen", "pieza_default.png"))

    def _admin_browse_image(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar Imagen de Pieza", BASE_DIR, "Imágenes (*.png *.jpg *.jpeg *.bmp)"
        )
        if fn:
            self.adm_rec_img.setText(fn)

    def _admin_take_current_pos_as_zero(self):
        self.adm_rec_cx.setText(f"{limitar_coord_x(self.pos_actual_x):.2f}")
        self.adm_rec_cy.setText(f"{limitar_coord_y(self.pos_actual_y):.2f}")

    def _admin_new_recipe(self):
        n = len(self.config.get("recetas", [])) + 1
        nueva = {
            "nombre": f"#{n} - Nueva Receta KOSTAL",
            "pieza": "Nueva Pieza",
            "modelo": f"MOD-{n:02d}",
            "filas": 3,
            "columnas": 5,
            "piezas_por_jig": 15,
            "espacio_x": 20.0,
            "espacio_y": 35.0,
            "cero_x": 0.0,
            "cero_y": 0.0,
            "set_0_listo": True,
            "vel_x": 100.0,
            "vel_y": 100.0,
            "imagen": "pieza_default.png",
        }
        self.config.setdefault("recetas", []).append(nueva)
        guardar_configuracion_disco(self.config)
        self._refresh_admin_recipes_list()
        self.combo_admin_rec.setCurrentText(nueva["nombre"])

    def _admin_delete_recipe(self):
        recetas = self.config.get("recetas", [])
        if len(recetas) <= 1:
            self.overlay.show_message("Acción No Permitida", "Debe existir al menos una receta en el sistema.", "warn")
            return
        actual = self.combo_admin_rec.currentText()
        self.config["recetas"] = [r for r in recetas if r.get("nombre") != actual]
        guardar_configuracion_disco(self.config)
        self._refresh_admin_recipes_list()
        self.cargar_receta_por_nombre(self.config["recetas"][0]["nombre"], silencioso=True)

    def _admin_save_recipe(self):
        orig_name = self.combo_admin_rec.currentText()
        rec = next((r for r in self.config.get("recetas", []) if r.get("nombre") == orig_name), None)
        if not rec:
            return
        try:
            f = max(1, min(5, int(float(self.adm_rec_filas.text() or 3))))
            c = max(1, min(5, int(float(self.adm_rec_cols.text() or 5))))
            pz = max(1, min(f * c, int(float(self.adm_rec_pz_jig.text() or (f * c)))))
            dx = abs(float(self.adm_rec_dx.text() or 20.0))
            dy = abs(float(self.adm_rec_dy.text() or 35.0))
            cx = limitar_coord_x(self.adm_rec_cx.text())
            cy = limitar_coord_y(self.adm_rec_cy.text())
            vel = max(1.0, min(500.0, float(self.adm_rec_vel.text() or 100.0)))
        except ValueError:
            self.overlay.show_message("Error de Formato", "Verifique que los campos numéricos sean válidos.", "error")
            return

        nuevo_nom = self.adm_rec_nombre.text().strip() or orig_name
        rec.update({
            "nombre": nuevo_nom,
            "pieza": self.adm_rec_pieza.text().strip() or "Pieza",
            "modelo": self.adm_rec_modelo.text().strip() or "M1",
            "filas": f,
            "cols": c,
            "columnas": c,
            "piezas": pz,
            "piezas_por_jig": pz,
            "delta_x": dx,
            "espacio_x": dx,
            "delta_y": dy,
            "espacio_y": dy,
            "cero_guardado_x": cx,
            "cero_x": cx,
            "cero_guardado_y": cy,
            "cero_y": cy,
            "cero_calibrado": True,
            "set_0_listo": True,
            "velocidad": vel,
            "vel_x": vel,
            "vel_y": vel,
            "imagen": self.adm_rec_img.text().strip(),
        })
        guardar_configuracion_disco(self.config)
        self.cargar_receta_por_nombre(nuevo_nom, silencioso=True)
        self._refresh_admin_recipes_list()
        self.overlay.show_message("Receta Guardada", f"La receta '{nuevo_nom}' fue actualizada y activada.", "ok")

    # ---------------- TAB 1: ESTADÍSTICAS Y EXPORTACIÓN EXCEL ----------------
    def _build_admin_tab_stats(self):
        tab = QFrame()
        tab.setObjectName("IndustrialCard")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        lbl = QLabel("Historial de Cortes Horarios y Reportes de Producción")
        lbl.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 15px; font-weight: 900;")
        top.addWidget(lbl)
        top.addStretch(1)

        btn_exp = QPushButton("Exportar Reporte a Excel (.xlsx)")
        btn_exp.setMinimumHeight(36)
        btn_exp.setStyleSheet(f"background-color: {COLOR_OK}; color: white; font-weight: 800;")
        btn_exp.clicked.connect(self._export_stats_excel)
        top.addWidget(btn_exp)

        btn_clr = QPushButton("Reiniciar Contadores")
        btn_clr.setMinimumHeight(36)
        btn_clr.setStyleSheet(f"background-color: {COLOR_WARN}; color: white; font-weight: 800;")
        btn_clr.clicked.connect(self._reset_production_counters)
        top.addWidget(btn_clr)
        lay.addLayout(top)

        self.tbl_stats = QTableWidget(0, 7)
        self.tbl_stats.setHorizontalHeaderLabels([
            "Fecha / Hora", "Operador", "Receta / Jig", "Total", "OK", "NOK", "% Calidad"
        ])
        self.tbl_stats.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_stats.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.tbl_stats, 1)
        return tab

    def _refresh_admin_stats_table(self):
        hist = self.config.get("historial_horas", [])
        self.tbl_stats.setRowCount(len(hist))
        for r_idx, item in enumerate(reversed(hist)):
            vals = [
                item.get("fecha", "-"),
                item.get("usuario", "-"),
                item.get("receta", "-"),
                str(item.get("total", 0)),
                str(item.get("ok", 0)),
                str(item.get("nok", 0)),
                str(item.get("calidad", "100.0%")),
            ]
            for c_idx, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                cell.setTextAlignment(Qt.AlignCenter)
                self.tbl_stats.setItem(r_idx, c_idx, cell)

    def _reset_production_counters(self):
        self.produccion_total = 0
        self.produccion_ok = 0
        self.produccion_nok = 0
        self.config["produccion_total"] = 0
        self.config["produccion_ok"] = 0
        self.config["produccion_nok"] = 0
        self.config["historial_horas"] = []
        guardar_configuracion_disco(self.config)
        self._refresh_admin_stats_table()
        self._refresh_all_operator_ui()
        self.overlay.show_message("Contadores Reiniciados", "Se han puesto en cero los contadores de producción.", "ok")

    def _export_stats_excel(self):
        if not OPENPYXL_DISPONIBLE:
            self.overlay.show_message("Módulo Faltante", "openpyxl no está instalado en este entorno.", "warn")
            return
        fn, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar Reporte KOSTAL",
            os.path.join(BASE_DIR, f"Reporte_KOSTAL_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"),
            "Excel (*.xlsx)",
        )
        if not fn:
            return
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Produccion KOSTAL"
            headers = ["Fecha / Hora", "Operador", "Receta / Jig", "Total", "OK", "NOK", "% Calidad"]
            ws.append(headers)
            for cell in ws[1]:
                cell.font = XlFont(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="003A70")
                cell.alignment = Alignment(horizontal="center")
            for item in self.config.get("historial_horas", []):
                ws.append([
                    item.get("fecha", ""),
                    item.get("usuario", ""),
                    item.get("receta", ""),
                    item.get("total", 0),
                    item.get("ok", 0),
                    item.get("nok", 0),
                    item.get("calidad", "100.0%"),
                ])
            wb.save(fn)
            self.overlay.show_message("Exportación Exitosa", f"Reporte guardado en:\n{fn}", "ok")
        except Exception as e:
            self.overlay.show_message("Error al Exportar", str(e), "error")

    # ---------------- TAB 2: CONTROL DE USUARIOS ----------------
    def _build_admin_tab_users(self):
        tab = QFrame()
        tab.setObjectName("IndustrialCard")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        lbl = QLabel("Administración de Usuarios y Roles de Acceso")
        lbl.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 15px; font-weight: 900;")
        lay.addWidget(lbl)

        self.tbl_users = QTableWidget(0, 4)
        self.tbl_users.setHorizontalHeaderLabels(["ID Usuario", "Nombre Completo", "Rol", "Contraseña"])
        self.tbl_users.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_users.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.tbl_users, 1)

        row_add = QHBoxLayout()
        self.inp_u_id = QLineEdit()
        self.inp_u_id.setPlaceholderText("ID usuario (ej. op2)")
        self.inp_u_nom = QLineEdit()
        self.inp_u_nom.setPlaceholderText("Nombre completo")
        self.inp_u_pass = QLineEdit()
        self.inp_u_pass.setPlaceholderText("Contraseña / PIN")
        self.combo_u_rol = QComboBox()
        self.combo_u_rol.setView(QListView())
        self.combo_u_rol.addItems(["OPERADOR", "ADMINISTRADOR"])

        btn_add_u = QPushButton("Agregar / Actualizar")
        btn_add_u.setStyleSheet(f"background-color: {COLOR_OK}; color: white; font-weight: 800;")
        btn_add_u.clicked.connect(self._admin_add_user)

        btn_del_u = QPushButton("Eliminar Seleccionado")
        btn_del_u.setStyleSheet(f"background-color: {COLOR_ERR}; color: white; font-weight: 800;")
        btn_del_u.clicked.connect(self._admin_del_user)

        for w in (self.inp_u_id, self.inp_u_nom, self.inp_u_pass, self.combo_u_rol, btn_add_u, btn_del_u):
            w.setMinimumHeight(34)
            row_add.addWidget(w)
        lay.addLayout(row_add)
        return tab

    def _refresh_admin_users_table(self):
        usrs = self.config.get("usuarios", [])
        self.tbl_users.setRowCount(len(usrs))
        for r_idx, u in enumerate(usrs):
            for c_idx, k in enumerate(("user", "nombre", "rol", "pass")):
                it = QTableWidgetItem(str(u.get(k, "")))
                it.setTextAlignment(Qt.AlignCenter)
                self.tbl_users.setItem(r_idx, c_idx, it)
        self._refresh_login_users_combo()

    def _admin_add_user(self):
        uid = self.inp_u_id.text().strip()
        nom = self.inp_u_nom.text().strip()
        pwd = self.inp_u_pass.text().strip()
        rol = self.combo_u_rol.currentText()
        if not uid or not pwd:
            self.overlay.show_message("Datos Incompletos", "Ingrese al menos ID de usuario y contraseña.", "warn")
            return
        usrs = self.config.setdefault("usuarios", [])
        ex = next((u for u in usrs if u.get("user") == uid), None)
        if ex:
            ex.update({"nombre": nom or uid, "pass": pwd, "rol": rol})
        else:
            usrs.append({"user": uid, "nombre": nom or uid, "pass": pwd, "rol": rol})
        guardar_configuracion_disco(self.config)
        self._refresh_admin_users_table()
        self.inp_u_id.clear()
        self.inp_u_nom.clear()
        self.inp_u_pass.clear()

    def _admin_del_user(self):
        row = self.tbl_users.currentRow()
        if row < 0:
            return
        uid = self.tbl_users.item(row, 0).text()
        usrs = self.config.get("usuarios", [])
        if len(usrs) <= 1:
            return
        self.config["usuarios"] = [u for u in usrs if u.get("user") != uid]
        guardar_configuracion_disco(self.config)
        self._refresh_admin_users_table()

    # ---------------- TAB 3: CONFIGURACIÓN GLOBAL OPC UA ----------------
    def _build_admin_tab_config(self):
        tab = QFrame()
        tab.setObjectName("IndustrialCard")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        lbl = QLabel("Parámetros Globales de Red OPC UA y Estación")
        lbl.setStyleSheet(f"color: {COLOR_BRAND_PRIMARY}; font-size: 15px; font-weight: 900;")
        lay.addWidget(lbl)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.adm_cfg_est = QLineEdit()
        self.adm_cfg_ip = QLineEdit()
        self.adm_cfg_port = QLineEdit()
        self.adm_cfg_ns = QLineEdit()
        self.adm_cfg_prefix = QLineEdit()
        self.adm_cfg_hx = QLineEdit()
        self.adm_cfg_hy = QLineEdit()

        cfg_items = [
            ("Nombre de Estación:", self.adm_cfg_est, 0),
            ("IP del PLC WAGO (Puerto 1):", self.adm_cfg_ip, 1),
            ("Puerto OPC UA PLC:", self.adm_cfg_port, 2),
            ("Namespace Index (ns):", self.adm_cfg_ns, 3),
            ("Prefijo GVL de Nodos PLC:", self.adm_cfg_prefix, 4),
            ("Coordenada Home X (mm):", self.adm_cfg_hx, 5),
            ("Coordenada Home Y (mm):", self.adm_cfg_hy, 6),
        ]
        for txt, w, r in cfg_items:
            l = QLabel(txt)
            l.setStyleSheet("font-weight: 700; color: #334155;")
            grid.addWidget(l, r, 0)
            w.setMinimumHeight(36)
            grid.addWidget(w, r, 1)

        lay.addLayout(grid)
        lay.addStretch(1)

        row_b = QHBoxLayout()
        btn_reconnect = QPushButton("Reconectar con PLC Ahora")
        btn_reconnect.setMinimumHeight(42)
        btn_reconnect.setStyleSheet(f"background-color: {COLOR_ACCENT_CYAN}; color: white; font-weight: 800;")
        btn_reconnect.clicked.connect(self._admin_save_and_reconnect_plc)
        row_b.addWidget(btn_reconnect)

        btn_save_cfg = QPushButton("Guardar Configuración Global")
        btn_save_cfg.setMinimumHeight(42)
        btn_save_cfg.setStyleSheet(f"background-color: {COLOR_OK}; color: white; font-weight: 800;")
        btn_save_cfg.clicked.connect(self._admin_save_global_config)
        row_b.addWidget(btn_save_cfg)
        lay.addLayout(row_b)
        return tab

    def _load_admin_global_config_fields(self):
        self.adm_cfg_est.setText(self.config.get("estacion_nombre", "EST-KOSTAL-04"))
        self.adm_cfg_ip.setText(self.config.get("opc_ip", "192.168.1.69"))
        self.adm_cfg_port.setText(str(self.config.get("opc_port", "4840")))
        self.adm_cfg_ns.setText(str(self.config.get("plc_namespace", 4)))
        self.adm_cfg_prefix.setText(self.config.get("node_prefix", ""))
        self.adm_cfg_hx.setText(str(self.home_guardado_x))
        self.adm_cfg_hy.setText(str(self.home_guardado_y))

    def _admin_save_global_config(self):
        self.config["estacion_nombre"] = self.adm_cfg_est.text().strip() or "EST-KOSTAL-04"
        self.config["opc_ip"] = self.adm_cfg_ip.text().strip() or "192.168.1.69"
        self.config["opc_port"] = self.adm_cfg_port.text().strip() or "4840"
        try:
            self.config["plc_namespace"] = int(self.adm_cfg_ns.text().strip() or 4)
        except ValueError:
            self.config["plc_namespace"] = 4
        self.config["node_prefix"] = self.adm_cfg_prefix.text().strip()
        self.home_guardado_x = limitar_coord_x(self.adm_cfg_hx.text())
        self.home_guardado_y = limitar_coord_y(self.adm_cfg_hy.text())
        self.config["home_x"] = self.home_guardado_x
        self.config["home_y"] = self.home_guardado_y
        self.lbl_header_estacion.setText(self.config["estacion_nombre"])
        guardar_configuracion_disco(self.config)
        self.overlay.show_message("Configuración Guardada", "Parámetros globales actualizados en disco.", "ok")

    def _admin_save_and_reconnect_plc(self):
        self._admin_save_global_config()
        self.opc_worker.request_retry_connection()


# ==============================================================================
# PUNTO DE ENTRADA DE LA APLICACIÓN PYSIDE6
# ==============================================================================
def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Forzar paleta industrial clara explícita para que el Modo Oscuro de Windows 10/11
    # nunca pinte de negro los desplegables (QComboBox popups), tablas ni menús.
    light_pal = QPalette()
    light_pal.setColor(QPalette.Window, QColor(COLOR_BG_MAIN))
    light_pal.setColor(QPalette.WindowText, QColor(COLOR_TEXT_PRI))
    light_pal.setColor(QPalette.Base, QColor("#FFFFFF"))
    light_pal.setColor(QPalette.AlternateBase, QColor("#F8FAFC"))
    light_pal.setColor(QPalette.ToolTipBase, QColor("#FFFFFF"))
    light_pal.setColor(QPalette.ToolTipText, QColor(COLOR_TEXT_PRI))
    light_pal.setColor(QPalette.Text, QColor(COLOR_TEXT_PRI))
    light_pal.setColor(QPalette.Button, QColor("#FFFFFF"))
    light_pal.setColor(QPalette.ButtonText, QColor(COLOR_TEXT_PRI))
    light_pal.setColor(QPalette.BrightText, QColor("#FFFFFF"))
    light_pal.setColor(QPalette.Highlight, QColor("#E0F2FE"))
    light_pal.setColor(QPalette.HighlightedText, QColor("#0369A1"))
    app.setPalette(light_pal)

    kb_filter = _AutoTouchKeyboardFilter(app)
    app.installEventFilter(kb_filter)

    window = KostalHmiWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
