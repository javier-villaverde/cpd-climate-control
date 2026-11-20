"""
api/routes.py

API REST del sistema de control AC con metricas Prometheus integradas.

Arrancar:
    uvicorn api.routes:app --host 0.0.0.0 --port 8000 --reload

Endpoints principales:
  GET  /metrics          Metricas Prometheus (scraped por Prometheus)
  GET  /health           Health check
  GET  /estado           Estado actual del AC
  POST /encender         Encender AC
  POST /apagar           Apagar AC
  POST /temperatura      Establecer temperatura (directo o escalonado)
  POST /modo             Cambiar modo
  POST /fan              Cambiar velocidad ventilador
  POST /validar          Sincronizar con camara
  GET  /camara/roi       Ver ROI actual
  POST /camara/roi       Redefinir ROI en caliente
  POST /camara/debug     Capturar imagen de debug
  GET  /sensor           Ultima lectura del sensor de temperatura
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator

import modules.metricas as metricas
from modules.controlador_ac import ControladorAC
from modules.estado_ac import ModoAC, VelocidadFan
from modules.sensor_temperatura import SensorTemperatura

try:
    from config.settings import (
        API_HOST,
        API_PORT,
        SENSOR_POLL_INTERVAL,
        TEMP_MAX,
        TEMP_MIN,
    )
except ImportError:
    API_HOST             = "0.0.0.0"
    API_PORT             = 8000
    TEMP_MIN             = 20
    TEMP_MAX             = 23
    SENSOR_POLL_INTERVAL = 30

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instancias globales
# ---------------------------------------------------------------------------
_controlador: Optional[ControladorAC]     = None
_sensor:       Optional[SensorTemperatura] = None


async def _tarea_sensor(sensor: SensorTemperatura, intervalo: int) -> None:
    """
    Tarea asyncio que lee el sensor periodicamente y actualiza las metricas.
    Corre durante todo el ciclo de vida de la API.
    """
    logger.info("[API] Tarea sensor iniciada (cada %ds)", intervalo)
    while True:
        try:
            lectura = sensor.leer()
            if lectura:
                metricas.actualizar_sensor(lectura.temperatura)
                logger.debug("[API] Sensor: %.1fC  via %s", lectura.temperatura, lectura.backend)
        except Exception as e:
            logger.warning("[API] Error en tarea sensor: %s", e)
        await asyncio.sleep(intervalo)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _controlador, _sensor

    logger.info("[API] Arrancando sistema de control AC...")

    # Controlador AC
    _controlador = ControladorAC()

    # Inicializar metricas con el estado guardado en disco
    metricas.actualizar_desde_estado(_controlador.estado)
    metricas.actualizar_led(None)   # sin lectura de camara aun

    # Sensor de temperatura
    _sensor = SensorTemperatura()
    lectura_inicial = _sensor.leer()
    if lectura_inicial:
        metricas.actualizar_sensor(lectura_inicial.temperatura)
        logger.info(
            "[API] Sensor detectado [%s]: %.1fC",
            lectura_inicial.backend, lectura_inicial.temperatura,
        )
    else:
        logger.warning("[API] Sensor no disponible en el arranque")

    # Tarea de fondo para polling del sensor
    tarea = asyncio.create_task(_tarea_sensor(_sensor, SENSOR_POLL_INTERVAL))

    logger.info("[API] Sistema listo")
    yield  # API corriendo

    logger.info("[API] Apagando sistema...")
    tarea.cancel()
    try:
        await tarea
    except asyncio.CancelledError:
        pass


def _ctrl() -> ControladorAC:
    if _controlador is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Controlador AC no inicializado"
        )
    return _controlador


# ---------------------------------------------------------------------------
# Aplicacion FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Control AC — TFG",
    description=(
        "Control remoto de aire acondicionado via IR (Broadlink) "
        "con lectura de estado por camara y metricas Prometheus."
    ),
    version="3.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class TemperaturaRequest(BaseModel):
    grados: int = Field(..., ge=TEMP_MIN, le=TEMP_MAX,
                        description=f"Temperatura objetivo ({TEMP_MIN}-{TEMP_MAX}C)")
    escalonado: bool = Field(
        False,
        description=(
            "False: envia TEMP_<grados> directo (un solo codigo IR). "
            "True: envia transiciones una a una (TEMP_20_21, TEMP_21_22...)."
        )
    )


class ModoRequest(BaseModel):
    modo: str = Field(..., description=f"Modo: {[m.value for m in ModoAC]}")

    @field_validator("modo")
    @classmethod
    def validar_modo(cls, v: str) -> str:
        v = v.upper()
        if v not in ModoAC.__members__:
            raise ValueError(f"Modo invalido. Validos: {[m.value for m in ModoAC]}")
        return v


class FanRequest(BaseModel):
    velocidad: str = Field(..., description=f"Velocidad: {[v.value for v in VelocidadFan]}")

    @field_validator("velocidad")
    @classmethod
    def validar_velocidad(cls, v: str) -> str:
        v = v.upper()
        if v not in VelocidadFan.__members__:
            raise ValueError(f"Velocidad invalida. Validas: {[v.value for v in VelocidadFan]}")
        return v


class ROIRequest(BaseModel):
    x1: int = Field(..., ge=0)
    y1: int = Field(..., ge=0)
    x2: int = Field(..., ge=1)
    y2: int = Field(..., ge=1)

    @field_validator("x2")
    @classmethod
    def x2_mayor_x1(cls, v, info):
        if "x1" in info.data and v <= info.data["x1"]:
            raise ValueError("x2 debe ser mayor que x1")
        return v

    @field_validator("y2")
    @classmethod
    def y2_mayor_y1(cls, v, info):
        if "y1" in info.data and v <= info.data["y1"]:
            raise ValueError("y2 debe ser mayor que y1")
        return v


# ---------------------------------------------------------------------------
# Manejador de errores
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def manejador_error_generico(request, exc):
    logger.exception("[API] Error no controlado en %s: %s", request.url, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"exito": False, "error": str(exc), "detalle": "Revisa los logs"}
    )


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------
@app.get(
    "/metrics",
    summary="Metricas Prometheus",
    tags=["Prometheus"],
    include_in_schema=False,   # no aparece en /docs para no confundir
)
def get_metrics():
    """
    Endpoint scrapeado por Prometheus cada 15s (configurado en prometheus.yml).
    Devuelve todas las metricas en formato Prometheus text exposition.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Sistema
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Sistema"], summary="Health check")
def health():
    ctrl = _ctrl()
    return {
        "status":   "ok",
        "broadlink": ctrl.broadlink.ip,
        "sensor":   _sensor.backend if _sensor else "no_iniciado",
    }


@app.get("/estado", tags=["Sistema"], summary="Estado actual del AC")
def get_estado():
    return _ctrl().get_estado()


@app.post("/validar", tags=["Sistema"], summary="Sincronizar estado con camara")
def validar_estado():
    ctrl = _ctrl()
    res  = ctrl.validar_estado_actual()
    if res["exito"]:
        metricas.actualizar_led(res["led_final"])
        metricas.actualizar_desde_estado(ctrl.estado)
    if not res["exito"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=res)
    return res


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------
@app.get("/sensor", tags=["Sensor"], summary="Ultima lectura del sensor de temperatura")
def get_sensor():
    """
    Devuelve la ultima lectura del sensor de temperatura ambiente.
    El sensor se lee automaticamente en segundo plano cada SENSOR_POLL_INTERVAL segundos.
    """
    if _sensor is None or _sensor.ultima_lectura is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sensor no disponible"
        )
    lectura = _sensor.ultima_lectura
    return {
        **lectura.to_dict(),
        "backend": _sensor.backend,
    }


# ---------------------------------------------------------------------------
# Camara
# ---------------------------------------------------------------------------
@app.get("/camara/roi", tags=["Camara"], summary="Consultar ROI del LED")
def get_roi():
    ctrl    = _ctrl()
    x1,y1,x2,y2 = ctrl.detector.roi
    return {"roi": {"x1": x1, "y1": y1, "x2": x2, "y2": y2}}


@app.post("/camara/roi", tags=["Camara"], summary="Redefinir ROI en caliente")
def set_roi(body: ROIRequest):
    ctrl          = _ctrl()
    roi_anterior  = ctrl.detector.roi
    ctrl.detector.roi = (body.x1, body.y1, body.x2, body.y2)
    ctrl.detector._validar_roi()
    logger.info("[API] ROI actualizada: %s -> %s", roi_anterior, ctrl.detector.roi)
    return {
        "exito":       True,
        "roi_anterior": dict(zip(["x1","y1","x2","y2"], roi_anterior)),
        "roi_nueva":   {"x1": body.x1, "y1": body.y1, "x2": body.x2, "y2": body.y2},
        "mensaje":     "Para hacerla permanente, edita ROI_LED en config/settings.py",
    }


@app.post("/camara/debug", tags=["Camara"], summary="Capturar imagen de debug")
def capturar_debug():
    ctrl = _ctrl()
    try:
        from config.settings import DEBUG_DIR
    except ImportError:
        DEBUG_DIR = "debug_output"
    resultado = ctrl.detector.capturar_debug(directorio=str(DEBUG_DIR))
    if resultado is None:
        raise HTTPException(status_code=503, detail="Camara no disponible")
    metricas.actualizar_led(resultado)
    return {
        "exito":         True,
        "led_detectado": resultado,
        "estado_led":    "ENCENDIDO" if resultado else "APAGADO",
        "roi_usada":     dict(zip(["x1","y1","x2","y2"], ctrl.detector.roi)),
        "directorio":    str(DEBUG_DIR),
    }


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
def _actualizar_metricas_post_comando(ctrl: ControladorAC, res: dict) -> None:
    """Actualiza metricas Prometheus tras cualquier comando IR."""
    metricas.registrar_comando(res["boton"], res["exito"])
    if res["exito"]:
        metricas.actualizar_desde_estado(ctrl.estado)
    if res.get("led_final") is not None:
        metricas.actualizar_led(res["led_final"])


@app.post("/encender", tags=["Control"], summary="Encender el AC")
def encender():
    ctrl = _ctrl()
    res  = ctrl.encender()
    _actualizar_metricas_post_comando(ctrl, res)
    if not res["exito"]:
        raise HTTPException(status_code=503, detail=res)
    return res


@app.post("/apagar", tags=["Control"], summary="Apagar el AC")
def apagar():
    ctrl = _ctrl()
    res  = ctrl.apagar()
    _actualizar_metricas_post_comando(ctrl, res)
    if not res["exito"]:
        raise HTTPException(status_code=503, detail=res)
    return res


@app.post("/temperatura", tags=["Control"], summary="Establecer temperatura")
def set_temperatura(body: TemperaturaRequest):
    """
    **Modo directo** (escalonado=false): envia TEMP_<grados> en un solo codigo IR.

    **Modo escalonado** (escalonado=true): envia las transiciones una a una
    (TEMP_20_21, TEMP_21_22...). Usar si el AC no responde al codigo directo.
    """
    ctrl = _ctrl()
    res  = ctrl.set_temperatura(body.grados, escalonado=body.escalonado)
    _actualizar_metricas_post_comando(ctrl, res)
    if not res["exito"]:
        raise HTTPException(status_code=400, detail=res)
    return res


@app.post("/modo", tags=["Control"], summary="Cambiar modo de operacion")
def set_modo(body: ModoRequest):
    ctrl = _ctrl()
    res  = ctrl.set_modo(body.modo)
    _actualizar_metricas_post_comando(ctrl, res)
    if not res["exito"]:
        raise HTTPException(status_code=400, detail=res)
    return res


@app.post("/fan", tags=["Control"], summary="Cambiar velocidad del ventilador")
def set_fan(body: FanRequest):
    ctrl = _ctrl()
    res  = ctrl.set_velocidad_fan(body.velocidad)
    _actualizar_metricas_post_comando(ctrl, res)
    if not res["exito"]:
        raise HTTPException(status_code=400, detail=res)
    return res

@app.post("/broadlink/discover", tags=["Sistema"])
def broadlink_discover():
    """
    Lanza un discovery en la red para encontrar el Broadlink.
    Útil cuando el dispositivo ha cambiado de IP por DHCP.
    Guarda la nueva IP en broadlink_config.json automáticamente.
    """
    ctrl = _ctrl()
    try:
        device = ctrl.broadlink._descubrir_y_guardar()
        ctrl.broadlink.device = device
        return {
            "exito": True,
            "ip":  device.host[0],
            "mac": ":".join(format(x, "02x") for x in device.mac),
            "mensaje": "IP actualizada y guardada en broadlink_config.json"
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
