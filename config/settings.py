"""
config/settings.py

Configuracion centralizada del sistema TFG Control AC.
Todas las constantes del sistema estan aqui; ningun modulo
debe tener magic numbers propios.
"""

import os
from pathlib import Path

# =============================================================================
# RUTAS DEL PROYECTO
# =============================================================================

BASE_DIR   = Path(__file__).resolve().parent.parent

CONFIG_DIR = BASE_DIR / "config"
LOG_DIR    = BASE_DIR / "logs"
IMAGE_DIR  = BASE_DIR / "images"
DEBUG_DIR  = BASE_DIR / "debug_output"

for _dir in (CONFIG_DIR, LOG_DIR, IMAGE_DIR, DEBUG_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# =============================================================================
# FICHEROS DE DATOS
# =============================================================================

BROADLINK_CONFIG = CONFIG_DIR / "broadlink_config.json"
CODIGOS_AC       = CONFIG_DIR / "codigos_ac.json"
ESTADO_AC        = CONFIG_DIR / "estado_ac.json"
LOG_FILE         = LOG_DIR    / "ac_control.log"

# =============================================================================
# CAMARA Y DETECCION LED
# =============================================================================

CAMARA_INDEX  = 0
CAMARA_WIDTH  = 1280
CAMARA_HEIGHT = 720

# ROI del LED indicador del AC (x1, y1, x2, y2) en pixeles.
# Si la camara se mueve, recalibrar con:
#   1. POST /camara/debug  -> genera zoom_x100.png con coordenadas pixel a pixel
#   2. POST /camara/roi    -> actualiza en caliente sin reiniciar la API
#   3. Editar este valor para que el cambio sea permanente.
ROI_LED = (613,240, 622, 249)

# Umbral de brillo medio (0-255) para el Criterio 4 (escala de grises).
# Subir si hay falsos positivos con luz ambiente intensa.
# Bajar si el LED no se detecta en condiciones de poca luz.
UMBRAL_BRILLO = 100

# Minimo de pixeles activos en la mascara para que un criterio HSV vote ON.
UMBRAL_PIXELES = 1

# Frames descartados al abrir la camara para que el auto-exposure se estabilice.
WARMUP_FRAMES = 4

# =============================================================================
# BROADLINK
# =============================================================================

BROADLINK_TIMEOUT       = 5   # Timeout de conexion TCP (segundos)
BROADLINK_LEARN_TIMEOUT = 15  # Timeout de modo aprendizaje IR (segundos)

# =============================================================================
# LOGICA DE CONTROL AC
# =============================================================================

# Maximo de reintentos si falla la emision IR por error de red.
MAX_REINTENTOS = 3

# Espera entre reintentos (segundos)
DELAY_REINTENTO = 2.0

# Espera entre pulsaciones consecutivas en modo escalonado.
# El AC necesita este tiempo para procesar cada codigo antes de recibir el siguiente.
DELAY_ENTRE_PULSACIONES = 0.5

# Rango valido de temperatura del AC (grados Celsius).
# Limitado a los codigos IR capturados con el mando fisico.
# Si se capturan mas transiciones, ampliar este rango en consecuencia.
TEMP_MIN = 20
TEMP_MAX = 23

# =============================================================================
# API REST
# =============================================================================

API_HOST = "0.0.0.0"
API_PORT = 8000

# =============================================================================
# LOGGING
# =============================================================================

LOG_LEVEL_FILE    = "DEBUG"
LOG_LEVEL_CONSOLE = "INFO"

LOG_FORMAT   = "%(asctime)s [%(levelname)-8s] %(name)s - %(message)s"
LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

LOG_MAX_BYTES    = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3

# =============================================================================
# SENSOR DE TEMPERATURA AMBIENTE
# =============================================================================

# Backend a usar para leer la temperatura de la sala.
# HTTP   -> servicio HTTP (nodo del cluster con los sensores USB)
# MOCK   -> datos simulados (para desarrollo sin sensor fisico)
SENSOR_BACKEND  = "HTTP"
# URL del servicio HTTP del sensor.
# Probar: curl http://localhost:8001/temperatura   o   /metrics
# Cambiar la IP si el sensor corre en otro nodo del cluster.
SENSOR_HTTP_URL = os.getenv("SENSOR_HTTP_URL", "http://localhost:8001/temperatura")

# Segundos entre lecturas del sensor (tarea de fondo de la API)
SENSOR_POLL_INTERVAL = 30
